import re
import math
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from database import get_async_mongo_db
from services.product_sheet_sync import sync_product_sheet, write_product_sheet_gst_bulk, write_product_sheet_values
from services.stock_gst import DEFAULT_GST_RATE, normalize_category, normalize_gst_inclusive, normalize_gst_rate, select_gst_rows
from services.tally_service import (
    build_master_xml,
    build_physical_stock_xml,
    get_integration,
    normalized_settings,
    queue_tally_sync,
)
from services.utils import serialize_mongo
from services.websocket_service import broadcast_org_admin_ws, broadcast_org_ws, send_to_user


db = get_async_mongo_db()
ADMIN_ROLES = {"admin", "org:admin"}
EDITABLE_FIELDS = {"price", "description", "quantity", "gst_inclusive", "gst_rate"}
FINAL_STATES = {"completed", "rejected"}


async def ensure_stock_indexes() -> None:
    await db.stock_change_requests.create_index("change_id", unique=True)
    await db.stock_change_requests.create_index([("org_id", 1), ("status", 1), ("created_at", -1)])
    await db.stock_change_requests.create_index([("org_id", 1), ("product_id", 1), ("created_at", -1)])
    await db.stock_change_audit.create_index([("org_id", 1), ("product_id", 1), ("created_at", -1)])
    await db.stock_change_audit.create_index([("org_id", 1), ("change_id", 1), ("created_at", 1)])
    await db.stock_product_links.create_index([("org_id", 1), ("sheet_product_id", 1)], unique=True)
    await db.stock_product_links.create_index([("org_id", 1), ("tally_master_id", 1)], unique=True)
    await db.stock_allocations.create_index([("org_id", 1), ("tally_master_id", 1)], unique=True)
    await db.product_sheet_connections.create_index(
        [("org_id", 1), ("is_primary", 1)], unique=True,
        partialFilterExpression={"is_primary": True}, name="one_primary_stock_sheet_per_org",
    )


