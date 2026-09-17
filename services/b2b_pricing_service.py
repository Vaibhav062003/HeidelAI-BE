"""Dealer-triggered retrieval. No bulk history scan, chat replay, or sends."""
import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from pymongo import ReturnDocument

from database import get_async_mongo_db
from services.pricing_policy import PriceSnapshot, QuoteCandidate, decide_b2b_pricing, normalize_inr, normalize_gst_rate, gst_inclusive_inr
from services.sales_offer_policy import quote_revision_snapshot, extract_quoted_prices

EXTRACTOR_VERSION = 1
GREETINGS = re.compile(r"\s*(?:hi|hello|hey|thanks?|thank you|ok(?:ay)?|good morning|good evening)[!.\s]*", re.I)


async def ensure_indexes(org):
    db = get_async_mongo_db()
    await db[f"dealer_pricing_memory_{org}"].create_index([("product_id", 1), ("quoted_at", -1)])
    await db[f"dealer_pricing_memory_{org}"].create_index([("conversation_id", 1), ("quoted_at", -1)])
    await db[f"sales_offers_{org}"].create_index([("line_items.sku", 1), ("created_at", -1)])


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now():
    return datetime.now(timezone.utc)


def fingerprint(row):
    fields = {key: row.get(key) for key in ("content", "role", "mode", "status", "timestamp", "context", "payload")}
    if fields["status"] in {"sent", "delivered", "read"}:
        fields["status"] = "sent"
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def newer_message_query(cid, source):
    return {"conversation_id": cid, "mode": {"$nin": ["private", "note", "ai"]}, "$and": [
        {"$or": [{"role": "customer"}, {"role": "agent", "status": {"$in": ["sent", "delivered", "read"]}}]},
        {"$or": [{"timestamp": {"$gt": source["timestamp"]}},
                 {"timestamp": source["timestamp"], "_id": {"$gt": source.get("_id", source.get("message_id"))}}]}]}


async def enabled(org):
    row = await get_async_mongo_db().organizations.find_one({"org_id": org}, {"b2b_pricing_enabled": 1, "sales_workflow_v2_enabled": 1}) or {}
    return bool(row.get("b2b_pricing_enabled") and row.get("sales_workflow_v2_enabled"))


async def supplier_message(org, source_id):
    # Explicit linked sourcing evidence wins over the contact's general dealer tag.
    return bool(await get_async_mongo_db()[f"pricing_events_{org}"].find_one({
        "source_message_id": source_id, "counterparty_role": "supplier"}))


async def capture_request(case, items):
    from services.product_request_service import capture_product_requests
    from services.product_request_policy import request_key
    from services.sales_workflow_service import catalogue_org
    db, org = get_async_mongo_db(), case["org_id"]
    catalog = await catalogue_org(org)
    for item in case.get("items") or []:
        product = next((row for row in items if row["line_id"] == item["line_id"]), None)
        if not product or not item.get("source_message_id"):
            continue
        source = await db[f"messages_{org}"].find_one({"conversation_id": case["conversation_id"], "$or": [
            {"message_id": item["source_message_id"]}, {"id": item["source_message_id"]}]})
        if not source or await supplier_message(org, item["source_message_id"]):
            continue
        source = {**source, "message_id": item["source_message_id"]}
        raw = {"product_name": item["request"], "source_message_id": item["source_message_id"],
               "evidence": item.get("evidence"), "confidence": "high", "specificity": "specific_product",
               "product_reference_message_id": item["source_message_id"], "product_mention": item["request"]}
        await capture_product_requests({"org_id": org, "conversation_id": case["conversation_id"], "counterparty_role": "buyer"}, [raw], [source])
        key = request_key(org, case["conversation_id"], item["request"])
        await db.product_requests.update_one({"$or": [{"product_key": key}, {"_id": key}],
            "org_id": org, "archived_at": {"$exists": False},
            "source_message_id": item["source_message_id"], "reviewed_by": {"$exists": False}}, {"$set": {
                "product_id": product["product_id"], "catalogue_org_id": catalog, "catalogue_name": product["name"]}})


async def current_product(org, selected_id):
    from services.sales_workflow_service import catalogue_org
    from services.sales_action_service import _product
    catalog = await catalogue_org(org)
    product = await _product(catalog, selected_id)
    if not product or product.get("source_valid") is False or product.get("is_deleted") or product.get("deleted_at"):
        raise ValueError("Exact catalogue product is unavailable")
    publication = product.get("minimum_publication") or {}
    if publication.get("status") in {"publishing", "uncertain"}:
        raise ValueError("Minimum publication is pending or uncertain")
    connection = await get_async_mongo_db().product_sheet_connections.find_one({
        "org_id": catalog, "sheet_id": product.get("price_source_sheet_id"),
        "status": {"$in": ["active", "syncing"]}, "disconnect_requested_at": {"$exists": False}})
    if not connection:
        raise ValueError("Minimum price source is unavailable")
    snapshot = PriceSnapshot.from_product({**product, "stock_available": None,
        "target_selling_price": None, "cost_price": None, "cost_gst_inclusive": None})
    return product, snapshot


