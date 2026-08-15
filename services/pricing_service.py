"""Deterministic pricing guardrails for dealer WhatsApp conversations."""

import hashlib
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from config.settings import (
    KOMAL_ORG_ID,
    LEE_DEALER_ORG_ID,
    PRICING_FLOOR_MAX_AGE_MINUTES,
    PRICING_INTELLIGENCE_ORG_IDS,
)
from database import get_async_mongo_db
from services.utils import normalize_phone


HOURS_24 = timedelta(hours=24)
FLOOR_MAX_AGE = timedelta(minutes=PRICING_FLOOR_MAX_AGE_MINUTES)
EVENT_RETENTION = timedelta(days=30)
HOLDING_REPLY = "Our sales team is reviewing this price and will get back to you shortly."

_PRICE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<currency>₹|rs\.?|inr)?\s*"
    r"(?P<amount>\d{1,3}(?:[,\s]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale>k|thousand|lakh|lac)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_BARE_QUOTE_RE = re.compile(
    r"\b(?:our\s+price|price|rate|quote|offer|can\s+do|"
    r"(?:you\s+can\s+)?have\s+it\s+at|give\s+it\s+at|final\s+price|only)\b"
    r"\s*(?::|=|is|of|at|for|only)?\s*"
    r"(?P<amount>\d{1,3}(?:[ ,]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale>k|thousand|lakh|lac)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_INCLUSIVE_QUOTE_RE = re.compile(
    r"(?P<amount>\d{1,3}(?:[ ,]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale>k|thousand|lakh|lac)?\s*"
    r"(?:all\s+inclusive|incl(?:usive|uding)?\s+(?:of\s+)?gst|gst\s+included)",
    re.IGNORECASE,
)
_QUOTE_RANGE_RE = re.compile(
    r"\b(?:price|quote|offer|range|between|from)\b[^\d₹]{0,16}"
    r"(?:₹|rs\.?|inr)?\s*(?P<amount1>\d{1,3}(?:[ ,]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale1>k|thousand|lakh|lac)?\s*(?:-|–|to)\s*"
    r"(?:₹|rs\.?|inr)?\s*(?P<amount2>\d{1,3}(?:[ ,]\d{2,3})+|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<scale2>k|thousand|lakh|lac)?",
    re.IGNORECASE,
)
_SHORT_QUOTE_RE = re.compile(
    r"\b(?:at|for)\s+(?P<amount>\d{1,3}(?:[ ,]\d{2,3})+|\d{5,7})"
    r"\s*(?P<scale>k|thousand|lakh|lac)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LONG_QUOTE_RE = re.compile(
    r"\b(?:our\s+price|price|rate|quote|offer|final\s+price)\b[^\n]{0,80}?"
    r"\b(?:is|at|for|only)\s+(?:₹|rs\.?|inr)?\s*"
    r"(?P<amount>\d{1,3}(?:[ ,]\d{2,3})+|\d{5,7})"
    r"\s*(?P<scale>k|thousand|lakh|lac)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_LINE_ITEM_QUOTE_RE = re.compile(
    r"(?m)^\s*(?P<label>[A-Za-z][^\n:]{2,80}?)\s*(?::|\s-\s)\s*"
    r"(?:₹|rs\.?|inr)?\s*(?P<amount>\d{1,3}(?:[ ,]\d{2,3})+|\d{5,9})"
    r"\s*(?P<scale>k|thousand|lakh|lac)?\s*$",
    re.IGNORECASE,
)
_LINE_ITEM_SIGNAL_RE = re.compile(
    r"(?m)^\s*[A-Za-z][^\n:]{2,80}?\s*(?::|\s-\s)\s*(?:₹|rs\.?|inr)?\s*\d",
    re.IGNORECASE,
)
_PRICE_EXPRESSION_RE = re.compile(
    r"(?:₹|\b(?:rs\.?|inr)\b)\s*\d|"
    r"\b(?:price|rate|quote|offer|total|can\s+do)\b[^\n]{0,80}?\d",
    re.IGNORECASE,
)
_QUANTITY_RE = re.compile(
    r"\b(?:qty|quantity)\s*[:=]?\s*(?:[2-9]|[1-9]\d{1,2}|two|three|four|five|six|seven|eight|nine)\b|"
    r"(?<![,\d])\b(?:[2-9]|[1-9]\d{1,2}|two|three|four|five|six|seven|eight|nine)\s*(?:x|units?|pcs?|pieces?)\b|"
    r"\bx\s*(?:[2-9]|[1-9]\d{1,2})\b|"
    r"(?<![,\d])\b(?:[2-9]|[1-9]\d{1,2}|two|three|four|five|six|seven|eight|nine)\s+"
    r"(?=(?:"
    r"(?:rtx|gtx|rx|ryzen|intel|amd|sku[-_ ])|"
    r"(?:[a-z][a-z0-9-]*\s+){0,2}(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+"
    r"))",
    re.IGNORECASE,
)
_PRICE_CUE_RE = re.compile(
    r"(?:price|rate|quote|offer|can\s+do|available\s+at|getting(?:\s+at)?|"
    r"got(?:\s+at)?|offered(?:\s+at)?|need\s+at|want\s+at|target(?:\s+price)?|below|less\s+than)"
    r"\s*(?::|=|is|of|at|for|only)?\s*$",
    re.IGNORECASE,
)
_NON_PRICE_PREFIX_RE = re.compile(
    r"\b(?:pin(?:code)?|postal|zip|address|located|location|call|phone|mobile|"
    r"order|invoice|payment|paid|transaction|utr|model|rtx|gtx|ryzen|core)"
    r"(?:\s+(?:is|of|no\.?|number|id))?\s*$",
    re.IGNORECASE,
)
_PRICE_WORDS = re.compile(
    r"(?:₹|\b(?:price|rate|cost|quote|quotation|amount|rs|inr)\b)",
    re.IGNORECASE,
)
_REQUEST_WORDS = re.compile(
    r"\b(what|send|share|tell|check|need|please|pls|kitna|batao|availability|available)\b",
    re.IGNORECASE,
)
_OFFER_WORDS = re.compile(
    r"\b(our price|my price|best|final|offer|quoted?|can do|de denge|rate is|price is)\b",
    re.IGNORECASE,
)
_CLAIM_WORDS = re.compile(
    r"\b(getting|got|offered|already|available at|need at|want at|can you do|below|less|target|best price)\b",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"\b(immediate|cash|advance|pdc|post[- ]dated|credit|cod|\d+\s*days?)\b",
    re.IGNORECASE,
)
_IDENTIFIER_FIELDS = ("unique_identifier", "model_no", "sku", "sku_full")
_PRODUCT_MODEL_RE = re.compile(
    r"\b(?:"
    r"(?:rtx|gtx|rx|ryzen(?:\s+[3579])?|core\s+i[3579]|i[3579])\s*[- ]?\d{3,5}[a-z0-9]*|"
    r"sku[-_ ]?[a-z0-9-]+|"
    r"(?:[a-z][a-z0-9-]*\s+)?\d+(?:gb|tb)\s+(?:ssd|nvme|ram)"
    r")\b",
    re.IGNORECASE,
)


