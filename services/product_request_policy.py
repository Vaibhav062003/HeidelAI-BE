"""Small, deterministic safety rules for extracted enquiries and restock outreach."""
import hashlib
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

RESTOCK_TEMPLATE = "heidelai_product_back_in_stock_v1"
RESTOCK_BODY = (
    "Good news! {{1}} is back in stock. You asked us to let you know when it was available. "
    "Reply here for current pricing and availability. Reply STOP to opt out."
)
RESTOCK_FOOTER = "Powered by Heidelai.com - AI Sales Agent"

# These are review signals, never a blacklist of legitimate product titles.
_CONVERSATIONAL_PRODUCT = re.compile(
    r"[?!？！।]|"
    r"^(?:hello|hi|hey|thanks|thank you|please|what|how|when|where|why|"
    r"do|does|did|can|could|would|will|is|are|looking for|price for|cost of)\b(?!-)|"
    r"\b(?:do you|have you got|i want|i need|i am|i will|i have|i already|i would|"
    r"i['’]d like|i['’]ll|i['’]m|need a|want a|"
    r"we want|we need|send me|tell me|give me|let me|no longer|notify me)\b|"
    r"\b(?:ka|ki|ke)\s+(?:price|rate|cost|daam)\b|"
    r"\b(?:available(?:\s+hai)?|in stock|price|cost|details|chahiye|karna|kya hai|hai kya|mil sakta hai)[\s.,]*$|"
    r"^(?:mujhe|humein|kya|aapke paas|क्या|मुझे|हमें|आपके पास|ये|यह|वो)\s|"
    r"(?:चाहिए|मिल जाएगा|उपलब्ध है)\s*$",
    re.IGNORECASE,
)
_SEMANTIC_RISK = re.compile(
    r"(?<!\w)(?:no|not|never|don't|dont|cancel|instead|example|if|hypothetical|"
    r"already|bought|works|fine|yesterday|said|example|ignore|instructions|my|his|her|their|"
    r"nahi|nahin|मत|नहीं|उदाहरण)(?!\w)", re.IGNORECASE,
)
_ENQUIRY = re.compile(r"(?<!\w)(?:price|cost|stock|available|availability|specs|details|need|want|buy|book|"
                      r"chahiye|कीमत|दाम|चाहिए|उपलब्ध)(?!\w)", re.IGNORECASE)