async def historical_floor(org, product_id, source_id):
    """Old chats cannot borrow today's floor or an unbounded prior audit value."""
    offer = await get_async_mongo_db()[f"sales_offers_{org}"].find_one({"source_message_id": source_id}) or {}
    return next((row for row in offer.get("minimum_snapshots") or [] if row.get("product_id") == product_id), {})


def floor_in_quote_basis(floor, inclusive, rate):
    if floor.get("approved_floor_price") is None or not isinstance(floor.get("gst_inclusive"), bool):
        return {}
    if normalize_gst_rate(floor.get("gst_rate")) != normalize_gst_rate(rate):
        return {}
    value = gst_inclusive_inr(floor["approved_floor_price"], gst_inclusive=floor["gst_inclusive"], gst_rate=rate)
    if not inclusive:
        value /= 1 + normalize_gst_rate(rate) / 100
    return {**floor, "approved_floor_price": str(value.quantize(Decimal("0.01"))), "gst_inclusive": inclusive}


async def capture_sent_minimums(org, conversation_id, source_id, lines):
    if not lines or not await enabled(org):
        return []
    source = await get_async_mongo_db()[f"messages_{org}"].find_one({"conversation_id": conversation_id,
        "$or": [{"message_id": source_id}, {"id": source_id}]}) or {}
    stamp = source.get("timestamp")
    if not isinstance(stamp, datetime):
        return []
    result = []
    for line in lines[:50]:
        if not line.get("sku"):
            continue
        try:
            product, snapshot = await current_product(org, line["sku"])
            # A delayed outbox must not attribute a later price revision to an earlier offer.
            if snapshot.price_verified_at and timedelta(0) <= utc(stamp) - snapshot.price_verified_at <= timedelta(hours=1):
                result.append({"product_id": str(product["_id"]), "sku": snapshot.sku,
                    "approved_floor_price": str(snapshot.approved_floor_price), "price_version": snapshot.price_version,
                    "gst_rate": str(snapshot.gst_rate), "gst_inclusive": snapshot.selling_gst_inclusive})
        except (ValueError, TypeError):
            continue
    return result


async def _valid_sources(org, evidence):
    ids = list((evidence.get("source_hashes") or {}).keys())
    if not ids:
        return False
    rows = await get_async_mongo_db()[f"messages_{org}"].find({"conversation_id": evidence["conversation_id"],
        "$or": [{"message_id": {"$in": ids}}, {"id": {"$in": ids}}]}).limit(10).to_list(10)
    actual = {str(row.get("message_id") or row.get("id")): fingerprint(row) for row in rows}
    if actual != evidence["source_hashes"]:
        return False
    quote = rows and next((row for row in rows if str(row.get("message_id") or row.get("id")) == evidence["source_message_id"]), None)
    if not quote or quote.get("role") != "agent" or quote.get("mode") in {"private", "note", "ai"} or quote.get("status") not in {"sent", "delivered", "read"}:
        return False
    offer = await get_async_mongo_db()[f"sales_offers_{org}"].find_one({"source_message_id": evidence["source_message_id"]})
    return not offer or offer.get("delivery_status") != "failed"


def public_example(row, same_buyer=False):
    return {key: row.get(key) for key in ("product_id", "sku", "unit_price", "quantity", "gst_rate", "gst_inclusive", "terms", "historical_minimum", "historical_price_version", "outcome")} | {
        "id": row["_id"], "same_buyer": same_buyer, "quoted_at": utc(row["quoted_at"]).isoformat()}


