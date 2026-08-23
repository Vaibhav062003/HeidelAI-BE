"""Pure rules for sales-offer tracking."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
from typing import Optional


OFFER_STATUSES = {"open", "accepted", "billed", "paid", "lost", "expired", "superseded"}
DELIVERY_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
_TOTAL_RE = re.compile(
    r"\b(?:grand\s+total|total|all\s+inclusive)\b[^\d₹]{0,20}(?:₹|rs\.?|inr)?\s*"
    r"(?P<amount>\d{1,3}(?:[,]\d{2,3})+|\d+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
_CURRENCY_PRICE_RE = re.compile(
    r"(?:\bprice\b\s*(?::|=|is|of|at)?\s*)?(?:₹|rs\.?|inr)\s*"
    r"(?P<amount>\d{1,3}(?:[,]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale>k|thousand|lakh|lac)?",
    re.IGNORECASE,
)
_BARE_PRICE_RE = re.compile(
    r"\b(?:price|rate|quote|offer|total|can\s+do)\b\s*(?::|=|is)?\s*"
    r"(?P<amount>\d{1,3}(?:[,]\d{2,3})+|\d{5,7})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PRODUCT_CONTEXT_RE = re.compile(
    r"\b(?:rtx|gtx|ryzen|intel|gpu|cpu|ram|ssd|nvme|motherboard|cabinet|psu|cooler|laptop|pc|build|monitor|windows)\b|"
    r"\b(?=[A-Za-z0-9-]*[A-Za-z])(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]+\b",
    re.IGNORECASE,
)
_EXPLICIT_OFFER_RE = re.compile(
    r"\b(?:our|final|best|offered?)\s+price\b|\bprice\s+(?:is|will\s+be)\b|\bgrand\s+total\b|"
    r"\b(?:we\s+can|can\s+give|available)\s+(?:do\s+it\s+)?(?:at|for)\b|"
    r"\b(?:i\s+|we\s+)?can\s+do\s+(?:it\s+)?(?:at\s+|for\s+)?(?:₹|rs\.?|inr|\d)",
    re.IGNORECASE,
)
_NON_OFFER_AMOUNT_RE = re.compile(
    r"\b(?:discount|savings?|advance|shipping|delivery(?:\s+(?:charge|fee))?|freight)\b",
    re.IGNORECASE,
)


def is_admin(role: str) -> bool:
    return role in {"admin", "org:admin"}


def structured_offer_snapshot(context: Optional[dict]) -> Optional[dict]:
    """Validate the explicit offer payload produced by the Quote composer."""
    offer = context.get("offer") if isinstance(context, dict) else None
    if not isinstance(offer, dict):
        return None
    raw_items = offer.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 50:
        raise ValueError("A chat offer must contain between 1 and 50 products")

    items = []
    grand_total = Decimal("0")
    tax_modes = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Every chat-offer item must be an object")
        description = str(raw.get("description") or "").strip()
        if not description or len(description) > 240:
            raise ValueError("Every chat-offer item needs a product name")
        try:
            quantity = Decimal(str(raw.get("quantity", 1)))
            unit_price = Decimal(str(raw.get("unit_price")))
            gst_rate = Decimal(str(raw.get("gst_rate", 18)))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Chat-offer quantities, prices and GST must be numbers") from exc
        if not all(isfinite(float(value)) for value in (quantity, unit_price, gst_rate)):
            raise ValueError("Chat-offer numbers must be finite")
        if quantity <= 0 or quantity > 1000 or unit_price <= 0 or unit_price > 999_999_999 or gst_rate < 0 or gst_rate > 100:
            raise ValueError("Chat-offer values are outside the supported range")

        gst_inclusive = raw.get("gst_inclusive") is True
        entered_total = quantity * unit_price
        line_total = entered_total if gst_inclusive else entered_total * (Decimal("1") + gst_rate / Decimal("100"))
        line_total = line_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        grand_total += line_total
        tax_modes.add("included" if gst_inclusive else "extra")
        items.append({
            "description": description,
            "quantity": float(quantity),
            "unit_price": float(unit_price),
            "gst_rate": float(gst_rate),
            "gst_inclusive": gst_inclusive,
            "line_total": float(line_total),
        })

    return {
        "items": items,
        "amounts": [item["line_total"] for item in items],
        "total_amount": float(grand_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "tax_basis": next(iter(tax_modes)) if len(tax_modes) == 1 else "mixed",
    }


def report_query(
    *, org_id: str, user_id: str, role: str, start: Optional[datetime], end: Optional[datetime],
    source: Optional[str], status: Optional[str], agent_id: Optional[str],
) -> dict:
    query: dict = {"org_id": org_id, "delivery_status": {"$ne": "failed"}}
    if not is_admin(role):
        query["agent_id"] = user_id
    elif agent_id:
        query["agent_id"] = agent_id
    if source in {"formal_quote", "chat_price"}:
        query["source_type"] = source
    if status in OFFER_STATUSES:
        query["status"] = status
    if start or end:
        query["created_at"] = {}
        if start:
            query["created_at"]["$gte"] = start
        if end:
            query["created_at"]["$lt"] = end
    return query


def tax_basis(text: str) -> str:
    value = text or ""
    if re.search(
        r"\b(?:not\s+gst\s+included|gst\s+(?:is\s+)?not\s+(?:included|inclusive)|not\s+(?:included|inclusive)(?:\s+of)?\s+gst)\b",
        value,
        re.I,
    ):
        return "extra"
    if re.search(r"\b(?:gst\s+(?:is\s+)?(?:extra|excluded|exclusive)|(?:extra|excluding|exclusive|excl\.?)\s+(?:\d+(?:\.\d+)?\s*%\s*)?gst)\b", value, re.I):
        return "extra"
    if re.search(r"\b(?:gst\s*)?(?:included|inclusive)|\bincl\.?\s*gst\b", value, re.I):
        return "included"
    if re.search(r"(?:\+|extra|excluding|exclusive|excl\.?)\s*(?:\d+(?:\.\d+)?\s*%\s*)?gst", value, re.I):
        return "extra"
    return "unspecified"


def extract_quoted_prices(text: str) -> list[float]:
    values: list[float] = []
    for match in _CURRENCY_PRICE_RE.finditer(text or ""):
        before = re.split(r"[;,.\n]", (text or "")[max(0, match.start() - 60):match.start()])[-1]
        after = re.split(r"[;,.\n]", (text or "")[match.end():match.end() + 60])[0]
        explicit_before = _EXPLICIT_OFFER_RE.search(f"{before} {match.group()}") or re.search(r"\b(?:grand\s+total|total)\s*$", before, re.I)
        if (_NON_OFFER_AMOUNT_RE.search(before) or _NON_OFFER_AMOUNT_RE.search(after)) and not explicit_before:
            continue
        value = float(match.group("amount").replace(",", ""))
        scale = (match.group("scale") or "").lower()
        value *= 1_000 if scale in {"k", "thousand"} else 100_000 if scale in {"lakh", "lac"} else 1
        if 0 < value <= 9_999_999:
            values.append(value)
    for match in _BARE_PRICE_RE.finditer(text or ""):
        value = float(match.group("amount").replace(",", ""))
        if value not in values:
            values.append(value)
    return values


def offer_prices(text: str, extracted: list[float]) -> list[float]:
    if extracted:
        return extracted
    value = (text or "").strip()
    match = re.search(
        r"(?<!\d)(\d{5,7})(?P<suffix>\s*(?:(?:\+\s*)?(?:\d+(?:\.\d+)?\s*%\s*)?gst(?:\s+(?:extra|included|inclusive))?|all\s+inclusive))?\s*$",
        value,
        re.I,
    )
    has_suffix = bool(match and match.group("suffix"))
    if not match:
        return []
    prefix = value[:match.start()]
    if len(value) != len(match.group(1)) and not has_suffix and not _PRODUCT_CONTEXT_RE.search(prefix):
        return []
    return [float(match.group(1))]


def is_outbound_offer(text: str, prices: list[float]) -> bool:
    if not prices:
        return False
    value = (text or "").strip()
    explicit = _EXPLICIT_OFFER_RE.search(value)
    if re.search(r"\b(?:pin(?:code)?|postal|zip|address|located|location|call|phone|mobile|contact|order|invoice)\b", value, re.I) and not explicit:
        return False
    if re.search(r"\b(?:your\s+budget|you\s+paid|payment\s+(?:of|received)|refund(?:ed)?|amount\s+received)\b", value, re.I):
        return bool(explicit)
    if ("?" in value or re.match(r"\s*(?:can|could|would|will|do|did|is|are|what|how)\b", value, re.I)) and not explicit:
        return False
    return True


def explicit_total(text: str, prices: list[float]) -> Optional[float]:
    if len(prices) == 1:
        return float(prices[0])
    match = _TOTAL_RE.search(text or "")
    if not match:
        return None
    value = float(match.group("amount").replace(",", ""))
    return value if value in prices else None


def next_delivery_status(current: Optional[str], incoming: str) -> str:
    if current == "failed" or incoming not in DELIVERY_RANK:
        return current or incoming
    if incoming == "failed":
        return "failed"
    return incoming if DELIVERY_RANK[incoming] >= DELIVERY_RANK.get(current or "", 0) else str(current)


def quote_revision_snapshot(quote: dict, requested_revision: object) -> Optional[dict]:
    try:
        revision = int(requested_revision)
    except (TypeError, ValueError):
        return None
    if revision == int(quote.get("revision_number") or 1):
        return quote
    return next(
        (item for item in quote.get("revisions") or [] if int(item.get("revision_number") or 0) == revision),
        None,
    )
