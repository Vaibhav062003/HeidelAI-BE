"""Shared quote totals and storage shape for UI and AI-created quotes."""

import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from services.quote_math import quote_line_amounts

DEFAULT_QUOTE_FOOTER = "Powered by Heidelai.com | AI Sales Agent"
FOOTER_EDIT_ORGS = {
    value.strip() for value in os.getenv("FOOTER_EDIT_ORGS", "").split(",") if value.strip()
}


def can_edit_quote_footer(org_id: str) -> bool:
    return org_id in FOOTER_EDIT_ORGS


def quote_footer(org_id: str, value: object = None) -> str:
    custom = str(value or "").strip()
    return custom if custom and can_edit_quote_footer(org_id) else DEFAULT_QUOTE_FOOTER


def build_quote_document(
    payload: dict[str, Any], *, org_id: str, actor_id: str, actor_name: str,
) -> dict[str, Any]:
    data = deepcopy(payload)
    branding = data.get("branding") or {}
    branding["footer_text"] = quote_footer(org_id, branding.get("footer_text"))
    data["branding"] = branding
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