async def history_context(case, items, text):
    db, org, cid = get_async_mongo_db(), case["org_id"], case["conversation_id"]
    messages = db[f"messages_{org}"]
    # Keep reads bounded by the current conversation; never claim ownership via the chat UI GET.
    rows = await messages.find({"conversation_id": cid, "mode": {"$nin": ["private", "note"]},
        "type": {"$in": ["text", "interactive", "button"]}}).sort("timestamp", -1).limit(50).max_time_ms(2000).to_list(50)
    # Exact known product identities also find older negotiations, without a date cutoff.
    identities = list(dict.fromkeys(item["sku"] for item in items if item.get("sku")))[:10]
    if identities:
        pattern = "|".join(re.escape(value) for value in identities)
        older = await messages.find({"conversation_id": cid, "mode": {"$nin": ["private", "note"]},
            "content": {"$regex": pattern, "$options": "i"}}).sort("timestamp", -1).limit(10).max_time_ms(2000).to_list(10)
        for row in older:
            stamp = row.get("timestamp")
            if not isinstance(stamp, datetime):
                continue
            before = await messages.find({"conversation_id": cid, "timestamp": {"$lte": stamp}, "mode": {"$nin": ["private", "note"]}}).sort("timestamp", -1).limit(2).max_time_ms(1000).to_list(2)
            after = await messages.find({"conversation_id": cid, "timestamp": {"$gt": stamp}, "mode": {"$nin": ["private", "note"]}}).sort("timestamp", 1).limit(2).max_time_ms(1000).to_list(2)
            rows.extend([row, *before, *after])
    by_id = {str(row.get("message_id") or row.get("id") or ""): row for row in rows if row.get("message_id") or row.get("id")}
    selected = sorted(by_id.values(), key=lambda row: utc(row.get("timestamp") or now()), reverse=True)[:80]
    by_id = {str(row.get("message_id") or row.get("id")): row for row in selected}
    source_ids = list(by_id)
    supplier_events = await db[f"pricing_events_{org}"].find({"source_message_id": {"$in": source_ids}, "counterparty_role": "supplier"}).limit(80).to_list(80)
    excluded = {str(row.get("source_message_id")) for row in supplier_events}
    state = await db[f"dealer_pricing_memory_{org}"].find_one({"_id": "state:" + cid}) or {}
    hashes = state.get("analyzed_hashes") or {}
    unseen = any(hashes.get(key) != fingerprint(row) for key, row in by_id.items()) or state.get("extractor_version") != EXTRACTOR_VERSION
    history = [{"message_id": key, "conversation_id": cid, "text": str(row.get("content") or "")[:1500],
        "role": row.get("role"), "mode": row.get("mode"), "status": row.get("status"),
        "timestamp": utc(row.get("timestamp") or now()).isoformat()}
        for key, row in reversed(list(by_id.items())) if key not in excluded] if unseen else []
    product_ids = [item["product_id"] for item in items]
    cached = await db[f"dealer_pricing_memory_{org}"].find({"product_id": {"$in": product_ids},
        "extractor_version": EXTRACTOR_VERSION}).sort("quoted_at", -1).limit(50).max_time_ms(2000).to_list(50)
    # Current-conversation examples are preferred without assuming a same-name contact is the same buyer.
    cached.sort(key=lambda row: row.get("conversation_id") != cid)
    offers = []
    for row in cached:
        if await _valid_sources(org, row):
            offers.append(public_example(row, row["conversation_id"] == cid))
    structured = await db[f"sales_offers_{org}"].find({
        "delivery_status": {"$in": ["sent", "delivered", "read"]}, "agent_id": {"$nin": ["ai", "ai-sales"]},
        "$or": [{"conversation_id": cid}, {"line_items.sku": {"$in": identities}}],
    }).sort("created_at", -1).limit(30).max_time_ms(2000).to_list(30)
    for offer in structured:
        if offer.get("agent_id", "").startswith("ai"):
            continue
        source = await messages.find_one({"conversation_id": offer["conversation_id"], "$or": [
            {"message_id": offer["source_message_id"]}, {"id": offer["source_message_id"]}]})
        if not source or source.get("role") != "agent" or source.get("mode") in {"private", "ai", "note"} or source.get("status") not in {"sent", "delivered", "read"}:
            continue
        if await supplier_message(org, offer["source_message_id"]):
            continue
        offer_items, terms = offer.get("line_items") or [], ""
        if offer.get("source_type") == "formal_quote":
            quote = await db[f"quotes_{org}"].find_one({"quote_id": offer.get("quote_id"), "conversation_id": offer["conversation_id"]})
            snapshot = quote_revision_snapshot(quote or {}, offer.get("quote_revision"))
            if not snapshot or snapshot.get("discount"):
                continue
            offer_items, terms = snapshot.get("items") or [], " | ".join(snapshot.get("terms") or [])
        for index, line in enumerate(offer_items):
            product = next((item for item in items if str(line.get("sku") or "").casefold() == item["sku"].casefold()), None)
            if not product or not isinstance(line.get("gst_inclusive"), bool):
                continue
            try:
                price = str(normalize_inr(line.get("unit_price", line.get("private_unit_price"))))
                quantity = Decimal(str(line.get("quantity")))
                rate = str(normalize_gst_rate(line.get("gst_rate")))
                if not quantity.is_finite() or quantity != int(quantity) or not 1 <= quantity <= 9999:
                    continue
            except (ValueError, TypeError, ArithmeticError):
                continue
            stamp = source.get("timestamp")
            if not isinstance(stamp, datetime):
                continue
            floor = await historical_floor(org, product["product_id"], offer["source_message_id"])
            floor = floor_in_quote_basis(floor, line["gst_inclusive"], rate)
            row = {"_id": f"offer:{offer['_id']}:{index}", "product_id": product["product_id"], "sku": product["sku"],
                "conversation_id": offer["conversation_id"], "source_message_id": offer["source_message_id"],
                "source_hashes": {offer["source_message_id"]: fingerprint(source)}, "extractor_version": EXTRACTOR_VERSION,
                "unit_price": price, "quantity": int(quantity), "gst_rate": rate, "gst_inclusive": line["gst_inclusive"],
                "terms": terms, "quoted_at": stamp, "agent_name": offer.get("agent_name"),
                "historical_minimum": floor.get("approved_floor_price"), "historical_price_version": floor.get("price_version"),
                "outcome": offer.get("status") if offer.get("outcome_source") == "tally_verified" else "unknown"}
            if row["historical_minimum"] is not None and normalize_inr(price) < normalize_inr(row["historical_minimum"]):
                continue
            await db[f"dealer_pricing_memory_{org}"].update_one({"_id": row["_id"]}, {"$set": row}, upsert=True)
            offers.append(public_example(row, offer["conversation_id"] == cid))
    offers = list({row["id"]: row for row in offers}.values())
    offers.sort(key=lambda row: (not row["same_buyer"], -datetime.fromisoformat(row["quoted_at"]).timestamp()))
    return history, offers[:50], by_id


