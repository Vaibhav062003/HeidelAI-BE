"""Purchase preparation and coordination using existing cases, quotes and jobs."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from schemas.sales_workflow import PurchaseItem
from services.sales_case_service import _database, _collection, _ensure_org_index, get_active_sales_case

PURCHASE_REQUEST = re.compile(r"\b(need|want|quote|quotation|proforma|buy|proceed|order|invoice|bill|qty|quantity|replace|remove|instead|units?|pcs?)\b", re.I)
TERMINAL = {"billed", "paid", "cancelled", "refunded", "lost", "customer_cancelled"}


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def quantity_supported(quantity: int, proof: str, request: str) -> bool:
    number = re.escape(str(quantity))
    # Bare specifications/model numbers are not order quantities.
    if re.search(rf"\b{number}\s*(?:gb|tb|mb|mhz|ghz|w|hz|inch)\b", proof, re.I):
        return False
    return bool(re.search(rf"(?:\b(?:qty|quantity)\s*[:=]?\s*{number}\b|\b{number}\s*(?:units?|pcs?|pieces?|x)\b)", proof, re.I)
                or (request and re.search(rf"\b{number}\s+{re.escape(request)}(?:\W|$)", proof, re.I)))


def checked_items(items: list[dict]) -> list[dict]:
    if len(items) > 50:
        raise ValueError("A purchase can contain at most 50 items")
    result = [PurchaseItem.model_validate(item).model_dump() for item in items]
    if len({item["line_id"] for item in result}) != len(result):
        raise ValueError("Purchase item IDs must be unique")
    return result


def merge_changes(items: list[dict], changes: list[dict], messages: list[dict]) -> list[dict]:
    """Validate Math's evidence again at the backend trust boundary."""
    sources = {message["message_id"]: message["text"] for message in messages}
    merged = {item["line_id"]: dict(item) for item in checked_items(items)}
    if not isinstance(changes, list) or len(changes) > 50:
        raise ValueError("Invalid purchase preparation")
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Invalid purchase change")
        evidence = change.get("evidence")
        source = sources.get(change.get("source_message_id"), "")
        operation, line_id = change.get("operation"), change.get("line_id")
        if (not isinstance(evidence, str) or not evidence.strip() or evidence not in source
                or operation not in {"add", "update", "remove"}
                or (operation != "add" and line_id not in merged)):
            raise ValueError("A purchase change has no matching customer evidence")
        if operation in {"update", "remove"}:
            old_request = merged[line_id]["request"].casefold()
            if len(merged) > 1 and old_request not in evidence.casefold():
                raise ValueError("Name the existing item when changing a multi-item purchase")
            if operation == "remove" and not re.search(r"\b(remove|cancel|drop|replace|instead)\b", evidence, re.I):
                raise ValueError("Removing an item requires an explicit request")
        if operation == "remove":
            del merged[line_id]
            continue
        old = merged.get(line_id, {})
        data = {key: value for key, value in change.items() if key in PurchaseItem.model_fields}
        request = data.get("request", "")
        if request != old.get("request") and request.casefold() not in evidence.casefold():
            raise ValueError("The requested product is not supported by the message")
        quantity = data.get("quantity")
        if quantity is not None and quantity != old.get("quantity"):
            proof = data.get("quantity_evidence", "")
            if (not proof or proof not in source or str(quantity) not in
                    re.findall(r"(?<!\w)\d+(?!\w)", proof.replace(",", "")) or not quantity_supported(quantity, proof, request)):
                raise ValueError("Quantity is not supported by the customer message")
        merged[line_id] = PurchaseItem.model_validate(data).model_dump()
    return checked_items(list(merged.values()))


async def enabled(org_id: str) -> bool:
    org = await _database().organizations.find_one({"org_id": org_id}, {"sales_workflow_v2_enabled": 1})
    return bool(org and org.get("sales_workflow_v2_enabled") is True)


async def require_enabled(org_id: str) -> None:
    from services.entitlement_service import require_feature
    require_feature(org_id, "AI", strict=True)
    if not await enabled(org_id):
        raise HTTPException(409, "Sales workflow is not enabled for this organization")


async def ensure_case(org_id: str, conversation_id: str, customer_name: str = "") -> dict:
    await _ensure_org_index(org_id)
    case = await get_active_sales_case(org_id, conversation_id)
    if case:
        return case
    now = datetime.now(timezone.utc)
    case = {
        "_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()), "org_id": org_id,
        "conversation_id": conversation_id, "customer_name": customer_name,
        "status": "active", "stage": "new", "version": 0, "workflow_version": 2,
        "items": [], "facts": {}, "signals": {}, "last_move": {},
        "purchase_path": "quote", "next_action": "none", "action_status": "idle",
        "processed_events": [], "action_receipts": [], "stage_history": [],
        "created_at": now, "updated_at": now,
    }
    archived = await _collection(org_id).find_one({"conversation_id": conversation_id, "status": "archived", "next_case_id": {"$exists": True}}, sort=[("archived_at", -1)])
    if archived:
        case.update(case_id=archived["next_case_id"], started_at=archived["archived_at"])
    try:
        await _collection(org_id).insert_one(case)
        await event(case, "purchase_started", case["case_id"])
    except DuplicateKeyError:
        case = await get_active_sales_case(org_id, conversation_id)
    return case