def normalize_sku(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _sheet_sku(product: dict) -> str:
    return str(
        product.get("model_no") or product.get("sku") or product.get("sku_full")
        or product.get("unique_identifier") or product.get("product_name") or ""
    ).strip()


def _number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_error(prefix: str, error: Exception) -> str:
    detail = error.detail if isinstance(error, HTTPException) else str(error) if isinstance(error, ValueError) else type(error).__name__
    return f"{prefix}: {detail}"[:500]


def _sheet_quantity(product: dict) -> float:
    if product.get("source_valid") is False:
        return 0.0
    value = product.get("stock", product.get("quantity", product.get("available_qty")))
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None and isinstance(product.get("in_stock"), bool):
        return 1.0 if product["in_stock"] else 0.0
    return _number(value)


def _primary_source_product(product: dict, primary: dict | None) -> dict:
    """Shared Stock/restock view of the primary snapshot (never secondary residue)."""
    result = dict(product)
    sheet_id = str((primary or {}).get("sheet_id") or "")
    values = (product.get("source_values") or {}).get(sheet_id)
    if values is not None:
        for field in ("stock", "quantity", "available_qty", "in_stock"):
            result.pop(field, None)
        result.update(values)
        result["updated_at"] = values.get("updated_at") or (primary or {}).get("last_synced_at")
    return result


def _stock_fresh(stamp) -> bool:
    if not isinstance(stamp, datetime):
        return False
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    age = datetime.now(timezone.utc) - stamp
    return -timedelta(minutes=5) <= age <= timedelta(hours=24)


async def effective_stock_product(org_id: str, product: dict, database=None) -> dict:
    try:
        return await asyncio.wait_for(_effective_stock_product(org_id, product, database), timeout=5)
    except (PyMongoError, TimeoutError):
        return {**product, "stock_resolution_error": "Stock source could not be verified; retry after stock sync"}


async def _effective_stock_product(org_id: str, product: dict, database=None) -> dict:
    """Bounded, fail-closed restock lookup using Stock's merge/source precedence.

    Only matching SKU candidates and explicit links are read, not the inventory.
    Ambiguous/truncated matches must be reconciled in Stock before outreach.
    """
    database = database if database is not None else db
    result = {**product, "stock_resolution_error": "Stock source is unverified"}
    primary = await database.product_sheet_connections.find_one({"org_id": org_id, "is_primary": True})
    effective = _primary_source_product(product, primary)
    sources = product.get("source_sheet_ids") or []
    sources = [sources] if isinstance(sources, str) else list(sources)
    if product.get("source_sheet_id"):
        sources.append(product["source_sheet_id"])
    if sources:
        primary_id = str((primary or {}).get("sheet_id") or "")
        authority = primary if primary_id in sources else await database.product_sheet_connections.find_one({
            "org_id": org_id, "sheet_id": product.get("price_source_sheet_id") or (sources[0] if len(set(sources)) == 1 else None),
        })
        if (not authority or authority.get("status") not in {"active", "syncing"}
                or authority.get("disconnect_requested_at")):
            return result
        snapshot = (product.get("source_values") or {}).get(str(authority.get("sheet_id")))
        if not isinstance(snapshot, dict) or snapshot.get("source_valid") is False:
            return result
        effective = _primary_source_product(product, authority)
    if effective.get("source_valid") is False:
        return result
    key = normalize_sku(_sheet_sku(effective))
    if not key or len(key) > 200:
        return result
    pattern = "^[^a-z0-9]*" + "[^a-z0-9]*".join(re.escape(c) for c in key) + "[^a-z0-9]*$"
    fields = ["model_no", "sku", "sku_full", "unique_identifier", "product_name"]
    if (primary or {}).get("sheet_id"):
        fields.append(f"source_values.{primary['sheet_id']}.model_no")
    candidates = await database[f"products_{org_id}"].find({"$or": [
        {field: {"$regex": pattern, "$options": "i"}} for field in fields
    ]}).limit(6).max_time_ms(2000).to_list(6)
    if len(candidates) >= 6:
        return result
    sheets = [_primary_source_product(item, primary) for item in candidates]
    sheets = [item for item in sheets if normalize_sku(_sheet_sku(item)) == key]
    if len(sheets) != 1 or str(sheets[0].get("_id")) != str(product.get("_id")):
        return result
    # The candidate read is newer than the caller's product snapshot. Do not
    # resurrect quantity that changed between the initial lookup and this check.
    effective = _primary_source_product(sheets[0], authority if sources else primary)
    if effective.get("source_valid") is False:
        return result
    tally = await database.tally_stock_items.find({"org_id": org_id, "$or": [
        {field: {"$regex": pattern, "$options": "i"}} for field in ("model_number", "name")
    ]}).limit(6).max_time_ms(2000).to_list(6)
    if len(tally) >= 6:
        return result
    tally = [item for item in tally if normalize_sku(item.get("model_number") or item.get("name")) == key]
    link = await database.stock_product_links.find_one({"org_id": org_id, "sheet_product_id": str(product.get("_id"))})
    if link:
        linked = await database.tally_stock_items.find_one({"org_id": org_id, "master_id": link.get("tally_master_id")})
        if not linked:
            return result
        if not any(item.get("master_id") == linked.get("master_id") for item in tally):
            tally.append(linked)
    # A candidate linked elsewhere may have been moved out of this SKU group.
    # Refuse uncertain conflicts rather than promise another product's stock.
    if tally and await database.stock_product_links.find_one({"org_id": org_id,
        "tally_master_id": {"$in": [str(item.get("master_id")) for item in tally]},
        "sheet_product_id": {"$ne": str(product.get("_id"))}}):
        return result
    integration = await database.tally_integrations.find_one({"org_id": org_id}, {"last_seen_at": 1})
    online = _tally_online(integration)
    merged = _row([effective], tally, key, online)
    stamp = tally[0].get("synced_at") if online and len(tally) == 1 else effective.get("updated_at")
    quantity = merged["quantity"]
    if merged["conflicts"] or not math.isfinite(quantity) or not _stock_fresh(stamp):
        return result
    return {**effective, "stock": quantity, "updated_at": stamp, "stock_resolution_error": None}


def _version(sheet: dict | None, tally: dict | None) -> str:
    sheet_version = str((sheet or {}).get("updated_at") or "")
    tally_version = str((tally or {}).get("alter_id") or (tally or {}).get("synced_at") or "")
    return f"{sheet_version}|{tally_version}"


def _row(sheet_rows: list[dict], tally_rows: list[dict], key: str, tally_available: bool = True) -> dict:
    sheet = sheet_rows[0] if len(sheet_rows) == 1 else None
    tally = tally_rows[0] if len(tally_rows) == 1 else None
    conflicts = []
    if len(sheet_rows) > 1:
        conflicts.append("duplicate_sheet_sku")
    if len(tally_rows) > 1:
        conflicts.append("duplicate_tally_sku")
    sku = _sheet_sku(sheet or {}) or str((tally or {}).get("model_number") or (tally or {}).get("name") or key)
    source = "both" if sheet and tally else "sheet" if sheet else "tally"
    sheet_price = (sheet or {}).get("price")
    price = _number(sheet_price, _number((tally or {}).get("standard_rate")))
    quantity = (
        _number((tally or {}).get("closing_quantity"))
        if tally and (tally_available or not sheet)
        else _sheet_quantity(sheet or {})
    )
    description = str(
        (sheet or {}).get("description") or (sheet or {}).get("product_name")
        or (tally or {}).get("description") or (tally or {}).get("name") or sku
    )
    category = normalize_category((sheet or {}).get("category") or (sheet or {}).get("type") or (tally or {}).get("parent"))
    sheet_has_gst_rate = (sheet or {}).get("gst_rate") is not None
    tally_rate = (tally or {}).get("gst_rate")
    if tally_rate == 0 and not str((tally or {}).get("gst_applicable") or "").strip():
        tally_rate = None
    gst_rate = normalize_gst_rate((sheet or {}).get("gst_rate") if sheet_has_gst_rate else tally_rate, DEFAULT_GST_RATE)
    gst_inclusive = normalize_gst_inclusive((sheet or {}).get("gst_inclusive"))
    product_id = str((sheet or {}).get("_id") or f"tally:{(tally or {}).get('master_id') or key}")
    allocations = (tally or {}).get("allocations") or []
    if tally and (not allocations or abs(sum(_number(item.get("quantity")) for item in allocations) - quantity) > 0.001):
        allocations = [{"godown": "Main Location", "batch": "Primary Batch", "quantity": quantity}]
    return {
        "id": product_id,
        "sku": sku,
        "normalized_sku": key,
        "name": str((sheet or {}).get("product_name") or (sheet or {}).get("model_name") or (tally or {}).get("name") or sku),
        "description": description,
        "category": category,
        "price": price,
        "gst_inclusive": gst_inclusive,
        "gst_rate": gst_rate,
        "quantity": quantity,
        "unit": (tally or {}).get("unit") or (sheet or {}).get("unit") or "Nos",
        "source": source,
        "status": "conflict" if conflicts else "in_stock" if quantity > 0 else "out_of_stock",
        "conflicts": conflicts,
        "version": _version(sheet, tally),
        "sheet": {
            "product_id": str((sheet or {}).get("_id") or ""),
            "price": _number(sheet_price) if sheet_price is not None else None,
            "quantity": _sheet_quantity(sheet or {}) if sheet else None,
            "description": (sheet or {}).get("description") or (sheet or {}).get("product_name"),
            "category": (sheet or {}).get("category"),
            "gst_inclusive": gst_inclusive,
            "gst_rate": gst_rate,
            "source_sheet_ids": (sheet or {}).get("source_sheet_ids") or [],
            "source_rows": (sheet or {}).get("source_rows") or {},
        } if sheet else None,
        "tally": {
            "master_id": (tally or {}).get("master_id"),
            "name": (tally or {}).get("name"),
            "model_number": (tally or {}).get("model_number"),
            "description": (tally or {}).get("description"),
            "price": _number((tally or {}).get("standard_rate")),
            "gst_rate": normalize_gst_rate((tally or {}).get("gst_rate"), DEFAULT_GST_RATE),
            "quantity": _number((tally or {}).get("closing_quantity")),
            "alter_id": (tally or {}).get("alter_id"),
            "allocations": allocations,
        } if tally else None,
        "_sheet_document": sheet,
        "_tally_document": tally,
    }


async def _merged_stock(org_id: str) -> list[dict]:
    # ponytail: merge in memory up to 50k rows/source; materialize only when measured inventory size needs it.
    integration = await db.tally_integrations.find_one({"org_id": org_id}, {"last_seen_at": 1})
    tally_available = _tally_online(integration)
    products = await db[f"products_{org_id}"].find({}, {
        "product_name": 1, "model_name": 1, "model_no": 1, "sku": 1, "sku_full": 1,
        "unique_identifier": 1, "description": 1, "price": 1, "stock": 1, "quantity": 1,
        "available_qty": 1, "in_stock": 1, "unit": 1, "source_sheet_ids": 1,
        "source_rows": 1, "source_values": 1, "category": 1, "type": 1,
        "gst_inclusive": 1, "gst_rate": 1, "updated_at": 1,
        "target_selling_price": 1, "approved_floor_price": 1,
    }).to_list(length=50_000)
    primary = await db.product_sheet_connections.find_one(
        {"org_id": org_id, "status": "active", "is_primary": True}, {"sheet_id": 1, "last_synced_at": 1},
    )
    primary_sheet_id = str((primary or {}).get("sheet_id") or "")
    if primary_sheet_id:
        products = [_primary_source_product(product, primary) for product in products]
    tally = await db.tally_stock_items.find({"org_id": org_id}, {"sync_id": 0}).to_list(length=50_000)
    confirmed_allocations = {
        str(item.get("tally_master_id")): item
        for item in await db.stock_allocations.find({"org_id": org_id}, {"_id": 0}).to_list(length=50_000)
    }
    for item in tally:
        saved = confirmed_allocations.get(str(item.get("master_id")))
        rows = (saved or {}).get("allocations") or []
        if rows and abs(sum(_number(row.get("quantity")) for row in rows) - _number(item.get("closing_quantity"))) <= 0.001:
            item["allocations"] = rows
    sheet_by_sku: dict[str, list[dict]] = {}
    tally_by_sku: dict[str, list[dict]] = {}
    for product in products:
        key = normalize_sku(_sheet_sku(product))
        if key:
            sheet_by_sku.setdefault(key, []).append(product)
    for item in tally:
        key = normalize_sku(item.get("model_number") or item.get("name"))
        if key:
            tally_by_sku.setdefault(key, []).append(item)
    links = await db.stock_product_links.find({"org_id": org_id}, {"_id": 0}).to_list(length=50_000)
    products_by_id = {str(item.get("_id")): item for item in products}
    tally_by_id = {str(item.get("master_id")): item for item in tally}
    for link in links:
        product = products_by_id.get(str(link.get("sheet_product_id")))
        item = tally_by_id.get(str(link.get("tally_master_id")))
        if not product or not item:
            continue
        sheet_key = normalize_sku(_sheet_sku(product))
        tally_key = normalize_sku(item.get("model_number") or item.get("name"))
        if sheet_key == tally_key:
            continue
        if product in sheet_by_sku.get(sheet_key, []):
            sheet_by_sku[sheet_key].remove(product)
        if item in tally_by_sku.get(tally_key, []):
            tally_by_sku[tally_key].remove(item)
        sheet_by_sku.setdefault(sheet_key, []).append(product)
        tally_by_sku.setdefault(sheet_key, []).append(item)
    keys = [
        key for key in set(sheet_by_sku) | set(tally_by_sku)
        if sheet_by_sku.get(key) or tally_by_sku.get(key)
    ]
    return [_row(sheet_by_sku.get(key, []), tally_by_sku.get(key, []), key, tally_available) for key in sorted(keys)]


def _tally_online(integration: dict | None) -> bool:
    last_seen = (integration or {}).get("last_seen_at")
    if not isinstance(last_seen, datetime):
        return False
    seen = last_seen.replace(tzinfo=timezone.utc) if last_seen.tzinfo is None else last_seen
    now = datetime.now(timezone.utc)
    return now - timedelta(seconds=90) <= seen <= now + timedelta(minutes=5)


async def list_stock(
    org_id: str, *, page: int = 1, page_size: int = 50, query: str = "", source: str = "",
    status: str = "", low_stock: float | None = None, sort: str = "sku", direction: str = "asc",
) -> dict:
    rows = await _merged_stock(org_id)
    categories = sorted({row["category"] for row in rows}, key=str.casefold)
    needle = query.strip().casefold()
    if needle:
        rows = [row for row in rows if needle in " ".join((row["sku"], row["name"], row["description"])).casefold()]
    if source in {"sheet", "tally", "both"}:
        rows = [row for row in rows if row["source"] == source]
    if status:
        rows = [row for row in rows if row["status"] == status]
    if low_stock is not None:
        rows = [row for row in rows if row["quantity"] <= low_stock]
    sort_key = {"sku": "sku", "price": "price", "quantity": "quantity", "updated": "version"}.get(sort, "sku")
    rows.sort(key=lambda row: (row.get(sort_key) is None, row.get(sort_key)), reverse=direction == "desc")
    total = len(rows)
    start = (page - 1) * page_size
    integration = await db.tally_integrations.find_one({"org_id": org_id}, {"last_seen_at": 1, "last_sync_at": 1})
    pending = await db.stock_change_requests.count_documents({"org_id": org_id, "status": {"$nin": list(FINAL_STATES)}})
    return {
        "items": [_public(row) for row in rows[start:start + page_size]],
        "total": total, "page": page, "page_size": page_size,
        "categories": categories,
        "pending_changes": pending,
        "tally_connected": _tally_online(integration),
        "last_tally_sync_at": serialize_mongo((integration or {}).get("last_sync_at")),
    }


def _public(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


async def _find_stock(org_id: str, product_id: str) -> dict:
    for row in await _merged_stock(org_id):
        if row["id"] == product_id:
            return row
    raise HTTPException(status_code=404, detail="Stock product not found")


def _date_filter(from_date: str, to_date: str) -> dict:
    result = {}
    if from_date:
        result["$gte"] = from_date
    if to_date:
        result["$lte"] = to_date
    return result


async def _stock_analytics(org_id: str, row: dict, from_date: str = "", to_date: str = "") -> dict:
    key = row["normalized_sku"]
    match_keys = {key, normalize_sku(row.get("name")), normalize_sku((row.get("tally") or {}).get("name"))} - {""}
    event_match: dict = {
        "event_type": {"$in": ["buyer_price_request", "buyer_price_claim", "buyer_product_interest"]},
        "counterparty_role": "buyer",
    }
    if from_date or to_date:
        dates = {}
        if from_date:
            dates["$gte"] = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        if to_date:
            dates["$lte"] = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
        event_match["created_at"] = dates
    events = await db[f"pricing_events_{org_id}"].find(event_match, {
        "source_message_id": 1, "conversation_id": 1, "sku": 1, "product_context": 1,
    }).to_list(length=20_000)
    asked = len({
        event.get("source_message_id") or event.get("conversation_id")
        for event in events
        if normalize_sku(event.get("sku") or event.get("product_context")) in match_keys
    } - {None, ""})

    invoice_match: dict = {"org_id": org_id, "lifecycle_status": "active"}
    if from_date or to_date:
        invoice_match["date"] = _date_filter(from_date, to_date)
    transactions = await db.tally_invoices.find(invoice_match, {
        "items": 1, "transaction_type": 1, "party_ledger": 1, "date": 1, "voucher_number": 1,
    }).to_list(length=20_000)
    sold_quantity = sold_amount = 0.0
    for transaction in transactions:
        sign = -1 if transaction.get("transaction_type") == "credit_note" else 1
        for item in transaction.get("items") or []:
            if normalize_sku(item.get("model_number") or item.get("sku") or item.get("name")) in match_keys:
                sold_quantity += sign * _number(item.get("quantity"))
                sold_amount += sign * _number(item.get("amount"))
    return {"asked": asked, "sold_quantity": sold_quantity, "sold_amount": sold_amount}


async def _quote_history(org_id: str, row: dict) -> list[dict]:
    key = row["normalized_sku"]
    match_keys = {key, normalize_sku(row.get("name")), normalize_sku((row.get("tally") or {}).get("name"))} - {""}
    history = []
    quotes = await db[f"quotes_{org_id}"].find({}, {
        "quote_id": 1, "quote_number": 1, "customer": 1, "items": 1, "issued_on": 1,
        "updated_at": 1, "updated_by_name": 1,
    }).sort("updated_at", -1).to_list(length=2_000)
    for quote in quotes:
        for item in quote.get("items") or []:
            if normalize_sku(item.get("sku") or item.get("description")) not in match_keys:
                continue
            customer = quote.get("customer") or {}
            history.append({
                "source": "quotation", "reference": quote.get("quote_number") or quote.get("quote_id"),
                "customer_name": customer.get("name") or "Customer", "customer_phone": customer.get("phone") or "",
                "customer_gstin": customer.get("gstin") or "", "quantity": _number(item.get("quantity")),
                "price": _number(item.get("private_unit_price")), "gst_inclusive": bool(item.get("gst_inclusive")),
                "created_at": serialize_mongo(quote.get("updated_at") or quote.get("issued_on")),
                "agent_name": quote.get("updated_by_name") or "Team member",
            })
    offers = await db[f"sales_offers_{org_id}"].find({"line_items": {"$type": "array"}}, {
        "line_items": 1, "customer_name": 1, "customer_phone": 1, "created_at": 1,
        "created_by_name": 1, "source_message_id": 1,
    }).sort("created_at", -1).to_list(length=2_000)
    for offer in offers:
        for item in offer.get("line_items") or []:
            if normalize_sku(item.get("sku") or item.get("model") or item.get("description")) not in match_keys:
                continue
            history.append({
                "source": "chat_offer", "reference": offer.get("source_message_id"),
                "customer_name": offer.get("customer_name") or "Customer", "customer_phone": offer.get("customer_phone") or "",
                "customer_gstin": "", "quantity": _number(item.get("quantity"), 1),
                "price": _number(item.get("unit_price", item.get("price"))), "gst_inclusive": bool(item.get("gst_inclusive")),
                "created_at": serialize_mongo(offer.get("created_at")), "agent_name": offer.get("created_by_name") or "Team member",
            })
    return sorted(history, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:200]


async def get_stock(org_id: str, product_id: str, from_date: str = "", to_date: str = "") -> dict:
    row = await _find_stock(org_id, product_id)
    audits = await db.stock_change_audit.find(
        {"org_id": org_id, "product_id": product_id}, {"_id": 0}
    ).sort("created_at", -1).limit(200).to_list(length=200)
    return {
        "product": _public(row),
        "analytics": await _stock_analytics(org_id, row, from_date, to_date),
        "quote_history": await _quote_history(org_id, row),
        "audit": serialize_mongo(audits),
    }


async def _audit(change: dict, action: str, user_id: str, role: str, details: dict | None = None) -> None:
    await db.stock_change_audit.insert_one({
        "audit_id": str(uuid.uuid4()), "org_id": change["org_id"], "change_id": change["change_id"],
        "product_id": change["product_id"], "sku": change["sku"], "action": action,
        "status": change.get("status"), "actor_id": user_id, "actor_role": role,
        "details": details or {}, "created_at": datetime.now(timezone.utc),
    })


async def _notify_admins(change: dict) -> None:
    try:
        from api.transfer_routes import get_org_memberships, membership_public_data
        memberships = await get_org_memberships(change["org_id"])
        recipients = [
            membership_public_data(item).get("user_id") for item in memberships
            if item.get("role") in ADMIN_ROLES and membership_public_data(item).get("user_id") != change["requested_by"]
        ]
        now = datetime.now(timezone.utc)
        documents = [{
            "notification_id": str(uuid.uuid4()), "notification_type": "stock_change_request",
            "org_id": change["org_id"], "recipient_id": recipient, "conversation_id": "",
            "customer_name": change["sku"], "from_agent_id": change["requested_by"],
            "from_agent_name": change.get("requested_by_name") or "Team member",
            "reason": change.get("reason") or "Stock change approval requested",
            "referenced_message_ids": [], "stock_change_id": change["change_id"],
            "created_at": now, "read_at": None, "expires_at": now + timedelta(days=30),
        } for recipient in recipients if recipient]
        if documents:
            await db.transfer_notifications.insert_many(documents)
            for document in documents:
                await send_to_user(document["recipient_id"], {
                    "type": "stock_change_requested", **serialize_mongo(document),
                }, org_id=change["org_id"])
    except Exception:
        # Approval remains durable in the Stock workspace even if Clerk/live notification is unavailable.
        await broadcast_org_admin_ws(change["org_id"], "stock_change_requested", {
            "change_id": change["change_id"], "sku": change["sku"], "reason": change.get("reason"),
        })


async def _set_status(
    change: dict, status: str, actor_id: str, role: str, *, error: str | None = None,
    details: dict | None = None, expected_status: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    update = {"status": status, "updated_at": now, "error": error}
    match = {"org_id": change["org_id"], "change_id": change["change_id"]}
    if expected_status:
        match["status"] = expected_status
    saved = await db.stock_change_requests.find_one_and_update(
        match,
        {"$set": update}, return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if not saved:
        raise HTTPException(status_code=409, detail="This stock change was modified by another user")
    await _audit(saved, status, actor_id, role, details)
    await broadcast_org_ws(change["org_id"], "stock_change_status", {
        "change_id": change["change_id"], "product_id": change["product_id"], "sku": change["sku"],
        "status": status, "error": error,
    })
    if status == "completed":
        await broadcast_org_ws(change["org_id"], "stock_updated", {
            "change_id": change["change_id"], "product_id": change["product_id"], "sku": change["sku"],
        })
    return saved


async def create_stock_change(org_id: str, user: dict, product_id: str, version: str, changes: dict, reason: str) -> dict:
    row = await _find_stock(org_id, product_id)
    allocation_values = changes.pop("allocations", [])
    changes = {key: value for key, value in changes.items() if key in EDITABLE_FIELDS and value is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="Change at least one stock field")
    if row["conflicts"]:
        raise HTTPException(status_code=409, detail="Resolve duplicate SKU conflicts before editing this product")
    if version != row["version"]:
        raise HTTPException(status_code=409, detail="Stock changed since it was opened. Refresh and retry")
    if "price" in changes and not 0 <= float(changes["price"]) <= 1_000_000_000:
        raise HTTPException(status_code=422, detail="Price must be between 0 and 1,000,000,000")
    if "quantity" in changes and not 0 <= float(changes["quantity"]) <= 10_000_000:
        raise HTTPException(status_code=422, detail="Quantity must be between 0 and 10,000,000")
    if "gst_inclusive" in changes and not isinstance(changes["gst_inclusive"], bool):
        raise HTTPException(status_code=422, detail="GST inclusive must be true or false")
    if "gst_rate" in changes:
        changes["gst_rate"] = normalize_gst_rate(changes["gst_rate"], -1)
        if changes["gst_rate"] < 0:
            raise HTTPException(status_code=422, detail="GST rate must be between 0 and 100")
    now = datetime.now(timezone.utc)
    change = {
        "change_id": str(uuid.uuid4()), "org_id": org_id, "product_id": product_id, "sku": row["sku"],
        "source": row["source"], "base_version": version, "before": {
            "price": row["price"], "description": row["description"], "quantity": row["quantity"],
            "gst_inclusive": row["gst_inclusive"], "gst_rate": row["gst_rate"],
        }, "changes": changes, "allocations": allocation_values, "reason": reason.strip(),
        "status": "approved" if user["role"] in ADMIN_ROLES else "pending_approval",
        "requested_by": user["user_id"], "requested_by_name": user.get("full_name") or "Team member",
        "requested_by_role": user["role"], "created_at": now, "updated_at": now, "error": None,
    }
    await db.stock_change_requests.insert_one(change)
    change.pop("_id", None)
    await _audit(change, "requested", user["user_id"], user["role"], {"changes": changes})
    if user["role"] not in ADMIN_ROLES:
        await _notify_admins(change)
        return serialize_mongo(change)
    return await apply_stock_change(change, user["user_id"], user["role"])


async def list_stock_changes(org_id: str, status: str = "", product_id: str = "", limit: int = 100) -> list[dict]:
    match: dict = {"org_id": org_id}
    if status:
        match["status"] = status
    if product_id:
        match["product_id"] = product_id
    rows = await db.stock_change_requests.find(match, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(rows)


async def review_stock_change(org_id: str, change_id: str, user_id: str, role: str, approve: bool, reason: str = "") -> dict:
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only organization admins can review stock changes")
    change = await db.stock_change_requests.find_one({"org_id": org_id, "change_id": change_id}, {"_id": 0})
    if not change:
        raise HTTPException(status_code=404, detail="Stock change request not found")
    if change["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="This stock change has already been reviewed")
    if not approve:
        change = await _set_status(
            change, "rejected", user_id, role, error=reason.strip() or None,
            expected_status="pending_approval",
        )
        return serialize_mongo(change)
    change = await _set_status(change, "approved", user_id, role, expected_status="pending_approval")
    return await apply_stock_change(change, user_id, role)


async def _queue_tally_change(change: dict, row: dict, actor_id: str) -> dict:
    integration = await get_integration(change["org_id"])
    if not normalized_settings(integration)["company_name"].strip() or not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Configure and connect TallyPrime before changing Tally-backed stock")
    tally = row["_tally_document"]
    target = {**tally,
        "name": tally["name"], "model_number": tally.get("model_number") or change["sku"],
        "standard_rate": change["changes"].get("price", tally.get("standard_rate") or 0),
        "description": change["changes"].get("description", tally.get("description") or row["description"]),
    }
    requests = []
    if any(field in change["changes"] for field in ("price", "description")):
        requests.append({"key": "master", "xml": build_master_xml(
            "stock_item", target, action="Alter", current_name=tally["name"], settings=integration,
        )})
    if "quantity" in change["changes"]:
        requests.append({"key": "master", "xml": build_physical_stock_xml(
            integration, tally, float(change["changes"]["quantity"]), change.get("allocations") or [],
        )})
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()), "job_type": "stock_change", "org_id": change["org_id"],
        "stock_change_id": change["change_id"], "status": "pending", "requests": requests,
        "attempts": 0, "created_at": now, "updated_at": now, "created_by": actor_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return job


async def _apply_sheet(change: dict, row: dict) -> dict:
    product = row["_sheet_document"] or {
        "sku": row["sku"], "name": row["name"], "model_no": row["sku"],
        "description": row["description"], "price": row["price"], "stock": row["quantity"],
        "category": row["category"], "source_rows": {},
    }
    result = await write_product_sheet_values(change["org_id"], product, change["changes"])
    # The writer publishes the verified row through the same normalizer as sync,
    # including selling price, tax treatment and price provenance.
    return result


async def apply_stock_change(change: dict, actor_id: str, role: str) -> dict:
    row = await _find_stock(change["org_id"], change["product_id"])
    if change["base_version"] != row["version"]:
        return serialize_mongo(await _set_status(change, "needs_review", actor_id, role, error="Stock changed before this request was applied"))
    if row["_tally_document"] and {"price", "description", "quantity"}.intersection(change["changes"]):
        try:
            job = await _queue_tally_change(change, row, actor_id)
        except HTTPException as exc:
            if exc.status_code == 409:
                return serialize_mongo(await _set_status(change, "queued_offline", actor_id, role, error=str(exc.detail)))
            return serialize_mongo(await _set_status(change, "failed", actor_id, role, error=_safe_error("Tally update could not be queued", exc)))
        except Exception as exc:
            return serialize_mongo(await _set_status(change, "failed", actor_id, role, error=_safe_error("Tally update could not be queued", exc)))
        await db.stock_change_requests.update_one(
            {"org_id": change["org_id"], "change_id": change["change_id"]},
            {"$set": {"tally_job_id": job["job_id"], "updated_at": datetime.now(timezone.utc)}},
        )
        change["tally_job_id"] = job["job_id"]
        return serialize_mongo(await _set_status(
            change, "queued_tally", actor_id, role, details={"job_id": job["job_id"]},
        ))
    try:
        sheet = await _apply_sheet(change, row)
    except Exception as exc:
        return serialize_mongo(await _set_status(change, "failed", actor_id, role, error=_safe_error("Sheet update failed", exc)))
    return serialize_mongo(await _set_status(change, "completed", actor_id, role, details={"sheet": sheet}))


async def finish_tally_stock_change(
    org_id: str, tally_job_id: str, success: bool, error: str | None = None, *,
    after_readback: bool = False, delivery_unknown: bool = False,
) -> None:
    change = await db.stock_change_requests.find_one({"org_id": org_id, "tally_job_id": tally_job_id}, {"_id": 0})
    if not change:
        return
    if change.get("status") == "completed":
        return
    if delivery_unknown:
        await _set_status(
            change, "needs_review", "tally_connector", "system",
            error="Tally delivery is uncertain. Verify the stock in TallyPrime before retrying.",
        )
        return
    if not success:
        await _set_status(change, "failed", "tally_connector", "system", error=error or "Tally rejected the stock change")
        return
    if not after_readback:
        if change.get("status") == "verifying_tally" and change.get("readback_job_id"):
            return
        readback, _ = await queue_tally_sync(org_id, force=True, force_new=True, triggered_by_job_id=tally_job_id)
        await db.stock_change_requests.update_one(
            {"org_id": org_id, "change_id": change["change_id"]},
            {"$set": {"readback_job_id": readback["job_id"], "updated_at": datetime.now(timezone.utc)}},
        )
        change["readback_job_id"] = readback["job_id"]
        await _set_status(change, "verifying_tally", "tally_connector", "system", details={"readback_job_id": readback["job_id"]})
        return
    row = await _find_stock(org_id, change["product_id"])
    expected = change["changes"]
    mismatch = (
        ("quantity" in expected and abs(row["quantity"] - float(expected["quantity"])) > 0.001)
        or ("price" in expected and abs(_number((row.get("tally") or {}).get("price")) - float(expected["price"])) > 0.01)
        or ("description" in expected and str((row.get("tally") or {}).get("description") or "").strip() != str(expected["description"]).strip())
    )
    if mismatch:
        await _set_status(change, "needs_review", "tally_connector", "system", error="Tally read-back did not match the requested stock values")
        return
    if "quantity" in expected and row.get("tally"):
        allocations = change.get("allocations") or [{
            "godown": "Main Location", "batch": "Primary Batch", "quantity": float(expected["quantity"]),
        }]
        await db.stock_allocations.update_one(
            {"org_id": org_id, "tally_master_id": row["tally"]["master_id"]},
            {"$set": {
                "allocations": allocations, "total_quantity": float(expected["quantity"]),
                "tally_alter_id": row["tally"].get("alter_id"), "confirmed_at": datetime.now(timezone.utc),
                "source_change_id": change["change_id"],
            }}, upsert=True,
        )
    has_sheet = await db.product_sheet_connections.count_documents({"org_id": org_id, "status": "active"})
    if has_sheet:
        try:
            sheet = await _apply_sheet(change, row)
        except Exception as exc:
            await _set_status(change, "partially_applied", "tally_connector", "system", error=_safe_error("Tally updated, but Sheet failed", exc))
            return
    else:
        sheet = None
    await _set_status(change, "completed", "tally_connector", "system", details={"sheet": sheet})


async def fail_tally_stock_readback(org_id: str, tally_job_id: str, error: str) -> None:
    """Keep a successful Tally write reviewable when downstream verification breaks."""
    change = await db.stock_change_requests.find_one(
        {"org_id": org_id, "tally_job_id": tally_job_id}, {"_id": 0},
    )
    if change and change.get("status") not in FINAL_STATES:
        await _set_status(
            change, "needs_review", "tally_connector", "system",
            error=f"Tally updated, but HeidelAI could not verify the read-back: {error}"[:500],
        )


async def retry_stock_change(org_id: str, change_id: str, user_id: str, role: str) -> dict:
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only organization admins can retry stock changes")
    change = await db.stock_change_requests.find_one({"org_id": org_id, "change_id": change_id}, {"_id": 0})
    if not change:
        raise HTTPException(status_code=404, detail="Stock change request not found")
    if change["status"] not in {"failed", "partially_applied", "needs_review", "queued_offline"}:
        raise HTTPException(status_code=409, detail="This stock change is not retryable")
    row = await _find_stock(org_id, change["product_id"])
    previous_status = change["status"]
    change["base_version"] = row["version"]
    claimed = await db.stock_change_requests.update_one(
        {"org_id": org_id, "change_id": change_id, "status": change["status"]},
        {"$set": {"base_version": row["version"], "status": "approved", "error": None, "updated_at": datetime.now(timezone.utc)},
         "$unset": {"tally_job_id": "", "readback_job_id": ""}},
    )
    if claimed.modified_count != 1:
        raise HTTPException(status_code=409, detail="This stock change was modified by another user")
    change.update({"status": "approved", "error": None})
    if previous_status == "partially_applied":
        try:
            sheet = await _apply_sheet(change, row)
        except Exception as exc:
            return serialize_mongo(await _set_status(change, "partially_applied", user_id, role, error=_safe_error("Tally updated, but Sheet failed", exc)))
        return serialize_mongo(await _set_status(change, "completed", user_id, role, details={"sheet": sheet, "retry": "sheet_only"}))
    return await apply_stock_change(change, user_id, role)


async def sync_all_stock(org_id: str, user_id: str) -> dict:
    connections = await db.product_sheet_connections.find({"org_id": org_id, "status": "active"}, {"sheet_id": 1}).to_list(length=100)
    sheet_results = []
    for connection in connections:
        sheet_results.append(await sync_product_sheet(org_id, connection["sheet_id"]))
    tally_job = None
    try:
        tally_job, _ = await queue_tally_sync(org_id, user_id, force=True)
    except HTTPException:
        pass
    return {"sheets": sheet_results, "tally": tally_job}


async def bulk_update_stock_gst(
    org_id: str, user_id: str, role: str, *, scope: str, category: str,
    gst_inclusive: bool, gst_rate: float,
) -> dict:
    if role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only organization admins can apply GST settings in bulk")
    if not isinstance(gst_inclusive, bool):
        raise HTTPException(status_code=422, detail="GST inclusive must be true or false")
    gst_rate = normalize_gst_rate(gst_rate, -1)
    if gst_rate < 0:
        raise HTTPException(status_code=422, detail="GST rate must be between 0 and 100")
    rows = select_gst_rows(await _merged_stock(org_id), scope, category)
    if not rows:
        raise HTTPException(status_code=404, detail="No products match this GST scope")
    conflicts = [row for row in rows if row["conflicts"]]
    eligible = [row for row in rows if not row["conflicts"]]
    if not eligible:
        raise HTTPException(status_code=409, detail="Resolve duplicate SKU conflicts before applying GST settings")
    products = [row["_sheet_document"] or {
        "sku": row["sku"], "model_no": row["sku"], "name": row["name"],
        "description": row["description"], "price": row["price"], "quantity": row["quantity"],
        "category": row["category"], "source_rows": {},
    } for row in eligible]
    result = await write_product_sheet_gst_bulk(org_id, products, gst_inclusive, gst_rate)
    skipped_skus = {str(item["sku"]).casefold() for item in result["skipped"]}
    applied = [row for row in eligible if row["sku"].casefold() not in skipped_skus]
    now = datetime.now(timezone.utc)
    if applied:
        await db.stock_change_audit.insert_many([{
            "audit_id": str(uuid.uuid4()), "org_id": org_id, "change_id": f"bulk-gst:{uuid.uuid4()}",
            "product_id": row["id"], "sku": row["sku"], "action": "bulk_gst_updated",
            "status": "completed", "actor_id": user_id, "actor_role": role,
            "details": {"scope": scope, "category": category if scope == "category" else "", "gst_inclusive": gst_inclusive, "gst_rate": gst_rate},
            "created_at": now,
        } for row in applied])
    await broadcast_org_ws(org_id, "stock_updated", {
        "bulk": True, "scope": scope, "category": category if scope == "category" else "",
        "updated": len(applied), "gst_inclusive": gst_inclusive, "gst_rate": gst_rate,
    })
    return {
        **result, "matched": len(rows), "applied": len(applied),
        "skipped_conflicts": len(conflicts), "category": category if scope == "category" else None,
    }


async def link_stock_products(org_id: str, sheet_product_id: str, tally_master_id: str, user_id: str) -> dict:
    if not ObjectId.is_valid(sheet_product_id):
        raise HTTPException(status_code=422, detail="Choose a valid Google Sheet product")
    product = await db[f"products_{org_id}"].find_one(
        {"_id": ObjectId(sheet_product_id)}, {"_id": 1, "model_no": 1, "sku": 1, "product_name": 1},
    )
    item = await db.tally_stock_items.find_one({"org_id": org_id, "master_id": tally_master_id}, {"master_id": 1})
    if not product or not item:
        raise HTTPException(status_code=404, detail="Sheet product or Tally stock item not found")
    current = await db.stock_product_links.find_one({"org_id": org_id, "sheet_product_id": sheet_product_id}, {"_id": 0})
    collision = await db.stock_product_links.find_one({
        "org_id": org_id, "tally_master_id": tally_master_id, "sheet_product_id": {"$ne": sheet_product_id},
    })
    if collision:
        raise HTTPException(status_code=409, detail="This Tally item is already linked to another Sheet product")
    if current and current.get("tally_master_id") == tally_master_id:
        return {"status": "success", "sheet_product_id": sheet_product_id, "tally_master_id": tally_master_id}
    now = datetime.now(timezone.utc)
    await db.stock_product_links.update_one(
        {"org_id": org_id, "sheet_product_id": sheet_product_id},
        {"$set": {"tally_master_id": tally_master_id, "updated_at": now, "updated_by": user_id},
         "$setOnInsert": {"created_at": now}}, upsert=True,
    )
    await db.stock_change_audit.insert_one({
        "audit_id": str(uuid.uuid4()), "org_id": org_id, "change_id": f"link:{uuid.uuid4()}",
        "product_id": sheet_product_id, "sku": _sheet_sku(product), "action": "linked_tally_source",
        "status": "completed", "actor_id": user_id, "actor_role": "admin",
        "details": {"previous_tally_master_id": (current or {}).get("tally_master_id"), "tally_master_id": tally_master_id},
        "created_at": now,
    })
    return {"status": "success", "sheet_product_id": sheet_product_id, "tally_master_id": tally_master_id}