def is_pricing_enabled(org_id: str) -> bool:
    supported = {item for item in (LEE_DEALER_ORG_ID, KOMAL_ORG_ID) if item}
    return bool(org_id and org_id in supported and org_id in PRICING_INTELLIGENCE_ORG_IDS)


def is_price_signal(text: str) -> bool:
    stripped = (text or "").strip()
    return bool(_looks_like_request(stripped) or extract_prices(stripped))


def is_supplier_response(text: str) -> bool:
    """True for a direct procurement answer, not a fresh buyer price question."""
    return bool(
        text
        and not _looks_like_request(text)
        and (
            re.fullmatch(r"\s*\d{5,7}\s*", text)
            or extract_prices(text, established_context=True)
            or re.search(
                r"\b(available|in stock|out of stock|can arrange|yes|no|na|nahi)\b",
                text,
                re.IGNORECASE,
            )
        )
    )


def extract_prices(text: str, *, established_context: bool = False) -> list[float]:
    """Extract INR-sized values without treating arbitrary model numbers as prices."""
    if not text:
        return []
    values = []
    for match in _PRICE_RE.finditer(text):
        before = text[max(0, match.start() - 48):match.start()]
        has_price_cue = bool(_PRICE_CUE_RE.search(before))
        if not (match.group("currency") or match.group("scale") or has_price_cue):
            continue
        if not has_price_cue and _NON_PRICE_PREFIX_RE.search(before):
            continue
        raw = match.group("amount").replace(",", "").replace(" ", "")
        value = float(raw)
        scale = (match.group("scale") or "").lower()
        if scale in {"k", "thousand"}:
            value *= 1_000
        elif scale in {"lakh", "lac"}:
            value *= 100_000
        if (match.group("currency") or scale or value >= 1_000) and value <= 9_999_999:
            values.append(value)
    return values


def extract_quoted_prices(text: str) -> list[float]:
    """Extract only values that an outbound message presents as a price."""
    from services.sales_offer_policy import (
        extract_quoted_prices as extract_sales_offer_prices,
        is_outbound_offer,
        offer_prices,
    )

    values = offer_prices(text, extract_sales_offer_prices(text))

    for pattern in (
        _BARE_QUOTE_RE,
        _INCLUSIVE_QUOTE_RE,
        _SHORT_QUOTE_RE,
        _LONG_QUOTE_RE,
        _LINE_ITEM_QUOTE_RE,
    ):
        for match in pattern.finditer(text or ""):
            value = float(match.group("amount").replace(",", "").replace(" ", ""))
            scale = (match.groupdict().get("scale") or "").lower()
            if scale in {"k", "thousand"}:
                value *= 1_000
            elif scale in {"lakh", "lac"}:
                value *= 100_000
            if 0 < value <= 9_999_999 and value not in values:
                values.append(value)
    for match in _QUOTE_RANGE_RE.finditer(text or ""):
        for suffix in ("1", "2"):
            value = float(match.group(f"amount{suffix}").replace(",", "").replace(" ", ""))
            scale = (match.group(f"scale{suffix}") or "").lower()
            if scale in {"k", "thousand"}:
                value *= 1_000
            elif scale in {"lakh", "lac"}:
                value *= 100_000
            if 0 < value <= 9_999_999 and value not in values:
                values.append(value)
    stripped = (text or "").strip()
    if re.fullmatch(r"\d{5,7}", stripped):
        value = float(stripped)
        if value not in values:
            values.append(value)
    if values:
        return values if is_outbound_offer(text, values) else []
    return []


def _looks_like_request(text: str) -> bool:
    return bool(
        text
        and _PRICE_WORDS.search(text)
        and ("?" in text or _REQUEST_WORDS.search(text) or not _PRICE_RE.search(text))
        and not _OFFER_WORDS.search(text)
    )


def _looks_like_claim(text: str, prices: list[float], buyer_context: bool) -> bool:
    return bool(prices and (_CLAIM_WORDS.search(text or "") or _OFFER_WORDS.search(text or "")))


def _multiple_product_mentions(text: str) -> bool:
    mentions = {match.group(0).lower().replace(" ", "") for match in _PRODUCT_MODEL_RE.finditer(text or "")}
    return len(mentions) > 1


def _has_product_identifier(text: str) -> bool:
    if _PRODUCT_MODEL_RE.search(text or ""):
        return True
    for token in re.findall(r"[A-Za-z0-9-]{4,}", text or ""):
        normalized = token.lower().strip("-")
        if re.fullmatch(r"\d+(?:gb|tb|days?|months?|years?|yrs?)", normalized):
            continue
        if any(character.isalpha() for character in normalized) and any(character.isdigit() for character in normalized):
            return True
    return False


def _phone(conversation_id: str) -> str:
    raw = (conversation_id or "").replace("whatsapp_", "").replace("dealer_", "")
    return normalize_phone(raw)


def _sku(product: Optional[dict]) -> Optional[str]:
    if not product:
        return None
    value = next((product.get(field) for field in _IDENTIFIER_FIELDS if product.get(field)), None)
    return str(value).strip() if value is not None else None


def _product_context(product: Optional[dict], fallback: str = "") -> str:
    if product:
        return str(product.get("product_name") or product.get("model_name") or _sku(product) or fallback)
    return fallback[:300]