async def event(case: dict, event_type: str, source_id: str, actor: str = "") -> None:
    from services.sales_learning_service import record_sales_learning_event
    await record_sales_learning_event(
        org_id=case["org_id"], conversation_id=case["conversation_id"],
        event_type=event_type, source_id=source_id, case=case, value=actor,
    )


async def publish(case: dict) -> None:
    await refresh_summary(case["org_id"], case["conversation_id"])
    from services.websocket_service import broadcast_org_ws
    await broadcast_org_ws(case["org_id"], "sales_case_updated", {
        "conversation_id": case["conversation_id"], "case_id": case["case_id"], "version": case["version"],
    })


async def save_items(case: dict, items: list[dict], expected_version: int, *, actor: str = "",
                     source_id: str = "", purchase_path: str | None = None) -> dict:
    items = checked_items(items)
    if actor:
        previous = {item["line_id"]: item for item in case.get("items") or []}
        for item in items:
            old = previous.get(item["line_id"], {})
            item["agent_corrected"] = bool(old.get("agent_corrected") or any(item.get(key) != old.get(key) for key in ("request", "quantity", "product_id")))
    fields = {"items": items, "workflow_version": 2, "updated_at": datetime.now(timezone.utc),
              "next_action": "prepare_quote", "action_status": "idle", "action_error": ""}
    if purchase_path:
        fields["purchase_path"] = purchase_path
    if actor:
        fields["last_corrected_by"] = actor
        fields["last_corrected_at"] = datetime.now(timezone.utc)
    update = {"$set": fields, "$inc": {"version": 1}, "$unset": {"preparation": "", "action_source_message_id": ""}}
    if source_id:
        update["$push"] = {"prepared_messages": {"$each": [source_id], "$slice": -200}}
    updated = await _collection(case["org_id"]).find_one_and_update(
        {"case_id": case["case_id"], "status": "active", "version": expected_version},
        update, return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Purchase changed. Refresh before preparing or correcting it")
    await event(updated, "correction" if actor else "preparation", f"{updated['case_id']}:{updated['version']}", actor)
    await publish(updated)
    return updated


async def catalogue_org(org_id: str) -> str:
    org = await _database().organizations.find_one({"org_id": org_id}, {"sales_catalogue_org_id": 1}) or {}
    value = str(org.get("sales_catalogue_org_id") or org_id)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise HTTPException(409, "Sales catalogue configuration is invalid")
    return value


async def verify_matches(org_id: str, items: list[dict], *, manual: bool = False, previous: list[dict] | None = None) -> list[dict]:
    """A model-provided identifier is not sufficient to authorize an order."""
    catalog = await catalogue_org(org_id)
    collection = _database()[f"product_{catalog}"]
    result = []
    old = {item["line_id"]: item for item in previous or []}
    for item in items:
        item = dict(item)
        prior = old.get(item["line_id"], {})
        if item["request"] == prior.get("request") and prior.get("product_id"):
            item["product_id"] = prior["product_id"]
        trusted_previous = item.get("product_id") == prior.get("product_id") and item["request"] == prior.get("request")
        if manual and not item.get("product_id"):
            rows = await collection.find({"org_id": catalog, "status": {"$ne": "withdrawn"}, "$or": [
                {field: {"$regex": re.escape(item["request"]), "$options": "i"}} for field in ("model_name", "sku", "model_no")
            ]}).limit(3).to_list(length=3)
            item["matches"] = [{"product_id": str(row["product_id"]), "name": str(row.get("model_name") or row.get("model_no") or ""), "sku": str(row.get("sku") or row.get("model_no") or "")} for row in rows if row.get("product_id")]
        if item.get("product_id"):
            row = await collection.find_one({"org_id": catalog, "product_id": item["product_id"], "status": {"$ne": "withdrawn"}})
            if not row:
                raise HTTPException(422, "Selected product is not in this organization's catalogue")
            identifiers = [str(row.get(key) or "").strip().casefold() for key in (
                "product_id", "sku", "sku_full", "model_no", "unique_identifier", "model_name")]
            if not manual and not trusted_previous:
                exact = await collection.find({"org_id": catalog, "status": {"$ne": "withdrawn"}, "$or": [
                    {key: {"$regex": "^" + re.escape(item["request"].strip()) + "$", "$options": "i"}}
                    for key in ("product_id", "sku", "sku_full", "model_no", "unique_identifier", "model_name")
                ]}).limit(2).to_list(length=2)
                if item["request"].strip().casefold() not in identifiers or len(exact) != 1 or exact[0].get("product_id") != item["product_id"]:
                    item["product_id"] = None
        result.append(item)
    return result


async def prepare_purchase(org_id: str, conversation_id: str, expected_version: int,
                           *, actor: str = "", message: dict | None = None, expected_case_id: str | None = None) -> dict:
    await require_enabled(org_id)
    if expected_case_id == "new" and await get_active_sales_case(org_id, conversation_id):
        raise HTTPException(409, "A purchase has started. Refresh before preparing it")
    case = await ensure_case(org_id, conversation_id)
    if case["version"] != expected_version or (expected_case_id and expected_case_id != "new" and case["case_id"] != expected_case_id):
        raise HTTPException(409, "Purchase changed. Refresh and try again")
    if case.get("billing_status") in TERMINAL:
        raise HTTPException(409, "Start a new purchase before preparing more items")
    if message:
        messages = [message]
        if case.get("last_corrected_at"):
            row = await _database()[f"messages_{org_id}"].find_one({"conversation_id": conversation_id, "$or": [{"message_id": message["message_id"]}, {"id": message["message_id"]}]})
            if row and isinstance(row.get("timestamp"), datetime) and utc(row["timestamp"]) <= utc(case["last_corrected_at"]):
                messages = []
    else:
        rows = await _database()[f"messages_{org_id}"].find({
            "conversation_id": conversation_id, "role": "customer", "type": {"$in": ["text", "interactive", "button"]},
            **({"timestamp": {"$gt": case.get("last_corrected_at") or case["started_at"]}} if case.get("last_corrected_at") or case.get("started_at") else {}),
        }).sort("timestamp", -1).limit(20).to_list(length=20)
        messages = [{"message_id": str(row.get("message_id") or row.get("id") or row.get("_id")), "text": row["content"][:4000]}
                    for row in reversed(rows) if isinstance(row.get("content"), str) and row["content"].strip()]
    seen = set(case.get("prepared_messages") or [])
    messages = [row for row in messages if row["message_id"] not in seen]
    if messages:
        from services.agent_service import _chatbot_http_client, CHATBOT_BASE_URL
        from services.internal_request import signed_json
        from config.settings import INTERNAL_HMAC_SECRET
        path = "/api/v2/sales/prepare"
        payload = {"org_id": org_id, "messages": messages, "items": case.get("items") or []}
        body, headers = signed_json(payload, path, INTERNAL_HMAC_SECRET)
        response = await _chatbot_http_client().post(CHATBOT_BASE_URL + path, content=body, headers=headers, timeout=45)
        response.raise_for_status()
        result = response.json()
        items = merge_changes(case.get("items") or [], result.get("changes"), messages)
        items = await verify_matches(org_id, items, previous=case.get("items"))
        if items != (case.get("items") or []):
            case = await save_items(case, items, expected_version, source_id=messages[-1]["message_id"])
            await cancel_followups(org_id, conversation_id)
        await _collection(org_id).update_one({"case_id": case["case_id"], "status": "active"}, {
            "$push": {"prepared_messages": {"$each": [row["message_id"] for row in messages], "$slice": -200}},
        })
    return await assess_and_save(case)


async def assess(case: dict) -> dict:
    from services.sales_action_service import _product, _stock_count, _name, _sku
    from services.stock_service import effective_stock_product, _stock_fresh
    from services.pricing_policy import PriceSnapshot, QuoteCandidate, decide_pricing_v1
    from services.pricing_decision_service import get_pricing_settings
    from config.settings import PRICING_FLOOR_MAX_AGE_MINUTES
    from datetime import timedelta

    catalog = await catalogue_org(case["org_id"])
    mode = str((await get_pricing_settings(case["org_id"])).get("mode") or "off")
    lines, blockers = [], []
    for item in case.get("items") or []:
        line = {**item, "name": item["request"], "auto_send": False}
        reasons = []
        if not item.get("product_id"):
            reasons.append("Select the exact catalogue product")
        if item.get("quantity") is None:
            reasons.append("Confirm the quantity")
        product = await _product(catalog, item["product_id"]) if item.get("product_id") else None
        if item.get("product_id") and not product:
            reasons.append("Match the selected product to current stock")
        if product:
            product = await effective_stock_product(catalog, product)
            line.update({"name": _name(product), "sku": _sku(product)})
            stock = _stock_count(product)
            if product.get("stock_resolution_error") or not _stock_fresh(product.get("updated_at")):
                reasons.append("Refresh or verify stock")
            elif stock is None or (item.get("quantity") and stock < item["quantity"]):
                reasons.append("Insufficient confirmed stock")
            try:
                snapshot = PriceSnapshot.from_product({**product, "stock_available": stock})
                amount = snapshot.target_selling_price
                if amount is None:
                    raise ValueError("Approved selling price is missing")
                candidate = QuoteCandidate(amount=amount, sku=snapshot.sku, currency=snapshot.currency,
                    gst_inclusive=snapshot.selling_gst_inclusive, gst_rate=snapshot.gst_rate,
                    quantity=item.get("quantity") or 1, structured=True, exact_product_match=True, actor="ai")
                decision = decide_pricing_v1(snapshot, candidate, now=datetime.now(timezone.utc),
                    max_age=timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES))
                line.update({"unit_price": float(amount), "gst_rate": float(snapshot.gst_rate),
                    "gst_inclusive": snapshot.selling_gst_inclusive,
                    "price_version": snapshot.price_version, "stock": stock})
                line["auto_send"] = mode == "review" and decision.action == "auto_send" and not reasons
                if decision.action != "auto_send":
                    reasons.extend(decision.reasons)
                elif mode != "review":
                    reasons.append("Pricing policy requires agent review")
            except (ValueError, TypeError) as exc:
                reasons.append(str(exc))
        line["issues"] = reasons
        lines.append(line)
        blockers.extend({"line_id": item["line_id"], "reason": reason} for reason in reasons)
    totals = {}
    for line in lines:
        key = line.get("sku") or line.get("product_id")
        if key:
            totals[key] = totals.get(key, 0) + (line.get("quantity") or 0)
    for line in lines:
        key = line.get("sku") or line.get("product_id")
        if line.get("stock") is not None and totals.get(key, 0) > line["stock"]:
            line["issues"].append("Combined quantity exceeds confirmed stock")
            line["auto_send"] = False
            blockers.append({"line_id": line["line_id"], "reason": line["issues"][-1]})
    if not lines:
        blockers.append({"line_id": "", "reason": "Add the products the customer wants"})
    link = await retail_link(case, lines) if case.get("purchase_path") == "retail" else None
    if case.get("purchase_path") == "retail" and not link:
        blockers.append({"line_id": "", "reason": "A verified checkout link is not available"})
    return {"items": lines, "blockers": blockers, "auto_send": bool(lines) and not blockers,
            "retail_link": link, "case_version": case["version"], "prepared_at": datetime.now(timezone.utc)}


