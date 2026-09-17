"""Small, deterministic safety rules for extracted enquiries and restock outreach."""
import hashlib
import math
import re
from datetime import datetime, timedelta, timezone

RESTOCK_TEMPLATE = "heidelai_product_back_in_stock_v1"
RESTOCK_BODY = (
    "Good news! {{1}} is back in stock. You asked us to let you know when it was available. "
    "Reply here for current pricing and availability. Reply STOP to opt out."
)
RESTOCK_FOOTER = "Powered by Heidelai.com - AI Sales Agent"


def request_key(org_id, conversation_id, product):
    # Keep variant punctuation/digits: RTX-4060 and RTX-4060 Ti must not collapse.
    name = " ".join(str(product).casefold().split())
    return hashlib.sha256(f"{org_id}\0{conversation_id}\0{name}".encode()).hexdigest()


def product_name(value):
    """Reject obvious prose; semantic specificity is supplied by the extractor."""
    name = " ".join(str(value or "").split()).strip()
    if not name or len(name) > 240 or re.search(r"[?？\n\r]", str(value)):
        return None
    if re.search(r"\b(?:setup|pc build|gaming pc build)\b", name, re.I):
        return None
    if re.match(r"(?:what(?:['’]?s)?\b|do you\b|can (?:you|i)\b|how\b|is (?:it|this|the)\b|i (?:want|need|will)\b|please\b|send me\b|looking for\b|mujhe\b|kya\b)", name, re.I):
        return None
    # ponytail: only obvious category/spec strings; model classification handles other languages/categories.
    if re.fullmatch(r"(?:(?:ddr[345]|\d+(?:gb|tb|mhz)|ram|ssd|hdd|nvme|gpu|cpu|gaming|pc|laptop|phone|with|and)\s*)+", name, re.I):
        return None
    return name


def grounded_request(raw, messages):
    """LLM output is untrusted. A customer message and exact quote are mandatory."""
    product = product_name(raw.get("product_name"))
    evidence = str(raw.get("evidence") or "").strip()
    source_id = str(raw.get("source_message_id") or "")
    if not product or not evidence or len(evidence) > 2000 or raw.get("specificity") != "specific_product":
        return None
    source = next((m for m in messages if str(m.get("message_id") or m.get("_id")) == source_id), None)
    if not source or source.get("role") != "customer" or source.get("mode") == "private":
        return None
    if source not in messages[-10:]:
        return None
    if evidence not in str(source.get("content") or ""):
        return None
    if raw.get("confidence") not in {"high", "medium"}:
        return None
    reference_id = str(raw.get("product_reference_message_id") or "")
    reference = next((m for m in messages if str(m.get("message_id") or m.get("_id")) == reference_id), None)
    mention = str(raw.get("product_mention") or "").strip()
    if (not reference or reference.get("mode") == "private"
            or reference.get("role") not in {"customer", "agent", "ai"}
            or messages.index(reference) > messages.index(source)
            or not mention or len(mention) > 240 or mention not in str(reference.get("content") or "")):
        return None
    if product.casefold() != " ".join(mention.casefold().split()) or not product_name(mention):
        return None
    return {"product_name": product, "evidence": evidence, "source_message_id": source_id,
            "product_reference_message_id": reference_id, "product_mention": mention,
            "specificity": "specific_product",
            "confidence": raw["confidence"], "source_at": source.get("timestamp"),
            "withdrawn": raw.get("withdrawn") is True,
            "notification_requested": raw.get("notification_requested") is True}


def available_stock(product):
    if (not product or product.get("is_deleted") or product.get("deleted_at")
            or product.get("stock_resolution_error") or product.get("source_valid") is False):
        return False
    # Unknown stock never counts as available; zero must not fall through to another field.
    for field in ("stock", "quantity", "available_qty"):
        if product.get(field) is not None:
            try:
                value = float(product[field])
                return math.isfinite(value) and value > 0
            except (ValueError, TypeError):
                return False
    return product.get("in_stock") is True


def eligibility_reason(row, product, now=None):
    now = now or datetime.now(timezone.utc)
    if row.get("archived_at"):
        return "Product request was archived"
    if row.get("status") != "requested":
        return "Interest is no longer active"
    if not row.get("product_id") or not row.get("reviewed_by"):
        return "Catalogue match needs review"
    if not row.get("consent_note") or not row.get("consent_by"):
        return "Restock notification permission has not been confirmed"
    asked = row.get("last_asked_at")
    if not isinstance(asked, datetime) or as_utc(asked) < now - timedelta(days=90):
        return "Request is older than 90 days"
    if not re.fullmatch(r"\d{7,15}", str(row.get("customer_phone") or "")):
        return "Missing valid WhatsApp customer number"
    if not row.get("business_phone_id"):
        return "Original WhatsApp line is unknown"
    if row.get("notification_status"):
        return "Already queued or contacted; check notification status"
    if not available_stock(product):
        return "Product is not confirmed in stock"
    updated = product.get("updated_at")
    if (not isinstance(updated, datetime) or as_utc(updated) < now - timedelta(hours=24)
            or as_utc(updated) > now + timedelta(minutes=5)):
        return "Stock information is stale or unverified; sync stock within the last 24 hours"
    return ""


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def approved_restock_template(templates):
    for template in templates:
        if (template.get("name") != RESTOCK_TEMPLATE or template.get("language") != "en_US"
                or template.get("status") != "APPROVED" or template.get("category") != "MARKETING"):
            continue
        parts = template.get("components") or []
        if (len(parts) == 2 and
                any(p.get("type") == "BODY" and p.get("text") == RESTOCK_BODY for p in parts) and
                any(p.get("type") == "FOOTER" and p.get("text") == RESTOCK_FOOTER for p in parts)):
            return True
    return False
