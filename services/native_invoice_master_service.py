"""Read-only invoice presets from the existing contacts and product catalogue."""

import base64
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_async_mongo_db
from services.catalogue_fields import product_id
from services.india_tax import GST_STATE_NAMES, is_valid_gstin


db = get_async_mongo_db()


def _cursor_offset(value: str) -> int:
    if not value:
        return 0
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        offset = int(decoded["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid preset cursor") from exc


def _next_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}, separators=(",", ":")).encode()).decode().rstrip("=")


def _text(value, maximum: int = 500) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if item)
    if isinstance(value, dict):
        value = ", ".join(str(item) for item in value.values() if item)
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:maximum]


def _pincode(value) -> str:
    digits = re.sub(r"\D", "", _text(value, 20))
    return digits if len(digits) == 6 else ""


async def customer_presets(org_id: str, search: str = "", limit: int = 50, cursor: str = "") -> dict:
    query = {}
    if search.strip():
        pattern = {"$regex": re.escape(search.strip()), "$options": "i"}
        query = {"$or": [{field: pattern} for field in ("full_name", "customer_name", "company", "email", "phone_number", "gstin", "tally.legal_name")]}
    offset = _cursor_offset(cursor)
    rows = await db[f"contacts_{org_id}"].find(query).sort([("full_name", 1), ("_id", 1)]).skip(offset).limit(limit + 1).to_list(length=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    result = []
    seen = set()
    for row in rows:
        tally = row.get("tally") or {}
        name = _text(row.get("full_name") or row.get("customer_name") or tally.get("legal_name") or row.get("company"), 120)
        if not name:
            continue
        gstin = _text(row.get("gstin") or tally.get("gstin"), 15).upper()
        gstin = gstin if is_valid_gstin(gstin) else ""
        state_code = _text(row.get("state_code") or tally.get("state_code"), 2)
        if state_code not in GST_STATE_NAMES:
            state_code = gstin[:2] if gstin else ""
        key = (gstin, name.casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "name": name,
            "legal_name": _text(tally.get("legal_name") or row.get("legal_name"), 120),
            "trade_name": _text(tally.get("trade_name") or row.get("company"), 120),
            "gstin": gstin,
            "address": _text(row.get("address") or tally.get("address")),
            "location": _text(row.get("city") or row.get("location") or tally.get("location"), 120),
            "pincode": _pincode(row.get("pincode") or tally.get("pincode")),
            "state_code": state_code,
            "phone": _text(row.get("phone_number") or row.get("phone"), 40),
            "email": _text(row.get("email"), 100),
            "payment_details": "",
            "country_code": "IN",
        })
    return {"customers": result, "next_cursor": _next_cursor(offset + limit) if has_more else None}


def _paise(product: dict) -> int:
    value = next((product.get(key) for key in ("selling_price", "price", "mrp", "target_unit_price") if product.get(key) is not None), 0)
    try:
        return max(0, min(999_999_999_00, int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))))
    except (InvalidOperation, TypeError, ValueError):
        return 0


async def item_presets(org_id: str, search: str = "", limit: int = 50, cursor: str = "") -> dict:
    organization = await db.organizations.find_one({"org_id": org_id}, {"sales_catalogue_org_id": 1}) or {}
    configured = str(organization.get("sales_catalogue_org_id") or "")
    catalogue_org = configured if re.fullmatch(r"[A-Za-z0-9_-]+", configured) else org_id
    query = {}
    if search.strip():
        pattern = {"$regex": re.escape(search.strip()), "$options": "i"}
        query = {"$or": [{field: pattern} for field in ("product_name", "model_name", "description", "model_no", "unique_identifier", "hsn_sac", "hsn", "sac")]}
    offset = _cursor_offset(cursor)
    rows = await db[f"products_{catalogue_org}"].find(query).sort([("product_name", 1), ("_id", 1)]).skip(offset).limit(limit + 1).to_list(length=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    result = []
    for row in rows:
        description = _text(row.get("product_name") or row.get("model_name") or row.get("description"))
        if not description:
            continue
        hsn = re.sub(r"\D", "", _text(row.get("hsn_sac") or row.get("hsn") or row.get("sac"), 12))
        supply_type = "services" if hsn.startswith("99") and len(hsn) == 6 else "goods"
        try:
            gst_rate_bps = round(float(row.get("gst_rate") or 0) * 100)
        except (TypeError, ValueError):
            gst_rate_bps = 0
        result.append({
            "id": product_id(row),
            "description": description,
            "hsn_sac": hsn,
            "quantity": "1",
            "uqc": _text(row.get("tally_unit") or row.get("uqc") or "NOS", 10),
            "unit_price_paise": _paise(row),
            "gst_rate_bps": max(0, min(10_000, gst_rate_bps)),
            "gst_inclusive": row.get("gst_inclusive") is True,
            "supply_type": supply_type,
            "line_kind": "item",
        })
    return {"items": result, "next_cursor": _next_cursor(offset + limit) if has_more else None}