async def retail_link(case: dict, lines: list[dict]) -> str | None:
    """Only a current, unambiguous checkout on the connected merchant's host."""
    db = _database()
    connection = await db.shopify_connections.find_one({"org_id": case["org_id"], "status": "connected"})
    if not connection:
        return None
    conversation = await db[f"conversations_{case['org_id']}"].find_one({"conversation_id": case["conversation_id"]}) or {}
    phone = re.sub(r"\D", "", str(conversation.get("customer_phone_no") or conversation.get("customer_id") or ""))
    if not phone:
        return None
    rows = await db.shopify_checkouts.find({"org_id": case["org_id"], "phone": {"$in": [phone, "+" + phone]},
        "record_status": "abandoned", **({"created_at": {"$gte": case["started_at"]}} if case.get("started_at") else {}),
    }).sort("updated_at", -1).limit(2).to_list(length=2)
    if len(rows) != 1:
        return None
    products = rows[0].get("products") or []
    requested = sorted((str(line.get("name") or "").casefold().strip(), line.get("quantity") or 0) for line in lines)
    checkout_items = sorted((str(item.get("name") or "").casefold().strip(), item.get("quantity") or 0) for item in products)
    if not requested or requested != checkout_items:
        return None
    link = str(rows[0].get("recovery_url") or "")
    parsed = urlsplit(link)
    if parsed.scheme == "https" and parsed.hostname == connection.get("shop") and not parsed.username and not parsed.password:
        case["checkout_provider_id"] = rows[0]["provider_id"]
        case["checkout_token"] = rows[0].get("checkout_token") or ""
        case["cart_token"] = rows[0].get("cart_token") or ""
        return link
    return None