def _as_utc(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def floor_snapshot(product: Optional[dict], now: Optional[datetime] = None) -> tuple[Optional[float], str]:
    """Return a usable Sheet floor and a stable reason when it cannot be used."""
    if not product:
        return None, "product_unresolved"
    price = product.get("price")
    if isinstance(price, str):
        cleaned = re.sub(r"(?i)\b(?:inr|rs\.?)\b|₹|,|\s", "", price)
        try:
            price = float(cleaned)
        except ValueError:
            price = None
    if (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or not math.isfinite(price)
        or price <= 0
    ):
        return None, "missing_floor"
    if not (
        product.get("source") == "google_sheet"
        or product.get("source_sheet_id")
        or product.get("source_sheet_ids")
    ):
        return None, "non_sheet_floor"
    updated_at = _as_utc(product.get("updated_at"))
    if not updated_at:
        return None, "missing_floor_timestamp"
    age = (now or datetime.now(timezone.utc)) - updated_at
    if age < -timedelta(minutes=5):
        return None, "future_floor_timestamp"
    if age > FLOOR_MAX_AGE:
        return None, "stale_floor"
    return float(price), "current_sheet_floor"


def _floor_tax_basis(product: Optional[dict]) -> Optional[str]:
    if not product:
        return None
    value = str(product.get("tax_basis") or product.get("price_tax_basis") or "").lower()
    if value in {"included", "inclusive", "gst_inclusive"}:
        return "included"
    if value in {"extra", "exclusive", "gst_extra"}:
        return "extra"
    if isinstance(product.get("gst_inclusive"), bool):
        return "included" if product["gst_inclusive"] else "extra"
    return None


def quote_learning(floor: Optional[float], quoted_price: float) -> tuple[bool, Optional[float], Optional[float], str]:
    """Describe a human quote relative to its Sheet floor."""
    if floor is None:
        return False, None, None, "floor_unavailable"
    difference = float(quoted_price) - float(floor)
    if difference < 0:
        return False, None, difference, "below_floor"
    if difference > 50_000 or difference > float(floor) * 0.5:
        return False, None, difference, "implausible_markup"
    return True, difference, difference, "valid_human_markup"


async def ensure_pricing_indexes() -> None:
    db = get_async_mongo_db()
    if not PRICING_INTELLIGENCE_ORG_IDS:
        logger.warning("Pricing intelligence is disabled: PRICING_INTELLIGENCE_ORG_IDS is empty")
    for org_id in (LEE_DEALER_ORG_ID, KOMAL_ORG_ID):
        if not is_pricing_enabled(org_id):
            continue
        col = db[f"pricing_events_{org_id}"]
        try:
            await col.create_index("event_key", unique=True)
            await col.create_index([("source_message_id", 1), ("event_type", 1)])
            await col.create_index([("escalation_status", 1), ("next_attempt_at", 1)])
            await col.create_index(
                [("dealer_phone", 1), ("sku", 1), ("event_type", 1), ("created_at", -1)]
            )
            await col.create_index([("sku", 1), ("event_type", 1), ("created_at", -1)])
            await col.create_index([("dealer_phone", 1), ("sku", 1), ("hold_until", -1)])
            await col.create_index("expires_at", expireAfterSeconds=0)
            await db[f"pricing_delivery_{org_id}"].create_index("updated_at", expireAfterSeconds=86_400)
            audit_col = db[f"pricing_audit_outbox_{org_id}"]
            await audit_col.create_index([("status", 1), ("next_attempt_at", 1)])
            await audit_col.create_index("expires_at", expireAfterSeconds=0)
        except Exception as exc:
            logger.error(f"Pricing indexes unavailable for org={org_id}: {exc}")


async def _record(org_id: str, event: dict) -> bool:
    now = datetime.now(timezone.utc)
    event.setdefault("created_at", now)
    event.setdefault("expires_at", now + EVENT_RETENTION)
    event.setdefault("learnable", False)
    event.setdefault("counterparty_role", "unknown")
    event.setdefault("direction", "unknown")
    if not event.get("event_key"):
        raw = "|".join(
            str(event.get(key) or "")
            for key in ("source_message_id", "event_type", "dealer_phone", "sku", "raw_text")
        )
        event["event_key"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    result = await get_async_mongo_db()[f"pricing_events_{org_id}"].update_one(
        {"event_key": event["event_key"]},
        {"$setOnInsert": event},
        upsert=True,
    )
    return bool(result.upserted_id)


async def _event_for_message(org_id: str, message_id: Optional[str]) -> Optional[dict]:
    if not message_id:
        return None
    return await get_async_mongo_db()[f"pricing_events_{org_id}"].find_one(
        {"source_message_id": message_id},
        sort=[("created_at", -1)],
    )


async def _last_event(org_id: str, dealer_phone: str) -> Optional[dict]:
    col = get_async_mongo_db()[f"pricing_events_{org_id}"]
    event = await col.find_one(
        {
            "dealer_phone": dealer_phone,
            "created_at": {"$gte": datetime.now(timezone.utc) - HOURS_24},
        },
        sort=[("created_at", -1)],
    )
    if event and event.get("event_type") == "ai_customer_quote" and event.get("source_message_id"):
        skus = await col.distinct(
            "sku",
            {
                "dealer_phone": dealer_phone,
                "event_type": "ai_customer_quote",
                "source_message_id": event["source_message_id"],
            },
        )
        if len([sku for sku in skus if sku]) > 1:
            event = {**event, "sku": None, "product_context": "multiple products"}
    return event


async def _message_for_id(org_id: str, message_id: Optional[str]) -> Optional[dict]:
    if not message_id:
        return None
    return await get_async_mongo_db()[f"messages_{org_id}"].find_one(
        {"$or": [{"message_id": message_id}, {"id": message_id}]},
        {"content": 1, "role": 1, "message_id": 1},
    )


async def _resolve_product(
    org_id: str,
    text: str,
    *,
    context_event: Optional[dict] = None,
    context_message: Optional[dict] = None,
) -> Optional[dict]:
    products = get_async_mongo_db()[f"products_{org_id}"]
    context_sku = (context_event or {}).get("sku")
    context_product = None
    if context_sku:
        context_product = await products.find_one(
            {"$or": [{field: context_sku} for field in _IDENTIFIER_FIELDS]}
        )

    stopwords = {
        "price", "rate", "quote", "cost", "what", "send", "share", "please", "for",
        "available", "availability", "best", "final", "our", "your", "the", "this",
        "that", "with", "from", "have", "only", "inclusive", "including", "gst",
        "is", "are", "can", "you", "it", "at", "all", "and", "or", "to", "of",
        "total", "grand", "build", "bundle", "offer", "special", "today", "now",
        "yes", "no", "okay", "ok", "we", "i", "do",
    }

    async def search_catalog(value: str) -> tuple[Optional[dict], bool, bool]:
        searchable = re.sub(
            r"(?<![A-Za-z0-9])(?:₹|rs\.?|inr)?\s*"
            r"(?:\d{1,3}(?:[ ,]\d{2,3})+|\d{5,9}|\d+(?:\.\d+)?\s*(?:k|thousand|lakh|lac))"
            r"(?![A-Za-z0-9])",
            " ",
            value or "",
            flags=re.IGNORECASE,
        )
        terms = []
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{1,}", searchable):
            normalized = token.lower().strip("._/-")
            if not normalized or normalized in stopwords:
                continue
            if token.isdigit() and not 2 <= len(token) <= 4:
                continue
            if token not in terms:
                terms.append(token)
        terms = terms[:8]
        if not terms:
            return None, False, False
        exact = [
            {field: {"$regex": f"^{re.escape(term)}$", "$options": "i"}}
            for term in terms
            if any(character.isdigit() for character in term) and any(character.isalpha() for character in term)
            for field in _IDENTIFIER_FIELDS
        ]
        text_match = {
            "$and": [
                {
                    "$or": [
                        {"search_text": {"$regex": re.escape(term), "$options": "i"}},
                        {"product_name": {"$regex": re.escape(term), "$options": "i"}},
                        {"model_name": {"$regex": re.escape(term), "$options": "i"}},
                        {"model_no": {"$regex": re.escape(term), "$options": "i"}},
                    ]
                }
                for term in terms
            ]
        }
        query = {"$or": [*exact, text_match]} if exact else text_match
        candidates = await products.find(query).limit(2).to_list(length=2)
        if len(candidates) == 1:
            return candidates[0], True, True
        return None, True, bool(candidates)

    output_product, output_has_terms, output_matches_catalog = await search_catalog(text)
    if output_product:
        if context_product:
            output_ids = {str(output_product.get(field)).lower() for field in ("_id", *_IDENTIFIER_FIELDS) if output_product.get(field)}
            context_ids = {str(context_product.get(field)).lower() for field in ("_id", *_IDENTIFIER_FIELDS) if context_product.get(field)}
            if output_ids and context_ids and output_ids.isdisjoint(context_ids):
                return None
        return output_product
    if output_has_terms and (output_matches_catalog or _has_product_identifier(text)):
        return None
    if context_product:
        return context_product
    context_product, _, _ = await search_catalog((context_message or {}).get("content", ""))
    return context_product


async def _known_supplier(org_id: str, dealer_phone: str) -> bool:
    return bool(
        await get_async_mongo_db()["dealer_directory"].find_one(
            {
                "org_id": org_id,
                "active": True,
                "phone": {"$in": [dealer_phone, f"+{dealer_phone}"]},
            },
            {"_id": 1},
        )
    )


async def _active_hold(org_id: str, dealer_phone: str, sku: str) -> Optional[dict]:
    return await get_async_mongo_db()[f"pricing_events_{org_id}"].find_one(
        {
            "dealer_phone": dealer_phone,
            "sku": sku,
            "hold_until": {"$gt": datetime.now(timezone.utc)},
        },
        sort=[("hold_until", -1)],
    )


async def _market_summary(org_id: str, sku: Optional[str], floor: Optional[float] = None) -> dict:
    if not sku:
        return {"dealer_count": 0, "quote_min": None, "quote_max": None}
    since = datetime.now(timezone.utc) - HOURS_24
    col = get_async_mongo_db()[f"pricing_events_{org_id}"]
    floor_filter = {"sheet_floor_price": floor} if floor is not None else {}
    valid_activity = {
        "$or": [
            {"event_type": {"$in": ["buyer_price_request", "buyer_price_claim", "human_customer_quote"]}},
            {"event_type": "ai_customer_quote", "delivery_status": {"$in": ["delivered", "read"]}},
        ]
    }
    dealer_numbers = await col.distinct(
        "dealer_phone",
        {
            "sku": sku,
            **floor_filter,
            "counterparty_role": "buyer",
            **valid_activity,
            "created_at": {"$gte": since},
        },
    )
    quotes = await col.find(
        {
            "sku": sku,
            **floor_filter,
            "counterparty_role": "buyer",
            "$or": [
                {"event_type": "human_customer_quote"},
                {"event_type": "ai_customer_quote", "delivery_status": {"$in": ["delivered", "read"]}},
            ],
            "quoted_price": {"$type": "number"},
            "created_at": {"$gte": since},
            "ignored_reason": {"$ne": "below_floor"},
        },
        {"quoted_price": 1},
    ).to_list(length=500)
    values = [float(item["quoted_price"]) for item in quotes]
    return {
        "dealer_count": len([phone for phone in dealer_numbers if phone]),
        "quote_min": min(values) if values else None,
        "quote_max": max(values) if values else None,
    }


async def _escalate(
    org_id: str,
    *,
    event_key: str,
    dealer_phone: str,
    customer_name: str,
    raw_text: str,
    event_type: str,
    sku: Optional[str],
    product_context: str,
    floor: Optional[float],
    observed_price: Optional[float],
    reason: str,
) -> None:
    from services.team_service import create_ai_escalation

    market = await _market_summary(org_id, sku, floor)
    details = [
        f"Dealer: {dealer_phone}",
        f"Product: {product_context or sku or 'unresolved'}",
        f"SKU: {sku or 'unresolved'}",
        f"Sheet floor: {floor if floor is not None else 'unavailable'}",
        f"Observed price: {observed_price if observed_price is not None else 'none'}",
        f"Reason: {reason}",
        f"Dealers asking in last 24h: {market['dealer_count']}",
        f"Customer quote range in last 24h: {market['quote_min']} - {market['quote_max']}",
        f"Message: {raw_text}",
    ]
    await create_ai_escalation(
        org_id,
        {
            "id": f"pricing:{hashlib.sha256(event_key.encode()).hexdigest()}",
            "user_name": customer_name or "Dealer",
            "customer_number": dealer_phone,
            "customer_text": "\n".join(details),
            "inferred_intent": event_type,
            "status": "pending_human_review",
            "role": "pricing_guardrail",
            "sender_name": "Pricing Intelligence",
            "metadata": {
                "sku": sku,
                "product_context": product_context,
                "sheet_floor_price": floor,
                "observed_price": observed_price,
                "reason": reason,
                "market_summary": market,
            },
        },
    )


async def _deliver_pricing_escalation(org_id: str, event_key: str) -> None:
    """Claim and deliver one durable pricing escalation."""
    col = get_async_mongo_db()[f"pricing_events_{org_id}"]
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4().hex
    result = await col.update_one(
        {
            "event_key": event_key,
            "$or": [
                {"escalation_status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": now}},
                {"escalation_status": "sending", "escalation_claimed_at": {"$lt": now - timedelta(minutes=5)}},
            ],
        },
        {"$set": {"escalation_status": "sending", "escalation_claim_id": claim_id, "escalation_claimed_at": now}},
    )
    if result.modified_count != 1:
        return
    event = await col.find_one({"event_key": event_key, "escalation_claim_id": claim_id})
    payload = (event or {}).get("escalation_payload")
    if not payload:
        await col.update_one(
            {"event_key": event_key, "escalation_claim_id": claim_id},
            {"$set": {"escalation_status": "failed", "next_attempt_at": now + timedelta(minutes=1)}},
        )
        return
    try:
        await _escalate(org_id, event_key=event_key, **payload)
    except Exception as exc:
        logger.error(f"Pricing escalation failed for org={org_id}: {exc}")
        await col.update_one(
            {"event_key": event_key, "escalation_claim_id": claim_id},
            {
                "$set": {"escalation_status": "failed", "next_attempt_at": now + timedelta(minutes=1)},
                "$inc": {"escalation_attempts": 1},
            },
        )
        return
    await col.update_one(
        {"event_key": event_key, "escalation_claim_id": claim_id},
        {"$set": {"escalation_status": "sent", "escalated_at": datetime.now(timezone.utc)}},
    )


async def _queue_pricing_escalation(org_id: str, event_key: str, **payload) -> None:
    col = get_async_mongo_db()[f"pricing_events_{org_id}"]
    now = datetime.now(timezone.utc)
    await col.update_one(
        {"event_key": event_key, "escalation_status": {"$nin": ["sent", "sending"]}},
        {"$set": {"escalation_status": "pending", "escalation_payload": payload, "next_attempt_at": now}},
    )
    await _deliver_pricing_escalation(org_id, event_key)


async def process_pending_pricing_escalations() -> None:
    now = datetime.now(timezone.utc)
    for org_id in PRICING_INTELLIGENCE_ORG_IDS:
        if not is_pricing_enabled(org_id):
            continue
        col = get_async_mongo_db()[f"pricing_events_{org_id}"]
        rows = await col.find(
            {
                "$or": [
                    {"escalation_status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": now}},
                    {"escalation_status": "sending", "escalation_claimed_at": {"$lt": now - timedelta(minutes=5)}},
                ]
            },
            {"event_key": 1},
        ).limit(50).to_list(length=50)
        for row in rows:
            await _deliver_pricing_escalation(org_id, row["event_key"])
        audits = await get_async_mongo_db()[f"pricing_audit_outbox_{org_id}"].find(
            {"status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": now}},
            {"_id": 1},
        ).limit(50).to_list(length=50)
        for audit in audits:
            await _process_accepted_ai_audit(org_id, audit["_id"])