async def save_examples(org, examples, sources, items):
    """Recheck model-derived values against source messages at the backend boundary."""
    products = {item["product_id"]: item for item in items}
    result = {}
    for row in examples[:20]:
        if not isinstance(row, dict):
            continue
        source_id = row.get("source_message_id")
        source, product = sources.get(source_id, {}), products.get(row.get("product_id"))
        evidence = row.get("evidence")
        if not product or source.get("role") != "agent" or source.get("mode") in {"private", "ai", "note"} or source.get("status") not in {"sent", "delivered", "read"}:
            continue
        if not isinstance(evidence, str) or not evidence or evidence not in str(source.get("content") or ""):
            continue
        refs = [source_id, row.get("product_source_id"), row.get("quantity_source_id"), row.get("tax_source_id")]
        if row.get("terms"):
            refs.append(row.get("terms_source_id"))
        if any(ref not in sources or sources[ref].get("conversation_id") != source.get("conversation_id") for ref in refs):
            continue
        if any(not isinstance(row.get(field), str) or not row[field] or row[field] not in str(sources[row[prefix + "_source_id"]].get("content") or "")
               for prefix, field in (("product", "product_evidence"), ("quantity", "quantity_evidence"), ("tax", "tax_evidence"))):
            continue
        identity = row["product_evidence"].strip().casefold()
        if identity not in {product["sku"].casefold(), product["name"].casefold()}:
            continue
        if not re.search(r"(?<![\w-])" + re.escape(identity) + r"(?![\w-])",
                         str(sources[row["product_source_id"]].get("content") or "").casefold()):
            continue
        if any(sources[ref].get("role") not in {"agent", "customer"} or sources[ref].get("mode") in {"private", "ai", "note"}
               or (sources[ref].get("role") == "agent" and sources[ref].get("status") not in {"sent", "delivered", "read"}) for ref in refs):
            continue
        if re.search(r"[$€£]|\b(?:USD|EUR|GBP|AED)\b", evidence, re.I):
            continue
        try:
            price = normalize_inr(row.get("unit_price"))
            # Bare reply numbers are allowed only when the model supplied explicit unit, product and tax evidence.
            values = [normalize_inr(value) for value in extract_quoted_prices(evidence)]
            if not values:
                values = [normalize_inr(value) for value in re.findall(
                    r"(?<![\w.])(\d+(?:\.\d{1,2})?)\s*(?:each|per\s+unit|/\s*unit)\b", evidence.replace(",", ""), re.I)]
                if re.fullmatch(r"\s*\d+(?:\.\d{1,2})?\s*", evidence.replace(",", "")):
                    values.append(normalize_inr(evidence))
            if price not in values:
                continue
            rate = str(normalize_gst_rate(row.get("gst_rate")))
            qty = row.get("quantity")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 9999 or not isinstance(row.get("gst_inclusive"), bool):
                continue
            if not re.search(rf"\b{qty}\s*(?:units?|pcs?|pieces?)\b|\b(?:qty|quantity)\s*[:=]?\s*{qty}\b", row["quantity_evidence"], re.I):
                continue
            if qty > 1 and not re.search(r"\b(each|per\s+(?:unit|pc|piece)|unit\s+price|rate)\b|/\s*(?:unit|pc|piece)", evidence, re.I):
                continue
            tax = row["tax_evidence"].lower()
            included = bool(re.search(r"\b(incl(?:uded|usive)?|including)\b", tax))
            extra = bool(re.search(r"\b(extra|excl(?:uded|usive)?|excluding)\b|\+\s*(?:\d+\s*%\s*)?gst", tax))
            if (sources[row["tax_source_id"]].get("role") != "agent" or included == extra or row["gst_inclusive"] != included
                    or not re.search(rf"(?<!\d){re.escape(format(Decimal(rate).normalize(), 'f'))}(?:\.0+)?\s*%", tax)):
                continue
            if row.get("terms") and row["terms"] not in str(sources[row["terms_source_id"]].get("content") or ""):
                continue
        except (ValueError, TypeError):
            continue
        stamp = source.get("timestamp")
        if not isinstance(stamp, datetime) or await supplier_message(org, source_id):
            continue
        floor = await historical_floor(org, product["product_id"], source_id)
        floor = floor_in_quote_basis(floor, row["gst_inclusive"], rate)
        if floor.get("approved_floor_price") is not None and price < normalize_inr(floor["approved_floor_price"]):
            continue
        identity = f"message:{source_id}:{product['product_id']}"
        record = {"_id": identity, "product_id": product["product_id"], "sku": product["sku"],
            "conversation_id": source["conversation_id"], "source_message_id": source_id,
            "source_hashes": {ref: fingerprint(sources[ref]) for ref in set(refs)}, "extractor_version": EXTRACTOR_VERSION,
            "unit_price": str(price), "quantity": qty, "gst_rate": rate, "gst_inclusive": row["gst_inclusive"],
            "terms": str(row.get("terms") or "")[:1000], "quoted_at": stamp, "agent_name": source.get("sender_name"),
            "historical_minimum": floor.get("approved_floor_price"), "historical_price_version": floor.get("price_version"), "outcome": "unknown"}
        await get_async_mongo_db()[f"dealer_pricing_memory_{org}"].update_one({"_id": identity}, {"$set": record}, upsert=True)
        result[(source_id, product["product_id"])] = identity
    return result


