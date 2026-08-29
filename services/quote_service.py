"""Shared quote totals and storage shape for UI and AI-created quotes."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from services.quote_math import quote_line_amounts


def build_quote_document(
    payload: dict[str, Any], *, org_id: str, actor_id: str, actor_name: str,
) -> dict[str, Any]:
    data = deepcopy(payload)
    stored_items = []
    subtotal = 0.0
    gst_total = 0.0
    for item in data["items"]:
        unit_price = float(item.pop("unit_price"))
        base, gst, line_total = quote_line_amounts(
            float(item["quantity"]), unit_price, float(item.get("gst_rate") or 0),
            bool(item.get("gst_inclusive")),
        )
        subtotal += base
        gst_total += gst
        stored_items.append({
            **item,
            "private_unit_price": round(unit_price, 2),
            "private_line_subtotal": base,
            "public_line_total": line_total,
        })
    subtotal = round(subtotal, 2)
    gst_total = round(gst_total, 2)
    shipping = float(data.get("shipping") or 0)
    discount = min(float(data.get("discount") or 0), subtotal + gst_total + shipping)
    return {
        **data, "org_id": org_id, "items": stored_items,
        "private_subtotal": subtotal, "private_gst_total": gst_total,
        "discount": round(discount, 2),
        "public_grand_total": round(subtotal + gst_total + shipping - discount, 2),
        "updated_at": datetime.now(timezone.utc),
        "updated_by": actor_id, "updated_by_name": actor_name,
    }