async def validate_ai_pricing_response(
    *,
    org_id: str,
    conversation_id: str,
    source_message_id: Optional[str],
    content: str,
) -> tuple[bool, str]:
    """Fail closed unless every AI price is backed by one current Sheet product."""
    if not is_pricing_enabled(org_id):
        return True, "pricing_disabled"
    quoted_prices = extract_quoted_prices(content)
    if not quoted_prices:
        from services.sales_offer_policy import is_outbound_offer

        unsafe_expression = bool(
            _LINE_ITEM_SIGNAL_RE.search(content or "")
            or (
                _PRICE_EXPRESSION_RE.search(content or "")
                and is_outbound_offer(content, [1.0])
            )
        )
        if not unsafe_expression:
            return True, "no_outbound_price"
        reason = "invalid_ai_price"
    elif len(quoted_prices) > 1 and not _QUOTE_RANGE_RE.search(content or ""):
        reason = "ambiguous_multiple_product_prices"
    elif _multiple_product_mentions(content):
        reason = "ambiguous_multiple_products"
    elif _QUANTITY_RE.search(content or ""):
        reason = "structured_quantity_required"
    else:
        reason = None

    context_event = await _event_for_message(org_id, source_message_id)
    context_message = await _message_for_id(org_id, source_message_id)
    product = await _resolve_product(
        org_id,
        content,
        context_event=context_event,
        context_message=context_message,
    )
    sku = _sku(product) or (context_event or {}).get("sku")
    product_context = _product_context(product, content)
    floor, floor_status = floor_snapshot(product)
    if not reason and not product:
        reason = "product_unresolved"
    elif not reason and floor is None:
        reason = floor_status
    elif not reason:
        from services.sales_offer_policy import tax_basis

        quote_tax_basis = tax_basis(content)
        if quote_tax_basis == "unspecified":
            quote_tax_basis = "included"
        floor_tax_basis = _floor_tax_basis(product)
        if not floor_tax_basis:
            reason = "missing_floor_tax_basis"
        elif quote_tax_basis != floor_tax_basis:
            reason = "tax_basis_mismatch"
    if not reason and floor is not None and any(price < floor for price in quoted_prices):
        reason = "below_floor_ai_price"

    if not reason:
        return True, "validated_ai_prices"

    event_key = (
        f"{source_message_id or conversation_id}:ai_output_blocked:"
        f"{hashlib.sha256(content.encode()).hexdigest()}"
    )
    await _record(
        org_id,
        {
            "event_key": event_key,
            "event_type": "ai_output_blocked",
            "direction": "outbound",
            "actor": "ai",
            "counterparty_role": "buyer",
            "dealer_phone": _phone(conversation_id),
            "conversation_id": conversation_id,
            "sku": sku,
            "product_context": product_context,
            "raw_text": content,
            "observed_price": min(quoted_prices) if quoted_prices else None,
            "observed_prices": quoted_prices,
            "sheet_floor_price": floor,
            "source_message_id": source_message_id,
            "decision_reason": reason,
            "learnable": False,
        },
    )
    await _queue_pricing_escalation(
        org_id,
        event_key,
        dealer_phone=_phone(conversation_id),
        customer_name="Dealer",
        raw_text=content,
        event_type="ai_output_blocked",
        sku=sku,
        product_context=product_context,
        floor=floor,
        observed_price=min(quoted_prices) if quoted_prices else None,
        reason=reason,
    )
    return False, reason