async def last_sent_quote(case):
    if not case.get("quote_id"):
        return None
    db, org = get_async_mongo_db(), case["org_id"]
    offer = await db[f"sales_offers_{org}"].find_one({"quote_id": case["quote_id"], "conversation_id": case["conversation_id"],
        "delivery_status": {"$in": ["sent", "delivered", "read"]}}, sort=[("created_at", -1)])
    quote = await db[f"quotes_{org}"].find_one({"quote_id": case["quote_id"], "conversation_id": case["conversation_id"]})
    if not offer or not quote or offer.get("quote_revision") != quote.get("revision_number", 1):
        return None
    return {"quote_id": quote["quote_id"], "revision": offer["quote_revision"], "valid_until": quote.get("valid_until"),
        "items": [{"line_id": row["id"], "sku": row.get("sku"), "quantity": row["quantity"], "unit_price": row["private_unit_price"],
                   "gst_rate": row.get("gst_rate"), "gst_inclusive": row.get("gst_inclusive")} for row in quote.get("items") or []],
        "terms": quote.get("terms") or [], "discount": quote.get("discount") or 0}


async def recommend(case, message):
    """One bounded interpretation per active inbound event, no perpetual backfill."""
    from services.sales_workflow_service import _collection, catalogue_org
    from services.agent_service import _chatbot_http_client, CHATBOT_BASE_URL
    from services.internal_request import signed_json
    from config.settings import INTERNAL_HMAC_SECRET, PRICING_FLOOR_MAX_AGE_MINUTES
    org, cid, source_id = case["org_id"], case["conversation_id"], message["message_id"]
    if GREETINGS.fullmatch(message["text"]):
        return case
    if await supplier_message(org, source_id):
        return case
    db = get_async_mongo_db()
    source = await db[f"messages_{org}"].find_one({"conversation_id": cid, "$or": [{"message_id": source_id}, {"id": source_id}]})
    if not source or not isinstance(source.get("timestamp"), datetime):
        raise HTTPException(409, "Pricing source message is unavailable")
    source_hash = fingerprint(source)
    prior = case.get("b2b_decision") or {}
    if prior.get("source_message_id") == source_id and prior.get("source_hash") == source_hash and prior.get("intent") in {"quote", "accept"}:
        return case
    claim = hashlib.sha256(f"{case['case_id']}:{source_id}".encode()).hexdigest()
    order = str(source.get("_id", source_id))
    # A newer event can supersede in-flight analysis; only its claim may publish.
    claimed = await _collection(org).find_one_and_update({"case_id": case["case_id"], "status": "active", "version": case["version"],
        "$or": [{"b2b_claim_at": {"$exists": False}}, {"b2b_claim_at": {"$lt": source["timestamp"]}},
                {"b2b_claim_at": source["timestamp"], "b2b_claim_order": {"$lte": order}}]},
        {"$set": {"b2b_claim": claim, "b2b_claim_at": source["timestamp"], "b2b_claim_order": order}}, return_document=ReturnDocument.AFTER)
    if not claimed:
        from services.ai_reply_guard import AIProviderUnavailable
        raise AIProviderUnavailable("sales_action_in_progress", escalation_required=False)
    decision = {"source_message_id": source_id, "source_hash": source_hash, "intent": "review", "reason": "Pricing evidence is incomplete", "prices": [], "coverage": "partial"}
    try:
        async with asyncio.timeout(40):
            items = []
            for item in case.get("items") or []:
                if not item.get("product_id") or not item.get("quantity"):
                    raise ValueError("Exact product and quantity are required")
                product, snapshot = await current_product(org, item["product_id"])
                check = decide_b2b_pricing(snapshot, QuoteCandidate(amount=snapshot.approved_floor_price, sku=snapshot.sku,
                    gst_inclusive=snapshot.selling_gst_inclusive, gst_rate=snapshot.gst_rate, structured=True, exact_product_match=True),
                    evidence_ids=("source_check",), max_age=timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES))
                if check.action != "auto_send":
                    raise ValueError(", ".join(check.reasons))
                items.append({"line_id": item["line_id"], "product_id": str(product["_id"]), "sku": snapshot.sku,
                    "name": str(product.get("product_name") or product.get("model_name") or item["request"]), "quantity": item["quantity"],
                    "minimum": str(snapshot.approved_floor_price), "price_version": snapshot.price_version,
                    "gst_rate": str(snapshot.gst_rate), "gst_inclusive": snapshot.selling_gst_inclusive,
                    "mrp": str(snapshot.mrp) if snapshot.mrp is not None else None})
            if not items:
                await _collection(org).update_one({"case_id": case["case_id"], "b2b_claim": claim}, {"$unset": {"b2b_claim": ""}})
                return case
            await capture_request(case, items)
            history, offers, sources = await history_context(case, items, message["text"])
            settings = await db[f"quote_settings_{org}"].find_one({"_id": "branding"}) or {}
            previous = await last_sent_quote(case)
            payload = {"org_id": org, "message": message["text"][:4000], "items": items, "history": history, "offers": offers,
                "seller_terms": [str(value)[:1000] for value in (settings.get("terms") or [])[:20]], "last_quote": previous, "coverage": "partial"}
            body, headers = signed_json(payload, "/api/v2/sales/pricing", INTERNAL_HMAC_SECRET)
            response = await _chatbot_http_client().post(CHATBOT_BASE_URL + "/api/v2/sales/pricing", content=body, headers=headers, timeout=30)
            response.raise_for_status()
            result = response.json()
            refs = await save_examples(org, result.get("examples") or [], sources, items)
            intent = result.get("intent")
            if intent not in {"quote", "accept", "none", "review"}:
                raise ValueError("Unrecognised pricing decision")
            prices = []
            for proposal in (result.get("proposals") or [])[:50]:
                item = next((row for row in items if row["line_id"] == proposal.get("line_id")), None)
                if not item:
                    continue
                evidence_ids = list(dict.fromkeys(refs.get((ref, item["product_id"]), ref) for ref in proposal.get("evidence_ids") or [] if isinstance(ref, str)))
                evidence = await db[f"dealer_pricing_memory_{org}"].find({"_id": {"$in": evidence_ids}, "product_id": item["product_id"]}).limit(10).to_list(10)
                if not evidence_ids or len(evidence) != len(evidence_ids) or not all([await _valid_sources(org, row) for row in evidence]):
                    continue
                amount = normalize_inr(proposal["unit_price"])
                if amount < normalize_inr(item["minimum"]) or (item["mrp"] and amount > normalize_inr(item["mrp"])):
                    continue
                prices.append({**item, "unit_price": str(amount), "evidence_ids": evidence_ids, "reason": str(proposal.get("reason") or "")[:500]})
            if intent == "accept":
                from services.sales_action_service import is_invoice_draft_intent
                explicit_acceptance = bool(re.fullmatch(r"\s*(?:proceed|confirmed|confirm(?:\s+the)?\s+order|yes(?:[, ]+(?:please|proceed|go ahead))?|go ahead|accepted|book it)[!.\s]*", message["text"], re.I)
                    or is_invoice_draft_intent(message["text"]))
                old_prices = (case.get("b2b_decision") or {}).get("prices") or []
                # Accept only the latest sent, unexpired quote with exactly the current commercial terms.
                valid = bool(explicit_acceptance and previous and not previous["discount"] and case.get("quoted_version") == case["version"]
                    and str(previous.get("valid_until") or "") >= now().date().isoformat() and len(old_prices) == len(items))
                expected_lines = {row["line_id"] for row in items}
                sent_lines = (previous or {}).get("items") or []
                valid = valid and len(expected_lines) == len(items) == len(sent_lines)
                valid = valid and {row.get("line_id") for row in sent_lines} == expected_lines == {row.get("line_id") for row in old_prices}
                for item in items:
                    old = next((row for row in old_prices if row["line_id"] == item["line_id"]), {})
                    sent = next((row for row in (previous or {}).get("items", []) if row["line_id"] == item["line_id"]), {})
                    valid = valid and old.get("quantity") == item["quantity"] and sent.get("quantity") == item["quantity"]
                    valid = valid and str(sent.get("sku") or "").casefold() == item["sku"].casefold()
                    valid = valid and sent.get("gst_inclusive") == item["gst_inclusive"] and normalize_gst_rate(sent.get("gst_rate", 0)) == normalize_gst_rate(item["gst_rate"])
                    valid = valid and old.get("gst_inclusive") == item["gst_inclusive"] and normalize_gst_rate(old.get("gst_rate", 0)) == normalize_gst_rate(item["gst_rate"])
                    valid = valid and bool(old.get("unit_price") and sent.get("unit_price")) and normalize_inr(old.get("unit_price", 1)) == normalize_inr(sent.get("unit_price", 2))
                    valid = valid and normalize_inr(old.get("unit_price", 1)) >= normalize_inr(item["minimum"])
                if valid:
                    prices = [{**old, "price_version": item["price_version"], "minimum": item["minimum"]}
                              for item in items for old in old_prices if old["line_id"] == item["line_id"]]
                else:
                    intent = "quote" if len(prices) == len(items) else "review"
            if intent == "quote" and (len(prices) != len(items) or len({row["line_id"] for row in prices}) != len(items)):
                intent = "review"
            decision.update(intent=intent, reason=str(result.get("reason") or "")[:500], prices=prices,
                usage=result.get("usage") or {}, interpreted_at=now(), accepted_quote=previous if intent == "accept" else None)
            await db[f"dealer_pricing_memory_{org}"].update_one({"_id": "state:" + cid}, {"$set": {
                "extractor_version": EXTRACTOR_VERSION, "analyzed_hashes": {key: fingerprint(row) for key, row in sources.items()}, "updated_at": now()}}, upsert=True)
    except (ValueError, TypeError, KeyError, TimeoutError) as exc:
        decision.update(intent="review", reason=str(exc)[:300] or "History retrieval timed out", prices=[])
    except Exception:
        decision.update(intent="review", reason="Pricing history or AI service is temporarily unavailable", prices=[])
    # Any intervening human correction or newer buyer message invalidates this result.
    source = await db[f"messages_{org}"].find_one({"conversation_id": cid, "$or": [{"message_id": source_id}, {"id": source_id}]})
    if not source or fingerprint(source) != source_hash:
        from services.ai_reply_guard import AIProviderUnavailable
        raise AIProviderUnavailable("sales_action_in_progress", escalation_required=False)
    if source and source.get("timestamp"):
        newer = await db[f"messages_{org}"].find_one(newer_message_query(cid, source))
        if newer:
            from services.ai_reply_guard import AIProviderUnavailable
            raise AIProviderUnavailable("sales_action_in_progress", escalation_required=False)
    # Retain a valid outstanding offer when no price action is needed.
    if decision["intent"] == "none":
        await _collection(org).update_one({"case_id": case["case_id"], "b2b_claim": claim}, {"$unset": {"b2b_claim": ""}})
        return case
    old = case.get("b2b_decision") or {}
    changed = decision["intent"] == "quote" and [(r["line_id"], r["unit_price"], r["quantity"]) for r in decision["prices"]] != [
        (r["line_id"], r["unit_price"], r["quantity"]) for r in old.get("prices") or []]
    update = {"$set": {"b2b_decision": decision, "b2b_pricing": True, "purchase_path": "quote"}, "$unset": {"b2b_claim": ""}}
    if changed:
        update["$inc"] = {"version": 1}
        update["$unset"].update({"preparation": "", "action_source_message_id": ""})
    saved = await _collection(org).find_one_and_update({"case_id": case["case_id"], "version": case["version"], "status": "active", "b2b_claim": claim}, update, return_document=ReturnDocument.AFTER)
    if not saved:
        from services.ai_reply_guard import AIProviderUnavailable
        raise AIProviderUnavailable("sales_action_in_progress", escalation_required=False)
    return saved