def normalized_name(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def verification_status(row):
    return row.get("verification_status") or ("confirmed" if row.get("reviewed_by") else "needs_review")


def revision_match(row):
    # Legacy rows have no revision; Mongo equality with None matches missing fields.
    return {"revision": row.get("revision")}


def request_key(org_id, conversation_id, product):
    # Keep variant punctuation/digits: RTX-4060 and RTX-4060 Ti must not collapse.
    name = " ".join(str(product).casefold().split())
    return hashlib.sha256(f"{org_id}\0{conversation_id}\0{name}".encode()).hexdigest()


def grounded_request(raw, messages, context_messages=()):
    """Validate provenance independently of semantic classification and catalogue matching."""
    if not isinstance(raw, dict):
        return None
    product = " ".join(str(raw.get("product_name") or "").split())
    evidence = str(raw.get("evidence") or "").strip()
    source_id = str(raw.get("source_message_id") or "")
    if len(product) > 240 or not evidence or len(evidence) > 2000:
        return None
    source = next((m for m in messages if str(m.get("message_id") or m.get("_id")) == source_id), None)
    if not source or source.get("role") != "customer" or source.get("mode") == "private":
        return None
    if evidence not in str(source.get("content") or ""):
        return None
    if raw.get("confidence") not in {"high", "medium", "low"}:
        return None
    action = raw.get("action") or ("withdrawal" if raw.get("withdrawn") is True else "enquiry")
    kind = raw.get("entity_type") or ("product" if product else "unresolved")
    notification = raw.get("notification_action") or ("request" if raw.get("notification_requested") is True else "none")
    if action not in {"enquiry", "withdrawal", "fulfilled", "mention_only"} or kind not in {"product", "category", "unresolved"} or notification not in {"none", "request", "revoke"}:
        return None
    if not product and kind != "unresolved":
        return None
    product_source_id = str(raw.get("product_source_message_id") or source_id)
    product_source = source
    anchor_product_id = None
    mention = re.compile(r"(?<!\w)" + re.escape(normalized_name(product)) + r"(?!\w)")
    if product_source_id == source_id:
        if product and not mention.search(normalized_name(evidence)):
            return None
    else:
        history = sorted([*context_messages, *messages], key=lambda m: as_utc(m["timestamp"]) if isinstance(m.get("timestamp"), datetime) else datetime.min.replace(tzinfo=timezone.utc))
        source_position = next(i for i, message in enumerate(history) if message is source)
        product_source = next((m for m in history[:source_position]
                               if str(m.get("message_id") or m.get("_id")) == product_source_id), None)
        if (not product_source or product_source.get("mode") == "private"
                or not isinstance(product_source.get("timestamp"), datetime)
                or not isinstance(source.get("timestamp"), datetime)
                or as_utc(product_source["timestamp"]) > as_utc(source["timestamp"])):
            return None
        if product_source.get("role") != "customer":
            reply = source.get("context") or {}
            anchor_product_id = (product_source.get("metadata") or {}).get("product_id")
            if product_source.get("role") != "agent" or str(reply.get("id") or reply.get("message_id") or "") != product_source_id or not anchor_product_id:
                return None
        if product and not mention.search(normalized_name(product_source.get("content"))):
            return None
    requirements = []
    if not isinstance(raw.get("requirements", []), list):
        return None
    for requirement in raw.get("requirements") or []:
        if not isinstance(requirement, dict):
            return None
        quote = str(requirement.get("quote") or "")
        value = str(requirement.get("value") or "")
        requirement_source = str(requirement.get("source_message_id") or source_id)
        owner = source if requirement_source == source_id else product_source if requirement_source == product_source_id else None
        if not owner or not quote or quote not in str(owner.get("content") or "") or normalized_name(value) != normalized_name(quote):
            return None
        requirements.append({"name": str(requirement.get("name") or "other")[:40], "value": value,
                             "quote": quote, "source_message_id": requirement_source})
    if len(requirements) > 20:
        return None
    full_source = str(source.get("content") or "")
    return {"product_name": product or None, "evidence": full_source[:2000], "evidence_truncated": len(full_source) > 2000,
            "source_message_id": source_id, "source_text": full_source, "action": action,
            "entity_type": kind, "requirements": requirements, "notification_action": notification,
            "anchor_product_id": str(anchor_product_id) if anchor_product_id else None,
            "product_source_message_id": product_source_id,
            "confidence": raw["confidence"], "source_at": source.get("timestamp"),
            "withdrawn": action == "withdrawal", "notification_requested": notification == "request"}


def product_identity(item, catalogue_id=None, catalogue_org=None):
    if catalogue_id:
        return f"catalogue:{catalogue_org}:{catalogue_id}"
    qualifiers = sorted({normalized_name(r["value"]) for r in item["requirements"] if r["name"] not in {"quantity", "budget"}})
    return "text:" + normalized_name(item["product_name"]) + "\0" + "\0".join(qualifiers)


def needs_verification(item, catalogue_id=None):
    text = item["source_text"].strip()
    direct_enquiry = bool(re.match(r"^(?:(?:i|we)\s+(?:need|want|would like)|need\b|want\b|price (?:for|of)\b)", text, re.I)
                          or re.search(r"[?？]", text) and _ENQUIRY.search(text))
    return bool(not catalogue_id or item["entity_type"] != "product" or item["confidence"] != "high"
                or item["product_source_message_id"] != item["source_message_id"]
                or item["action"] != "enquiry" or item["notification_action"] == "revoke"
                or _CONVERSATIONAL_PRODUCT.search(item["product_name"] or "")
                or _SEMANTIC_RISK.search(text) or not direct_enquiry)


def verified_decision(item, verdict):
    verdict = verdict if isinstance(verdict, dict) else {}
    consistent = (normalized_name(verdict.get("product_name")) == normalized_name(item["product_name"])
                  and all(verdict.get(k) == item[k] for k in ("entity_type", "action", "notification_action")))
    status = verdict.get("verification_status")
    return status if status == "rejected" or status == "confirmed" and consistent else "needs_review"


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
    if verification_status(row) != "confirmed" or row.get("review_hold"):
        return "Product request needs confirmation"
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