async def _record_accepted_ai_response(
    *,
    org_id: str,
    conversation_id: str,
    inbound_message_id: Optional[str],
    outbound_message_id: str,
    content: str,
) -> None:
    """Record provider acceptance; delivery receipts decide whether it is learnable."""
    if not is_pricing_enabled(org_id) or not outbound_message_id:
        return
    quoted_prices = extract_quoted_prices(content)
    if not quoted_prices:
        return
    context_event = await _event_for_message(org_id, inbound_message_id)
    context_message = await _message_for_id(org_id, inbound_message_id)
    product = await _resolve_product(
        org_id,
        content,
        context_event=context_event,
        context_message=context_message,
    )
    floor, floor_status = floor_snapshot(product)
    sku = _sku(product) or (context_event or {}).get("sku")
    if not product or floor is None or any(price < floor for price in quoted_prices):
        return
    for quoted_price in quoted_prices:
        await _record(
            org_id,
            {
                "event_key": f"{outbound_message_id}:{sku or 'unresolved'}:{quoted_price}:ai_customer_quote",
                "event_type": "ai_customer_quote",
                "direction": "outbound",
                "actor": "ai",
                "counterparty_role": "buyer",
                "dealer_phone": _phone(conversation_id),
                "conversation_id": conversation_id,
                "sku": sku,
                "product_context": _product_context(product, content),
                "raw_text": content,
                "quoted_price": float(quoted_price),
                "sheet_floor_price": floor,
                "floor_status": floor_status,
                "source_message_id": outbound_message_id,
                "inbound_source_message_id": inbound_message_id,
                "delivery_status": "provider_accepted",
                "provider_accepted_at": datetime.now(timezone.utc),
                "learnable": False,
            },
        )
    await _reconcile_ai_pricing_delivery(org_id, outbound_message_id)