async def product_history(user, product_id):
    from services.work_team_service import conversation_access_condition
    db = get_async_mongo_db()
    access = await conversation_access_condition(user.org_id, user.user_id, user.role)
    pipeline = [{"$match": {"product_id": product_id}}, {"$sort": {"quoted_at": -1}}, {"$lookup": {
        "from": f"conversations_{user.org_id}", "let": {"cid": "$conversation_id"}, "pipeline": [
            {"$match": {"$and": [access, {"$expr": {"$eq": ["$conversation_id", "$$cid"]}}]}},
            {"$project": {"customer_name": 1}}], "as": "buyer"}}, {"$match": {"buyer.0": {"$exists": True}}}, {"$limit": 50}]
    rows = await db[f"dealer_pricing_memory_{user.org_id}"].aggregate(pipeline).to_list(50)
    valid = []
    for row in rows:
        if await _valid_sources(user.org_id, row):
            row.pop("source_hashes", None)
            row["customer_name"] = row.pop("buyer")[0].get("customer_name") or "Buyer"
            valid.append(row)
    audit = await db.stock_change_audit.find({"org_id": user.org_id, "product_id": product_id, "kind": "daily_minimum"}).sort("created_at", -1).limit(50).to_list(50)
    return {"items": valid, "audit": audit, "coverage": "partial", "note": "History is interpreted when a dealer enquires. This is not a complete sales ledger."}