async def assess_and_save(case: dict) -> dict:
    preparation = await assess(case)
    updated = await _collection(case["org_id"]).find_one_and_update(
        {"case_id": case["case_id"], "version": case["version"], "status": "active"},
        {"$set": {"preparation": preparation, "workflow_version": 2, **{key: case[key] for key in ("checkout_provider_id", "checkout_token", "cart_token") if key in case}}}, return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Purchase changed while checking commercial facts")
    await publish(updated)
    return updated


def work_summary(case: dict, conversation: dict, work: list[dict], followups: list[dict], now: datetime | None = None) -> dict:
    now = utc(now or datetime.now(timezone.utc))
    summary = {"attention": "waiting", "action": "waiting_for_customer", "reason": "",
        "owner_id": conversation.get("assigned_agent_id"), "owner_name": conversation.get("assigned_agent_name"), "due_at": None}
    if case.get("billing_status") in TERMINAL or case.get("stage") == "lost" or case.get("status") != "active":
        return {**summary, "attention": "completed", "action": "view_purchase"}
    active_work = [item for item in work if item.get("status") in {"pending", "in_progress", "needs_review"} and item.get("case_id") == case.get("case_id")]
    due = sorted([item for item in followups if item.get("status") == "pending" and isinstance(item.get("scheduled_time"), datetime)],
                 key=lambda item: utc(item["scheduled_time"]))
    if active_work:
        active_work.sort(key=lambda item: utc(item.get("due_at") or datetime.max.replace(tzinfo=timezone.utc)))
        item = active_work[0]
        summary.update(attention="blocked", action="open_team_work", reason=item.get("note") or "Waiting for team work",
            owner_id=item.get("assigned_agent_id"), owner_name=item.get("assigned_agent_name") or item.get("team_name"), due_at=item.get("due_at"))
    elif due and utc(due[0]["scheduled_time"]) <= now:
        summary.update(attention="follow_up_due", action="open_followup", due_at=due[0]["scheduled_time"])
    elif case.get("action_status") in {"blocked", "failed"}:
        summary.update(attention="blocked", action="prepare_quote", reason=case.get("action_error") or "Review blocked action")
    elif (case.get("preparation") or {}).get("blockers"):
        summary.update(attention="needs_action", action="edit_requirements", reason=case["preparation"]["blockers"][0]["reason"])
    elif case.get("items") and case.get("quoted_version") != case.get("version"):
        summary.update(attention="needs_action", action="prepare_quote")
    elif due:
        summary.update(due_at=due[0]["scheduled_time"])
    if conversation.get("needs_reply") and not conversation.get("is_ai_enabled"):
        summary.update(attention="needs_action", action="reply_to_customer")
    summary["sort_due_at"] = summary["due_at"] or datetime.max.replace(tzinfo=timezone.utc)
    return summary


async def case_view(case: dict, conversation: dict) -> dict:
    db, org_id = _database(), case["org_id"]
    work, followups = await asyncio.gather(
        db[f"conversation_work_{org_id}"].find({"conversation_id": case["conversation_id"], "status": {"$in": ["pending", "in_progress", "needs_review"]}}).to_list(length=100),
        db.scheduled_messages.find({"org_id": org_id, "conversation_id": case["conversation_id"], "status": "pending"}).to_list(length=100),
    )
    return {**case, "work_summary": work_summary(case, conversation, work, followups)}


async def new_purchase(org_id: str, conversation_id: str, expected_version: int, request_id: str, *, expected_case_id: str | None = None) -> dict:
    await require_enabled(org_id)
    replacement = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{org_id}:{conversation_id}:{request_id}"))
    case = await ensure_case(org_id, conversation_id)
    if case["case_id"] == replacement:
        return case
    if expected_case_id and case["case_id"] != expected_case_id:
        raise HTTPException(409, "A new purchase has started. Refresh first")
    if await _collection(org_id).find_one({"conversation_id": conversation_id, "next_case_id": replacement}):
        raise HTTPException(409, "This new-purchase request was already used")
    if case.get("action_status") == "processing":
        raise HTTPException(409, "Wait for the current purchase action to finish")
    archived = await _collection(org_id).find_one_and_update(
        {"case_id": case["case_id"], "version": expected_version, "status": "active"},
        {"$set": {"status": "archived", "archived_at": datetime.now(timezone.utc), "next_case_id": replacement}},
        return_document=ReturnDocument.AFTER,
    )
    if not archived:
        raise HTTPException(409, "Purchase changed. Refresh and try again")
    await cancel_followups(org_id, conversation_id)
    case = await ensure_case(org_id, conversation_id)
    await publish(case)
    return case


async def cancel_followups(org_id: str, conversation_id: str) -> None:
    await _database().scheduled_messages.update_many(
        {"org_id": org_id, "conversation_id": conversation_id, "status": {"$in": ["pending", "processing", "failed"]}},
        {"$set": {"status": "aborted", "aborted_at": datetime.now(timezone.utc)},
         "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""}},
    )


async def start_action(case: dict, source_id: str, customer_text: str = "") -> dict | None:
    """Each requirements revision owns a distinct existing sales-action job."""
    from services.ai_policy_service import is_effective_ai_enabled
    from services.sales_action_service import execute_sales_action
    if not await enabled(case["org_id"]) or not await is_effective_ai_enabled(case["org_id"], case["conversation_id"]):
        return None
    if case.get("billing_status") in TERMINAL or not (case.get("preparation") or {}).get("auto_send"):
        return None
    if await _database()[f"conversation_work_{case['org_id']}"].find_one({"case_id": case["case_id"], "status": {"$in": ["pending", "in_progress", "needs_review"]}}):
        return None
    from services.sales_action_service import is_invoice_draft_intent
    bill_request = is_invoice_draft_intent(customer_text) or customer_text.strip().casefold() == "proceed"
    action_type = "create_invoice_draft" if bill_request and case.get("quote_id") and case.get("quoted_version") == case["version"] else "start_checkout" if case.get("purchase_path") == "retail" else "create_quote"
    action_source = f"purchase:{case['version']}:{source_id}"
    updated = await _collection(case["org_id"]).find_one_and_update(
        {"case_id": case["case_id"], "version": case["version"], "status": "active"},
        {"$set": {"action_source_message_id": action_source, "action_type": action_type, "action_status": "pending"}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Purchase changed before the action could start")
    await event(updated, "ai_decision", action_source)
    return await execute_sales_action(case=updated, source_message_id=action_source,
                                      action_type=action_type, customer_text=customer_text)


async def automatic_purchase(org_id: str, conversation_id: str, source_id: str, text: str) -> dict | None:
    """V2 purchase requests bypass the stateful chat graph; other chat stays unchanged."""
    if not await enabled(org_id):
        return None
    from services.ai_policy_service import is_effective_ai_enabled
    from services.ai_reply_guard import AIProviderUnavailable
    if not await is_effective_ai_enabled(org_id, conversation_id):
        raise AIProviderUnavailable("ai_disabled", escalation_required=False)
    case = await get_active_sales_case(org_id, conversation_id)
    if not PURCHASE_REQUEST.search(text) and not re.fullmatch(r"\s*(?:stop|unsubscribe)\s*", text, re.I) and not (case and case.get("workflow_version") == 2 and case.get("items")):
        return None
    case = await ensure_case(org_id, conversation_id)
    if re.fullmatch(r"\s*(?:cancel (?:my |the )?(?:order|purchase)|stop|unsubscribe)[.!]?\s*", text, re.I):
        case = await _collection(org_id).find_one_and_update({"case_id": case["case_id"], "status": "active"}, {"$set": {"billing_status": "customer_cancelled", "stage": "lost"}, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER)
        await cancel_followups(org_id, conversation_id)
        if case:
            await publish(case)
        return {"final_response": "I have stopped follow-ups for this purchase and noted your request for the sales team."}
    if case.get("billing_status") in TERMINAL:
        return {"final_response": "This purchase is closed. Our sales team can start a new purchase for you."}
    await event(case, "customer_reply", source_id)
    before_version = case["version"]
    case = await prepare_purchase(org_id, conversation_id, case["version"], message={"message_id": source_id, "text": text[:4000]}, expected_case_id=case["case_id"])
    if case["version"] == before_version and not re.search(r"\b(quote|quotation|checkout|invoice|bill|proceed)\b", text, re.I):
        return None
    action = await start_action(case, source_id, text)
    if action:
        if action.get("pending"):
            raise AIProviderUnavailable("sales_action_in_progress", escalation_required=False)
        if action.get("requires_review"):
            raise AIProviderUnavailable("sales_action_review_required")
        return {"final_response": action["reply"], "sales_action": action}
    issues = (case.get("preparation") or {}).get("blockers") or []
    missing = [line for line in case.get("items") or [] if not line.get("product_id") or not line.get("quantity")]
    if missing:
        lines = [f"• {item['request']}: " + ("please confirm the exact model" if not item.get("product_id") else "how many units do you need?") for item in missing]
        reply = "Please confirm these purchase details:\n" + "\n".join(lines)
    else:
        reply = "I have prepared your requirements for the sales team to review."
    return {"final_response": reply, "purchase_requires_review": bool(issues)}


async def validate_delivery(org_id: str, job_id: str) -> None:
    """Called by both platform senders immediately before a new provider send."""
    from services.ai_reply_guard import AIProviderUnavailable
    job = await _database()[f"sales_action_jobs_{org_id}"].find_one({"_id": job_id}) or {}
    if job.get("purchase_version") is None:
        return
    case = await _collection(org_id).find_one({"case_id": job["case_id"], "status": "active"})
    if (not await enabled(org_id) or not case or case.get("version") != job["purchase_version"]
            or case.get("billing_status") in TERMINAL or case.get("action_source_message_id") != job.get("source_message_id")):
        raise AIProviderUnavailable("sales_action_review_required")
    quote_id = ((job.get("result") or {}).get("fields") or {}).get("quote_id")
    if quote_id:
        quote = await _database()[f"quotes_{org_id}"].find_one({"quote_id": quote_id}) or {}
        if quote.get("revision_number") != 1:
            raise AIProviderUnavailable("sales_action_review_required")
    conversation = await _database()[f"conversations_{org_id}"].find_one({"conversation_id": case["conversation_id"]}) or {}
    phone = re.sub(r"\D", "", str(conversation.get("customer_id") or ""))
    if phone and await _database().whatsapp_suppressions.find_one({"org_id": org_id, "phone": phone}):
        raise AIProviderUnavailable("sales_action_review_required")
    current = await assess(case)
    old = job.get("commercial_snapshot") or {}
    def terms(preparation):
        return [(item.get("product_id"), item.get("quantity"), item.get("unit_price"), item.get("gst_rate"),
                 item.get("gst_inclusive"), item.get("price_version")) for item in preparation.get("items") or []]
    if not current["auto_send"] or terms(current) != terms(old) or current.get("retail_link") != old.get("retail_link"):
        raise AIProviderUnavailable("sales_action_review_required")


async def resume_completed_work() -> None:
    """Poll the existing work records, which also serve as the durable completion queue."""
    from datetime import timedelta
    from loguru import logger
    from services.sales_action_service import mark_sales_action_delivered
    from services.followup_policy import can_auto_send
    orgs = await _database().organizations.find({"sales_workflow_v2_enabled": True}, {"org_id": 1}).to_list(length=1000)
    for org in orgs:
        org_id = org["org_id"]
        active = await _collection(org_id).find({"status": "active", "workflow_version": 2}, {"conversation_id": 1}).to_list(length=1000)
        # ponytail: pilot refresh is bounded to 1,000 active purchases; use event projection at larger scale.
        for purchase in active:
            await refresh_summary(org_id, purchase["conversation_id"])
        collection = _database()[f"conversation_work_{org_id}"]
        work = await collection.find({"status": "completed", "case_id": {"$ne": None}, "workflow_resumed_at": {"$exists": False}}).limit(25).to_list(length=25)
        for item in work:
            now = datetime.now(timezone.utc)
            claimed = await collection.find_one_and_update(
                {"work_id": item["work_id"], "workflow_resumed_at": {"$exists": False}, "$or": [
                    {"workflow_claimed_at": {"$exists": False}}, {"workflow_claimed_at": {"$lt": now - timedelta(minutes=3)}}]},
                {"$set": {"workflow_claimed_at": now}}, return_document=ReturnDocument.AFTER)
            if not claimed:
                continue
            try:
                case = await _collection(org_id).find_one({"case_id": item["case_id"], "status": "active"})
                conversation = await _database()[f"conversations_{org_id}"].find_one({"conversation_id": item["conversation_id"]}) or {}
                other_work = await collection.find_one({"case_id": item["case_id"], "status": {"$in": ["pending", "in_progress", "needs_review"]}})
                latest = await collection.find_one({"case_id": item["case_id"], "status": "completed"}, sort=[("updated_at", -1), ("work_id", -1)])
                if case and not other_work and latest and latest["work_id"] == item["work_id"] and case.get("billing_status") not in TERMINAL:
                    # Completion triggers a new check. It never writes commercial facts.
                    resume_id = f"work:{item['work_id']}:{item['version']}"
                    if case.get("resume_id") != resume_id:
                        case = await _collection(org_id).find_one_and_update(
                            {"case_id": case["case_id"], "status": "active", "version": case["version"]},
                            {"$set": {"resume_id": resume_id}, "$inc": {"version": 1}}, return_document=ReturnDocument.AFTER)
                    if case:
                        case = await assess_and_save(case)
                        await event(case, "preparation", resume_id)
                        newer_reply = conversation.get("last_sender") == "customer" and utc(conversation.get("last_message_timestamp") or now) > utc(item["updated_at"])
                        if not newer_reply and can_auto_send(conversation.get("platform", "whatsapp"), now, conversation.get("reply_window_ends_at")):
                            action = await start_action(case, resume_id)
                            if action and action.get("reply") and not any(action.get(key) for key in ("blocked", "pending", "requires_review")):
                                platform = conversation.get("platform", "whatsapp")
                                if platform == "whatsapp":
                                    from services.wa_service import send_whatsapp_message
                                    result = await send_whatsapp_message({"conversation_id": case["conversation_id"], "content": action["reply"], "sender_id": conversation.get("whatsapp_id") or conversation.get("business_phone_id")}, org_id, "ai", temp_id=f"workflow:{action['job_id']}", bot_context={"sales_action_job_id": action["job_id"]})
                                elif platform == "instagram":
                                    from services.ig_service import send_ig_message
                                    result = await send_ig_message(id=conversation.get("customer_id"), instagram_id=conversation.get("instagram_id"), message_text=action["reply"], org_id=org_id, mode="reply", enforce_ai_state=True, conversation_id=case["conversation_id"], temp_id=f"workflow:{action['job_id']}", bot_context={"sales_action_job_id": action["job_id"]})
                                else:
                                    result = None
                                if result and result.get("sales_action_job_id") == action["job_id"]:
                                    await mark_sales_action_delivered(org_id, action, result, platform=platform)
                await collection.update_one({"work_id": item["work_id"], "workflow_claimed_at": now}, {"$set": {"workflow_resumed_at": datetime.now(timezone.utc)}})
            except Exception as exc:
                # A fresh customer event or an agent preparation can retry after review.
                logger.warning("Purchase work resume requires review: {}", type(exc).__name__)
                await collection.update_one({"work_id": item["work_id"], "workflow_claimed_at": now}, {"$set": {"workflow_resumed_at": datetime.now(timezone.utc), "workflow_error": type(exc).__name__}})


async def verified_outcome(org_id: str, outcome: str, source_id: str, *, quote_id: str = "",
                           order: dict | None = None, _attempt: int = 0) -> bool:
    """Exact provider references only. Phone similarity never links revenue."""
    if outcome not in {"billed", "paid", "cancelled", "refunded"}:
        return False
    clauses = []
    if quote_id:
        quote = await _database()[f"quotes_{org_id}"].find_one({"org_id": org_id, "quote_id": quote_id}) or {}
        if quote.get("case_id"):
            clauses.append({"case_id": quote["case_id"]})
        else:
            clauses.append({"quote_id": quote_id})
    if order:
        clauses.extend({field: order[field]} for field in ("checkout_token", "cart_token") if order.get(field))
        if order.get("provider_id"):
            clauses.append({"order_provider_id": order["provider_id"]})
    if not clauses:
        return False
    matches = await _collection(org_id).find({"workflow_version": 2, "$or": clauses}).limit(2).to_list(length=2)
    if len(matches) != 1:
        return False
    case = matches[0]
    fields = {f"verified_outcomes.{outcome}": {"source_id": source_id, "at": datetime.now(timezone.utc)}}
    if order:
        fields["order_provider_id"] = order["provider_id"]
    # A late invoice event cannot downgrade a payment or refund.
    rank = {"billed": 1, "paid": 2, "cancelled": 3, "refunded": 4}
    if rank[outcome] >= rank.get(case.get("billing_status"), 0):
        fields.update(billing_status=outcome, stage=outcome)
    updated = await _collection(org_id).find_one_and_update(
        {"case_id": case["case_id"], "billing_status": case.get("billing_status")},
        {"$set": fields}, return_document=ReturnDocument.AFTER)
    if not updated:
        if _attempt >= 2:
            raise RuntimeError("Verified purchase changed repeatedly; retry the provider event")
        return await verified_outcome(org_id, outcome, source_id, quote_id=quote_id, order=order, _attempt=_attempt + 1)
    await event(updated, outcome, source_id)
    if updated.get("status") == "active":
        await cancel_followups(org_id, case["conversation_id"])
        await publish(updated)
    return True


async def workflow_report(org_id: str) -> dict:
    cases = _collection(org_id)
    rows = await cases.aggregate([
        {"$match": {"workflow_version": 2}},
        {"$group": {"_id": None, "purchases": {"$sum": 1},
            **{name: {"$sum": {"$cond": [{"$ne": [{"$ifNull": [f"$verified_outcomes.{name}", None]}, None]}, 1, 0]}}
               for name in ("billed", "paid", "cancelled", "refunded")},
            "resolved": {"$sum": {"$cond": [{"$in": ["$billing_status", ["billed", "paid", "cancelled", "refunded", "lost"]]}, 1, 0]}}}},
    ]).to_list(length=1)
    counts = rows[0] if rows else {"purchases": 0, "billed": 0, "paid": 0, "cancelled": 0, "refunded": 0, "resolved": 0}
    counts.pop("_id", None)
    return {**counts, "paid_rate": counts["paid"] / max(1, counts["purchases"]),
            "policy": "fixed_control", "human_minutes_saved": None,
            "measurement_note": "Human effort requires the pilot's sampled time audit; event counts are not minutes saved."}


async def refresh_summary(org_id: str, conversation_id: str) -> None:
    case = await get_active_sales_case(org_id, conversation_id)
    if not case or case.get("workflow_version") != 2:
        return
    conversation = await _database()[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}) or {}
    view = await case_view(case, conversation)
    await _database()[f"conversations_{org_id}"].update_one({"conversation_id": conversation_id}, {
        "$set": {"work_summary": view["work_summary"]},
    })


async def followup_obsolete(org_id: str, conversation_id: str, followup: dict, conversation: dict) -> bool:
    if not await enabled(org_id):
        return False
    case = await get_active_sales_case(org_id, conversation_id)
    if case and (case.get("billing_status") in {"billed", *TERMINAL} or case.get("stage") == "lost"):
        return True
    if case and followup.get("quote_id") and followup["quote_id"] != case.get("quote_id"):
        return True
    if conversation.get("last_sender") == "customer" and conversation.get("last_message_timestamp"):
        if utc(conversation["last_message_timestamp"]) > utc(followup.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)):
            return True
    phone = re.sub(r"\D", "", str(conversation.get("customer_id") or conversation.get("customer_phone_no") or ""))
    return bool(phone and await _database().whatsapp_suppressions.find_one({"org_id": org_id, "phone": phone}))