async def _process_accepted_ai_audit(org_id: str, message_id: str) -> None:
    col = get_async_mongo_db()[f"pricing_audit_outbox_{org_id}"]
    job = await col.find_one({"_id": message_id})
    if not job:
        return
    try:
        await _record_accepted_ai_response(org_id=org_id, **job["payload"])
    except Exception as exc:
        logger.error(f"Accepted AI pricing audit failed for org={org_id}: {exc}")
        await col.update_one(
            {"_id": message_id},
            {
                "$set": {
                    "status": "failed",
                    "last_error": str(exc)[:500],
                    "next_attempt_at": datetime.now(timezone.utc) + timedelta(minutes=1),
                },
                "$inc": {"attempts": 1},
            },
        )
        return
    await col.delete_one({"_id": message_id})


async def record_accepted_ai_response(
    *,
    org_id: str,
    conversation_id: str,
    inbound_message_id: Optional[str],
    outbound_message_id: str,
    content: str,
) -> None:
    """Durably audit a provider-accepted AI price without retrying the send."""
    if not is_pricing_enabled(org_id) or not outbound_message_id:
        return
    now = datetime.now(timezone.utc)
    await get_async_mongo_db()[f"pricing_audit_outbox_{org_id}"].update_one(
        {"_id": outbound_message_id},
        {
            "$setOnInsert": {
                "payload": {
                    "conversation_id": conversation_id,
                    "inbound_message_id": inbound_message_id,
                    "outbound_message_id": outbound_message_id,
                    "content": content,
                },
                "status": "pending",
                "attempts": 0,
                "created_at": now,
                "next_attempt_at": now,
                "expires_at": now + EVENT_RETENTION,
            }
        },
        upsert=True,
    )
    await _process_accepted_ai_audit(org_id, outbound_message_id)


async def _reconcile_ai_pricing_delivery(org_id: str, message_id: str) -> None:
    db = get_async_mongo_db()
    receipt_col = db[f"pricing_delivery_{org_id}"]
    receipt = await receipt_col.find_one({"_id": message_id})
    if not receipt:
        return
    incoming = str(receipt.get("status") or "")
    rank = {"provider_accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
    col = db[f"pricing_events_{org_id}"]
    rows = await col.find(
        {"source_message_id": message_id, "event_type": "ai_customer_quote"},
        {"event_key": 1, "delivery_status": 1},
    ).to_list(length=50)
    if not rows:
        return
    now = datetime.now(timezone.utc)
    for row in rows:
        current = row.get("delivery_status") or "provider_accepted"
        next_status = "failed" if incoming == "failed" else (
            incoming if rank[incoming] >= rank.get(current, 0) else current
        )
        changes = {"delivery_status": next_status, "delivery_updated_at": now}
        if next_status in {"delivered", "read"}:
            changes["delivered_at"] = now
        elif next_status == "failed":
            changes["ignored_reason"] = "delivery_failed"
        await col.update_one({"event_key": row["event_key"]}, {"$set": changes})
    await receipt_col.delete_one({"_id": message_id})


async def update_ai_pricing_delivery(org_id: str, message_id: str, status: str) -> None:
    if not is_pricing_enabled(org_id) or not message_id:
        return
    from services.sales_offer_policy import DELIVERY_RANK

    incoming = str(status or "").lower()
    if incoming not in DELIVERY_RANK:
        return
    now = datetime.now(timezone.utc)
    rank = DELIVERY_RANK[incoming]
    await get_async_mongo_db()[f"pricing_delivery_{org_id}"].update_one(
        {"_id": message_id},
        [{"$set": {
            "status": {"$cond": [
                {"$gte": [rank, {"$ifNull": ["$rank", 0]}]},
                incoming,
                {"$ifNull": ["$status", incoming]},
            ]},
            "rank": {"$max": [{"$ifNull": ["$rank", 0]}, rank]},
            "updated_at": now,
        }}],
        upsert=True,
    )
    await _reconcile_ai_pricing_delivery(org_id, message_id)


def _payment_terms(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).lower() for match in _PAYMENT_RE.finditer(text or "")))


