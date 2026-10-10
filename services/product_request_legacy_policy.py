"""Frozen safeguards for reviewed version-2 historical cleanup reports only."""
import re


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