async def assess(case):
    """Send-time validation reads current prices and evidence; it never calls AI."""
    from services.pricing_decision_service import get_pricing_settings
    from config.settings import PRICING_FLOOR_MAX_AGE_MINUTES
    db, org = get_async_mongo_db(), case["org_id"]
    mode = (await get_pricing_settings(org)).get("mode")
    decision = case.get("b2b_decision") or {}
    lines, blockers = [], []
    allowed = await enabled(org)
    for item in case.get("items") or []:
        line = {**item, "name": item["request"], "auto_send": False, "issues": [], "stock": None}
        reasons = line["issues"]
        proposal = next((row for row in decision.get("prices") or [] if row.get("line_id") == item["line_id"]), {})
        try:
            if not allowed:
                raise ValueError("B2B pricing is disabled")
            if not item.get("product_id") or not item.get("quantity"):
                raise ValueError("Confirm the exact product and quantity")
            if decision.get("intent") not in {"quote", "accept"} or not proposal:
                raise ValueError(decision.get("reason") or "Comparable human pricing evidence is needed")
            product, snapshot = await current_product(org, item["product_id"])
            if proposal.get("product_id") != str(product["_id"]) or proposal.get("quantity") != item["quantity"]:
                raise ValueError("Product or quantity changed since pricing was prepared")
            if proposal.get("gst_inclusive") != snapshot.selling_gst_inclusive or normalize_gst_rate(proposal.get("gst_rate")) != snapshot.gst_rate:
                raise ValueError("Tax treatment changed since pricing was prepared")
            refs = proposal.get("evidence_ids") or []
            examples = await db[f"dealer_pricing_memory_{org}"].find({"_id": {"$in": refs}, "product_id": str(product["_id"])}).limit(10).to_list(10)
            if not refs or len(examples) != len(refs) or not all([await _valid_sources(org, row) for row in examples]):
                raise ValueError("Human pricing evidence changed or is unavailable")
            candidate = QuoteCandidate(amount=proposal["unit_price"], sku=snapshot.sku, quantity=item["quantity"],
                gst_inclusive=proposal["gst_inclusive"], gst_rate=proposal["gst_rate"], structured=True, exact_product_match=True)
            checked = decide_b2b_pricing(snapshot, candidate, evidence_ids=tuple(refs), max_age=timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES))
            if checked.action != "auto_send":
                reasons.extend(checked.reasons)
            if mode != "review":
                reasons.append("Pricing is in observation mode; automatic pricing is not enabled")
            line.update(sku=snapshot.sku, unit_price=float(candidate.amount), gst_rate=float(snapshot.gst_rate),
                gst_inclusive=snapshot.selling_gst_inclusive, price_version=snapshot.price_version,
                minimum=str(snapshot.approved_floor_price), evidence_ids=refs, provisional_availability=True)
            line["auto_send"] = not reasons
        except (ValueError, TypeError) as exc:
            reasons.append(str(exc))
        blockers.extend({"line_id": item["line_id"], "reason": reason} for reason in reasons)
        lines.append(line)
    if not lines:
        blockers.append({"line_id": "", "reason": "Add the products the buyer wants"})
    return {"items": lines, "blockers": blockers, "auto_send": bool(lines) and not blockers,
            "retail_link": None, "case_version": case["version"], "prepared_at": now(), "b2b_pricing": True}


async def validate_output(org, conversation_id, content):
    from services.sales_workflow_service import validate_delivery
    from services.ai_reply_guard import AIProviderUnavailable
    job = await get_async_mongo_db()[f"sales_action_jobs_{org}"].find_one({
        "conversation_id": conversation_id, "commercial_snapshot.b2b_pricing": True,
        "status": "ready", "result.reply": content}, sort=[("created_at", -1)])
    if not job:
        return False, "grounded_b2b_quote_required"
    try:
        await validate_delivery(org, job["_id"])
    except AIProviderUnavailable:
        return False, "b2b_quote_changed"
    return True, "validated_b2b_quote"