async def observe_agent_message(
    *,
    org_id: str,
    conversation_id: str,
    content: str,
    source_message_id: Optional[str],
    reply_to_message_id: Optional[str],
    sender_name: str,
) -> None:
    """Log a successfully sent human message without blocking its delivery."""
    if not is_pricing_enabled(org_id) or not content:
        return
    dealer_phone = _phone(conversation_id)
    reply_event = await _event_for_message(org_id, reply_to_message_id)
    reply_message = await _message_for_id(org_id, reply_to_message_id)
    last_event = await _last_event(org_id, dealer_phone)
    known_supplier = await _known_supplier(org_id, dealer_phone)
    reply_role = (reply_event or {}).get("counterparty_role")
    buyer_context = bool(
        reply_role == "buyer"
        or (
            not reply_event
            and (
                (last_event or {}).get("counterparty_role") == "buyer"
                or ((reply_message or {}).get("role") == "customer" and not known_supplier)
            )
        )
    )
    supplier_context = bool(
        conversation_id.startswith("dealer_")
        or reply_role == "supplier"
        or (
            not reply_event
            and known_supplier
            and not buyer_context
            and (
                _looks_like_request(content)
                or bool(extract_prices(content, established_context=True))
            )
        )
        or (
            not reply_event
            and (last_event or {}).get("counterparty_role") == "supplier"
            and (last_event or {}).get("event_type") in {"supplier_request", "supplier_offer"}
        )
    )
    prices = (
        extract_quoted_prices(content)
        if buyer_context and not supplier_context
        else extract_prices(content, established_context=supplier_context)
    )

    if supplier_context:
        event_type, role = "supplier_request", "supplier"
    elif prices and (buyer_context or _OFFER_WORDS.search(content)):
        event_type, role = "human_customer_quote", "buyer"
    elif _looks_like_request(content):
        event_type, role = "buyer_price_followup", "buyer"
    elif prices:
        event_type, role = "unresolved_human_price", "unknown"
    else:
        return

    context_event = reply_event or last_event
    product = await _resolve_product(
        org_id,
        content,
        context_event=context_event,
        context_message=reply_message,
    )
    floor, floor_reason = floor_snapshot(product)
    observed = prices[0] if len(prices) == 1 else None
    learnable = False
    markup = None
    difference = None
    ignored_reason = "ambiguous_multiple_prices" if len(prices) > 1 else None
    if event_type == "human_customer_quote" and observed is not None:
        if _multiple_product_mentions(content):
            ignored_reason = "ambiguous_multiple_products"
        elif re.search(r"\b(?:incl(?:uding)?|excl(?:uding)?|\+)\s*gst\b|\bgst\s*(?:included|extra)\b", content, re.I):
            ignored_reason = "ambiguous_tax_basis"
        elif re.search(r"\b\d+\s*(?:pcs?|pieces?|units?|qty)\b", content, re.I):
            ignored_reason = "ambiguous_quantity_total"
        else:
            learnable, markup, difference, ignored_reason = quote_learning(floor, observed)

    await _record(
        org_id,
        {
            "event_key": (
                f"{source_message_id}:{event_type}"
                if source_message_id
                else hashlib.sha256(
                    f"{event_type}|{dealer_phone}|{_sku(product) or ''}|{content}".encode()
                ).hexdigest()
            ),
            "event_type": event_type,
            "direction": "outbound",
            "actor": "human_agent",
            "counterparty_role": role,
            "dealer_phone": dealer_phone,
            "conversation_id": conversation_id,
            "sku": _sku(product) or (context_event or {}).get("sku"),
            "product_context": _product_context(product, content),
            "raw_text": content,
            "observed_price": observed,
            "observed_prices": prices,
            "quoted_price": observed if event_type == "human_customer_quote" else None,
            "sheet_floor_price": floor,
            "floor_updated_at": (product or {}).get("updated_at"),
            "markup_amount": markup,
            "price_difference": difference,
            "learnable": learnable,
            "ignored_reason": ignored_reason or (None if role != "unknown" else "unresolved_direction"),
            "floor_status": floor_reason,
            "stock_snapshot": (product or {}).get(
                "stock",
                (product or {}).get("stock_available", (product or {}).get("in_stock")),
            ),
            "payment_terms": _payment_terms(content),
            "source_message_id": source_message_id,
            "reply_to_message_id": reply_to_message_id,
            "sender_name": sender_name,
            "decision_reason": event_type,
        },
    )


async def observe_customer_message(
    *,
    org_id: str,
    conversation_id: str,
    content: str,
    source_message_id: str,
    reply_to_message_id: Optional[str],
    customer_name: str,
) -> dict:
    """Observe an inbound turn and return whether normal automation must stop."""
    if not is_pricing_enabled(org_id) or not content:
        return {"intercept": False}
    dealer_phone = _phone(conversation_id)
    reply_event = await _event_for_message(org_id, reply_to_message_id)
    reply_message = await _message_for_id(org_id, reply_to_message_id)
    last_event = await _last_event(org_id, dealer_phone)
    fresh_buyer_signal = _looks_like_request(content) or bool(_CLAIM_WORDS.search(content))
    supplier_context = bool(
        (reply_event or {}).get("event_type") == "supplier_request"
        or (
            (last_event or {}).get("event_type") == "supplier_request"
            and not fresh_buyer_signal
            and (extract_prices(content, established_context=True) or re.search(r"\b(available|stock|yes|no)\b", content, re.I))
        )
    )
    if supplier_context:
        prices = extract_prices(content, established_context=True)
        product = await _resolve_product(
            org_id,
            content,
            context_event=reply_event or last_event,
            context_message=reply_message,
        )
        await _record(
            org_id,
            {
                "event_key": f"{source_message_id}:supplier_offer",
                "event_type": "supplier_offer",
                "direction": "inbound",
                "actor": "supplier",
                "counterparty_role": "supplier",
                "dealer_phone": dealer_phone,
                "conversation_id": conversation_id,
                "sku": _sku(product) or (reply_event or last_event or {}).get("sku"),
                "product_context": _product_context(product, (reply_event or last_event or {}).get("product_context", content)),
                "raw_text": content,
                "observed_price": prices[-1] if prices else None,
                "source_message_id": source_message_id,
                "reply_to_message_id": reply_to_message_id,
                "decision_reason": "linked_supplier_request",
            },
        )
        return {"intercept": True, "reply": None}

    buyer_context = bool(
        (reply_event or {}).get("counterparty_role") == "buyer"
        or (last_event or {}).get("counterparty_role") == "buyer"
        or (reply_message or {}).get("role") == "customer"
    )
    prices = extract_prices(content, established_context=buyer_context)
    is_request = _looks_like_request(content)
    is_claim = _looks_like_claim(content, prices, buyer_context)
    if not is_request and not is_claim:
        return {"intercept": False}

    context_event = reply_event or last_event
    product = await _resolve_product(
        org_id,
        content,
        context_event=context_event,
        context_message=reply_message,
    )
    sku = _sku(product) or (context_event or {}).get("sku")
    product_name = _product_context(product, content)
    observed = prices[0] if is_claim and len(prices) == 1 else None
    event_type = "buyer_price_claim" if is_claim else "buyer_price_request"

    if is_claim and (not sku or len(prices) != 1):
        unresolved_reason = (
            "ambiguous_multiple_prices"
            if len(prices) != 1
            else "sku_and_direction_unresolved"
        )
        event_key = f"{source_message_id}:{unresolved_reason}"
        await _record(
            org_id,
            {
                "event_key": event_key,
                "event_type": "unresolved_price_direction",
                "direction": "inbound",
                "actor": "external_contact",
                "counterparty_role": "unknown",
                "dealer_phone": dealer_phone,
                "conversation_id": conversation_id,
                "raw_text": content,
                "observed_price": observed,
                "observed_prices": prices,
                "source_message_id": source_message_id,
                "reply_to_message_id": reply_to_message_id,
                "ignored_reason": unresolved_reason,
            },
        )
        await _queue_pricing_escalation(
            org_id,
            event_key,
            dealer_phone=dealer_phone,
            customer_name=customer_name,
            raw_text=content,
            event_type="unresolved_price_direction",
            sku=sku,
            product_context=product_name,
            floor=None,
            observed_price=observed,
            reason=unresolved_reason,
        )
        return {"intercept": True, "reply": None}

    floor, floor_reason = floor_snapshot(product)
    existing_hold = await _active_hold(org_id, dealer_phone, sku) if sku else None
    hold_until = None
    reason = None
    intercept = False

    if existing_hold:
        intercept, reason = True, "active_sku_hold"
    elif (product or sku) and floor is None:
        intercept, reason = True, floor_reason
    elif is_claim and floor is not None and observed is not None and observed < floor:
        intercept, reason = True, "below_floor_claim"
        hold_until = datetime.now(timezone.utc) + HOURS_24

    event_key = f"{source_message_id}:{event_type}"
    await _record(
        org_id,
        {
            "event_key": event_key,
            "event_type": event_type,
            "direction": "inbound",
            "actor": "buyer",
            "counterparty_role": "buyer",
            "dealer_phone": dealer_phone,
            "conversation_id": conversation_id,
            "sku": sku,
            "product_context": product_name,
            "raw_text": content,
            "observed_price": observed,
            "observed_prices": prices,
            "sheet_floor_price": floor,
            "floor_updated_at": (product or {}).get("updated_at"),
            "floor_status": floor_reason,
            "hold_until": hold_until,
            "stock_snapshot": (product or {}).get(
                "stock",
                (product or {}).get("stock_available", (product or {}).get("in_stock")),
            ),
            "payment_terms": _payment_terms(content),
            "source_message_id": source_message_id,
            "reply_to_message_id": reply_to_message_id,
            "decision_reason": reason or "continue_to_sales_agent",
        },
    )
    if intercept and not existing_hold:
        await _queue_pricing_escalation(
            org_id,
            event_key,
            dealer_phone=dealer_phone,
            customer_name=customer_name,
            raw_text=content,
            event_type=event_type,
            sku=sku,
            product_context=product_name,
            floor=floor,
            observed_price=observed,
            reason=reason,
        )
    return {"intercept": intercept, "reply": HOLDING_REPLY if intercept else None}


async def record_oos_supplier_reply(
    *,
    org_id: str,
    dealer_phone: str,
    content: str,
    source_message_id: Optional[str],
    oos_doc: dict,
) -> None:
    """Mirror only the price signal; the existing OOS document stays canonical."""
    if not is_pricing_enabled(org_id):
        return
    attrs = oos_doc.get("extracted_attributes", {})
    context = " ".join(
        str(value)
        for value in (attrs.get("brand"), attrs.get("model_name"), oos_doc.get("raw_query_text"))
        if value
    )
    product = await _resolve_product(org_id, context)
    prices = extract_prices(content, established_context=True)
    if not prices and re.fullmatch(r"\s*\d{5,7}\s*", content or ""):
        prices = [float(content.strip())]
    await _record(
        org_id,
        {
            "event_key": f"{source_message_id or oos_doc.get('query_id')}:{dealer_phone}:supplier_offer",
            "event_type": "supplier_offer",
            "direction": "inbound",
            "actor": "supplier",
            "counterparty_role": "supplier",
            "dealer_phone": normalize_phone(dealer_phone),
            "conversation_id": f"dealer_{normalize_phone(dealer_phone)}",
            "sku": _sku(product),
            "product_context": _product_context(product, context),
            "raw_text": content,
            "observed_price": prices[-1] if prices else None,
            "source_message_id": source_message_id,
            "oos_query_id": oos_doc.get("query_id"),
            "decision_reason": "oos_request_link",
        },
    )
