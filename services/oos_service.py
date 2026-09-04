"""
oos_service.py

Handles:
  - Reading dealer directory from connected Google Sheets
  - Sending dealer inquiry messages (within time window)
  - Processing dealer replies
  - Logging responses to Google Sheet
  - Creating aggregated escalations
  - Expiry handling (24h TTL)
"""

import asyncio
import re
import urllib.parse
import uuid
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Optional, List, Dict

import httpx
from loguru import logger

from database import get_mongo_db, get_async_mongo_db

IST = timezone(timedelta(hours=5, minutes=30))
WINDOW_START = dt_time(11, 0)
WINDOW_END = dt_time(19, 30)
OOS_TTL_HOURS = 24
MAX_DEALERS_PER_SEND = 50
PENDING_SEND_BATCH = 100

# Validate Google Sheet IDs (alphanumeric + hyphens/underscores)
SHEET_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def canonical_brand(value: Optional[str]) -> str:
    """Normalize brand casing, hyphens, and whitespace for consistent dealer matching."""
    if not value:
        return ""
    key = " ".join(value.casefold().replace("-", " ").split())
    return {"lianli": "lian li"}.get(key, key)


def is_within_dealer_hours() -> bool:
    return True


def _get_sheet_by_purpose(org_id: str, purpose: str) -> Optional[Dict]:
    """Find a connected Google Sheet by its purpose tag."""
    db = get_mongo_db()
    return db.google_sheets_connections.find_one({"org_id": org_id, "purpose": purpose})


def _sanitize_for_sheet(value: str) -> str:
    """Strip leading = + - @ to prevent formula injection in Google Sheets."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


async def fetch_dealer_directory(org_id: str, sheet_id: Optional[str] = None, tab_name: Optional[str] = None) -> List[Dict]:
    """
    Read dealer directory rows from the fake Google Sheet 'dealer_directory' collection in MongoDB.
    Returns list of {name, phone, brands, category} dicts.
    """
    async_db = get_async_mongo_db()
    try:
        cursor = async_db["dealer_directory"].find({"org_id": org_id, "active": True})
        dealers = []
        async for doc in cursor:
            dealers.append({
                "name": doc.get("name", ""),
                "phone": doc.get("phone", ""),
                "brands": doc.get("brands", ""),
                "category": doc.get("category", ""),
            })
        logger.info(f"[OOS] Fetched {len(dealers)} active dealers from fake sheet collection 'dealer_directory' for org {org_id}")
        return dealers
    except Exception as e:
        logger.error(f"[OOS] Failed to fetch dealers from fake sheet collection 'dealer_directory' for org {org_id}: {e}")
        return []


def _format_brand(brand: str) -> str:
    """Format brand dynamically: uppercase short acronyms (e.g. NZXT, MSI, HP, AMD, LG) or title-case word brands."""
    if not brand:
        return ""
    b = brand.strip()
    vowels = {"a", "e", "i", "o", "u"}
    # Short acronym (<= 3 chars like HP/MSI/LG/AMD, or 4-char consonant acronym like NZXT)
    is_consonant_acronym = len(b) <= 4 and not any(c in vowels for c in b.lower()) and " " not in b
    if is_consonant_acronym or (len(b) <= 3 and " " not in b):
        return b.upper()
    return b.title()


CONVERSATIONAL_PREFIXES = [
    r"^(what|how)\s+about\s+",
    r"^about\s+",
    r"^do you have\s+",
    r"^have you got\s+",
    r"^is\s+(there\s+)?",
    r"^are\s+(there\s+)?",
    r"^can i get\s+",
    r"^can you check\s+(about\s+)?",
    r"^i need\s+",
    r"^i want\s+",
    r"^looking for\s+",
    r"^price for\s+",
    r"^cost of\s+",
    r"^give me\s+",
    r"^check\s+(about\s+)?",
    r"^please check\s+(about\s+)?",
]

BUDGET_SUFFIXES = [
    r",?\s*budget\s*(around|is|of)?\s*\d+.*$",
    r",?\s*price\s*(around|is|of)?\s*\d+.*$",
    r",?\s*under\s*\d+.*$",
    r",?\s*around\s*\d+.*$",
]


def clean_product_query(raw_query: str) -> str:
    """Strip conversational filler prefixes, punctuation, and budget mentions from customer queries."""
    if not raw_query:
        return ""
    q = raw_query.strip()
    for pat in CONVERSATIONAL_PREFIXES:
        q = re.sub(pat, "", q, flags=re.IGNORECASE).strip()
    for pat in BUDGET_SUFFIXES:
        q = re.sub(pat, "", q, flags=re.IGNORECASE).strip()
    q = q.rstrip("?!.,").strip()
    return q


def _format_product_title(text: str, brand: str = "") -> str:
    """Format product words cleanly preserving brand and capitalizing model words."""
    if not text:
        return ""
    if brand:
        formatted_brand = _format_brand(brand)
        pattern = re.compile(re.escape(brand), re.IGNORECASE)
        if pattern.search(text):
            remainder = pattern.sub("", text).strip()
            vowels = {"a", "e", "i", "o", "u"}
            rem_words = [
                w.upper() if (len(w) <= 4 and not any(c in vowels for c in w.lower()) and not w.isdigit()) else w.capitalize()
                for w in remainder.split()
            ]
            rem_str = " ".join(rem_words)
            return f"{formatted_brand} {rem_str}".strip()

    words = text.split()
    formatted = []
    vowels = {"a", "e", "i", "o", "u"}
    for w in words:
        if len(w) <= 4 and not any(c in vowels for c in w.lower()) and not w.isdigit():
            formatted.append(w.upper())
        else:
            formatted.append(w.capitalize())
    return " ".join(formatted)


def format_dealer_sourcing_message(extracted_attrs: dict, raw_query: str = "") -> str:
    """
    Format a concise, redrafted sourcing message for dealers across any brand/org.
    Excludes budget, raw customer query conversational noise, and internal labels.
    Produces:
        <Brand> <Model> <Category> in <Colour>

        bhai price and availability?
    """
    raw_brand = (extracted_attrs.get("brand") or "").strip()
    brand = _format_brand(raw_brand)
    model = (extracted_attrs.get("model_name") or "").strip()
    category = (extracted_attrs.get("category") or "").strip().title()
    specs = extracted_attrs.get("specs") or {}

    colour = specs.get("colour") or specs.get("color")

    parts = [p for p in (brand, model, category) if p]

    if len(parts) >= 2 or (len(parts) == 1 and not brand):
        title = " ".join(parts)
        if colour:
            title += f" in {str(colour).strip().capitalize()}"
    elif raw_query and raw_query.strip():
        cleaned_raw = clean_product_query(raw_query)
        if cleaned_raw:
            title = _format_product_title(cleaned_raw, raw_brand)
            if colour and colour.lower() not in title.lower():
                title += f" in {str(colour).strip().capitalize()}"
        else:
            title = parts[0] if parts else "Product Inquiry"
    elif parts:
        title = parts[0]
    else:
        title = "Product Inquiry"

    return f"{title}\n\nbhai price and availability?"


def generate_dealer_message(oos_doc: dict) -> str:
    """Generate the concise sourcing message to send to dealers."""
    attrs = oos_doc.get("extracted_attributes", {})
    raw_query = oos_doc.get("raw_query_text", "")
    return format_dealer_sourcing_message(attrs, raw_query)


async def send_oos_to_dealers(oos_doc: dict, org_id: str):
    """
    Fetch dealers from Google Sheet, generate message, and send via WhatsApp.
    Only sends if within business hours (11 AM - 7:30 PM IST).
    Uses atomic status transition to prevent double-sends.
    """
    from services.wa_service import send_whatsapp_message

    logger.info(f"[OOS] send_oos_to_dealers triggered for query_id={oos_doc['query_id']} (org={org_id})")

    if not is_within_dealer_hours():
        logger.info(f"[OOS] Outside dealer business hours. Keeping status as 'pending_send' for query_id={oos_doc['query_id']}")
        async_db = get_async_mongo_db()
        await async_db["oos_requests"].update_one(
            {"query_id": oos_doc["query_id"]},
            {"$set": {"status": "pending_send"}}
        )
        return

    # Atomic claim: only proceed if we can transition to "sending"
    async_db = get_async_mongo_db()
    claim = await async_db["oos_requests"].update_one(
        {"query_id": oos_doc["query_id"], "status": "pending_send"},
        {"$set": {"status": "sending"}}
    )
    if claim.modified_count == 0:
        logger.info(f"[OOS] Request query_id={oos_doc['query_id']} is already being processed or sent. Aborting duplicate send.")
        return  # already being processed

    logger.info(f"[OOS] Atomic claim succeeded. Transitioned query_id={oos_doc['query_id']} to status='sending'")

    # Fetch dealers from Google Sheet, filter by brand
    brand = oos_doc.get("extracted_attributes", {}).get("brand")
    logger.info(f"[OOS] Fetching dealers from directory for org {org_id}...")
    all_dealers = await fetch_dealer_directory(org_id)

    # Filter by brand if known or detected in raw query
    raw_query_raw = oos_doc.get("raw_query_text", "") or ""
    raw_query = raw_query_raw.lower()
    normalized_raw_query = " ".join(raw_query.replace("-", " ").split())
    compact_raw_query = normalized_raw_query.replace(" ", "")
    known_brands = [
        "lenovo", "asus", "dell", "msi", "hp", "apple", "mac", "macbook",
        "acer", "samsung", "gigabyte", "lian li", "lianli", "nzxt"
    ]
    detected_brand = None
    if brand:
        detected_brand = canonical_brand(brand)
    else:
        # Dynamically check if any brand configured in this tenant's dealer directory matches the raw query
        for d in all_dealers:
            for b in (d.get("brands") or "").split(","):
                cb = canonical_brand(b)
                if cb and (cb in normalized_raw_query or b.lower().strip() in raw_query or cb.replace(" ", "") in compact_raw_query):
                    detected_brand = cb
                    break
            if detected_brand:
                break
        if not detected_brand:
            for kb in known_brands:
                canonical_kb = canonical_brand(kb)
                if canonical_kb in normalized_raw_query or kb in raw_query or canonical_kb.replace(" ", "") in compact_raw_query:
                    detected_brand = canonical_kb
                    break

    if detected_brand in ("mac", "macbook", "apple"):
        brand_search_terms = ["apple", "mac", "macbook"]
    else:
        brand_search_terms = [detected_brand] if detected_brand else []

    if detected_brand:
        target_dealers = []
        for d in all_dealers:
            dealer_brands = [
                canonical_brand(v)
                for v in d.get("brands", "").split(",")
                if v.strip()
            ]
            if any(term in dealer_brands for term in brand_search_terms):
                target_dealers.append(d)
        logger.info(f"[OOS] Filtered {len(all_dealers)} dealers down to {len(target_dealers)} dealers matching brand='{detected_brand}' (search terms={brand_search_terms})")
        
        # Lenovo specific consumer / commercial sub-filtering
        if detected_brand == "lenovo":
            model_name = (oos_doc.get("extracted_attributes", {}).get("model_name", "") or "").lower()
            
            # Map keywords to their target dealer categories
            lenovo_category_map = {
                "v14": "commercial",
                "v15": "commercial",
                "thinkbook": "commercial",
                "thinkpad": "commercial",
                "x1 carbon": "commercial",
                "loq": "consumer",
                "loq essential": "consumer",
                "legion": "consumer",
                "legion pro": "consumer",
                "ideapad slim 1": "consumer",
                "ideapad slim 3": "consumer",
                "ideapad slim 5": "consumer",
                "ideapad": "consumer",
                "yoga": "consumer",
            }
            
            # Collect unique categories matched by keywords in query or model name
            matched_categories = {
                category
                for kw, category in lenovo_category_map.items()
                if kw in raw_query or kw in model_name
            }
            
            if len(matched_categories) == 1:
                category = list(matched_categories)[0]
                target_dealers = [d for d in target_dealers if d.get("category") == category]
                logger.info(f"[OOS] Lenovo {category} inquiry detected. Filtered to {len(target_dealers)} dealers.")
            else:
                logger.info(f"[OOS] Lenovo inquiry category undetermined or mixed ({matched_categories}). Using all {len(target_dealers)} Lenovo dealers.")
    else:
        target_dealers = all_dealers
        logger.info(f"[OOS] No brand specified. Using all {len(target_dealers)} resolved dealers.")

    target_dealers = target_dealers[:MAX_DEALERS_PER_SEND]

    if not target_dealers:
        logger.warning(f"[OOS] No matching dealers found for brand/category in query_id={oos_doc['query_id']}. Creating escalation and setting status to 'no_dealers'")
        await async_db["oos_requests"].update_one(
            {"query_id": oos_doc["query_id"]},
            {"$set": {"status": "no_dealers"}}
        )
        # Escalation to team when no matching dealer exists in dealer_directory
        from services.team_service import create_ai_escalation
        inquiry_time = oos_doc.get("created_at", "")
        if isinstance(inquiry_time, datetime):
            inquiry_time = inquiry_time.astimezone(IST).strftime("%H:%M %d-%b-%Y")

        escalation_text = (
            f"Customer Number: {oos_doc.get('customer_number', 'N/A')}\n"
            f"Customer Inquiry: {oos_doc.get('raw_query_text', 'N/A')}\n"
            f"Inquiry Time: {inquiry_time}\n"
            f"Status: ESCALATED - No dealer found matching brand '{detected_brand or 'N/A'}' or requested category in dealer directory.\n"
        )

        await create_ai_escalation(org_id, {
            "user_name": oos_doc.get("customer_name", "Customer"),
            "customer_number": oos_doc.get("customer_number", ""),
            "customer_text": escalation_text,
            "inferred_intent": "no_dealer_found",
            "status": "pending_human_review",
            "role": "oos_no_dealer",
            "sender_name": "OOS System",
            "metadata": {
                "oos_query_id": oos_doc.get("query_id"),
                "detected_brand": detected_brand,
            }
        })
        return

    # Generate dealer message
    dealer_message = generate_dealer_message(oos_doc)
    logger.info(f"[OOS] Formatted dealer inquiry message:\n{dealer_message}")

    # Save target_dealers and message to the doc for tracking
    await async_db["oos_requests"].update_one(
        {"query_id": oos_doc["query_id"]},
        {"$set": {
            "target_dealers": [
                {"name": d.get("name", ""), "phone": d.get("phone", ""), "brands": d.get("brands", "")}
                for d in target_dealers
            ],
            "dealer_message": dealer_message,
        }}
    )

    # Reuse the sender bound to the source customer conversation.
    db = get_mongo_db()
    source_conversation = await asyncio.to_thread(
        db[f"conversations_{org_id}"].find_one,
        {"conversation_id": oos_doc.get("conversation_id")},
    )
    from services.wa_service import create_or_update_whatsapp_conversation, resolve_outbound_whatsapp_connection
    wa_connection = resolve_outbound_whatsapp_connection(
        org_id,
        (source_conversation or {}).get("whatsapp_id"),
        (source_conversation or {}).get("business_phone_id"),
    )
    wa_id = (wa_connection or {}).get("wa_id")
    if not wa_id:
        logger.error(f"[OOS] No WhatsApp connection found for org {org_id}, cannot send dealer messages")
        await async_db["oos_requests"].update_one(
            {"query_id": oos_doc["query_id"]},
            {"$set": {"status": "send_failed", "error": "no_wa_connection"}}
        )
        return

    logger.info(f"[OOS] Sending WhatsApp notifications to {len(target_dealers)} dealers using account WA_ID={wa_id}")

    sent_dealers = []
    sem = asyncio.Semaphore(5)  # max 5 concurrent WhatsApp sends

    async def _send_one(dealer: dict):
        dealer_phone = dealer.get("phone", "")
        dealer_name = dealer.get("name", "")
        if not dealer_phone:
            return
        async with sem:
            try:
                logger.info(f"[OOS] Sending WhatsApp message to dealer '{dealer_name}' at +{dealer_phone}...")
                dealer_conversation_id = await create_or_update_whatsapp_conversation(
                    org_id=org_id,
                    recipient_id=wa_id,
                    customer_phone_no=dealer_phone,
                    customer_name=dealer_name or dealer_phone,
                    last_message=dealer_message,
                    last_sender="ai",
                    mode="ai",
                    business_phone_id=wa_connection.get("phone_id"),
                    business_phone=wa_connection.get("phone"),
                )
                idempotency_key = f"oos_{oos_doc.get('query_id', uuid.uuid4().hex)}_{dealer_phone}_{uuid.uuid4().hex[:6]}"
                res = await send_whatsapp_message({
                    "conversation_id": dealer_conversation_id,
                    "content": dealer_message,
                    "sender_id": wa_id,
                    "recipient_override": dealer_phone,
                    "customer_name": dealer_name,
                    "idempotency_key": idempotency_key,
                }, org_id, "reply")
                message_ids = res.get("message_ids", []) if res else []
                logger.success(f"[OOS] WhatsApp successfully sent to dealer '{dealer_name}' at +{dealer_phone} (message_ids={message_ids})")
                sent_dealers.append({
                    "dealer_phone": dealer_phone,
                    "dealer_name": dealer_name,
                    "message_ids": message_ids,
                    "sent_at": datetime.now(timezone.utc),
                })
            except Exception as e:
                logger.error(f"[OOS] Failed to send message to dealer '{dealer_name}' at +{dealer_phone}: {e}")

    await asyncio.gather(*[_send_one(d) for d in target_dealers])

    # Update with results
    new_status = "dealers_contacted" if sent_dealers else "send_failed"
    update = {"$set": {"status": new_status}}
    if sent_dealers:
        update["$push"] = {"sent_dealers": {"$each": sent_dealers}}

    await async_db["oos_requests"].update_one(
        {"query_id": oos_doc["query_id"]},
        update
    )
    if sent_dealers:
        logger.success(f"[OOS] Contacted {len(sent_dealers)} dealers successfully. Query_id={oos_doc['query_id']} transitioned status to '{new_status}'")
    else:
        logger.error(f"[OOS] Failed to contact any dealers for query_id={oos_doc['query_id']}. Status set to 'send_failed'")


async def handle_dealer_reply(
    org_id: str,
    dealer_phone: str,
    content: str,
    message_context: Optional[dict] = None,
    create_escalation: bool = True,
    source_message_id: Optional[str] = None,
) -> bool:
    """
    Check if an incoming WhatsApp message is a dealer replying to an OOS request.
    Handles the case where customer and dealer are the SAME phone number.
    If matched:
      - Atomically append reply to prevent duplicates
      - Log to Google Sheet (Dealer Responses)
      - Create AI escalation with aggregated context
    Returns True if this was a dealer reply (should skip normal AI flow).
    """
    async_db = get_async_mongo_db()
    oos_col = async_db["oos_requests"]

    replied_message_id = message_context.get("id") if message_context else None

    logger.info(
        f"[OOS] handle_dealer_reply invoked for dealer=...{dealer_phone[-4:]}, "
        f"content_length={len(content or '')}, replied_message_id={replied_message_id}"
    )

    # Statuses where we expect dealer replies (only after message was actually sent)
    active_statuses = ["dealers_contacted", "partial_replies", "sending"]

    from services.pricing_service import is_supplier_response

    supplier_response = is_supplier_response(content)
    oos_doc = None
    # Reply-to correlation identifies the request, but the text still has to be
    # a supplier answer. A dealer may also use this number as a customer and
    # ask a question while quoting the request; that must continue to the
    # conversational dealer/customer router instead of being stored as a quote.
    if replied_message_id:
        logger.info(f"[OOS] Attempting match by replied message_id: {replied_message_id}")
        oos_doc = await oos_col.find_one({
            "org_id": org_id,
            "status": {"$in": active_statuses},
            "sent_dealers.message_ids": replied_message_id
        })
        if oos_doc:
            logger.success(f"[OOS] Match succeeded by message_id {replied_message_id} for query_id={oos_doc['query_id']}")
            if not supplier_response:
                logger.info(
                    f"[OOS] Reply to query_id={oos_doc['query_id']} is not a supplier answer; "
                    "falling back to the chatbot agent"
                )
                return False

    # Without reply-to correlation, a supplier-shaped answer and one unique
    # active request are both required. Otherwise normal customer questions
    # from a registered dealer would be swallowed by the OOS workflow.
    allow_phone_fallback = supplier_response
    if not oos_doc and allow_phone_fallback:
        logger.info(f"[OOS] Attempting unique match by dealer phone number: {dealer_phone}")
        matches = await oos_col.find(
            {
                "org_id": org_id,
                "status": {"$in": active_statuses},
                "$or": [
                    {"sent_dealers.dealer_phone": dealer_phone},
                    {"target_dealers.phone": dealer_phone},
                ],
            },
        ).sort("created_at", -1).limit(2).to_list(length=2)
        if len(matches) == 1:
            oos_doc = matches[0]
            logger.success(f"[OOS] Unique phone match succeeded for query_id={oos_doc['query_id']}")
        elif len(matches) > 1:
            logger.warning(f"[OOS] Ambiguous phone-only reply for dealer=...{dealer_phone[-4:]}; not assigning it")
            return False

    if not oos_doc:
        logger.info(f"[OOS] No active OOS request matched for dealer_phone={dealer_phone}. Falling back to normal chatbot agent.")
        return False

    # Get dealer name from sent_dealers
    dealer_name = ""
    for sd in oos_doc.get("sent_dealers", []):
        if sd.get("dealer_phone") == dealer_phone:
            dealer_name = sd.get("dealer_name", "")
            break

    reply_entry = {
        "dealer_phone": dealer_phone,
        "dealer_name": dealer_name,
        "content": content,
        "source_message_id": source_message_id,
        "replied_at": datetime.now(timezone.utc),
        "steps": {
            "pricing": "pending",
            "sheet": "pending",
            "escalation": "pending" if create_escalation else "done",
        },
    }

    # Atomic update: push reply and set status in one op
    # Accept any active status (covers race where reply arrives while still sending)
    result = await oos_col.update_one(
        {
            "org_id": org_id,
            "query_id": oos_doc["query_id"],
            "status": {"$in": active_statuses},
            **({"dealer_replies.source_message_id": {"$ne": source_message_id}} if source_message_id else {}),
        },
        {
            "$push": {"dealer_replies": reply_entry},
            "$set": {"status": "partial_replies"}
        }
    )

    if result.modified_count == 0:
        existing = source_message_id and await oos_col.find_one(
            {
                "org_id": org_id,
                "query_id": oos_doc["query_id"],
                "dealer_replies.source_message_id": source_message_id,
            },
        )
        if existing:
            oos_doc = existing
            reply_entry = next(
                reply
                for reply in oos_doc.get("dealer_replies", [])
                if reply.get("source_message_id") == source_message_id
            )
            await _complete_dealer_reply_steps(org_id, oos_doc, reply_entry)
            return True
        logger.warning(f"[OOS] Could not append reply for query_id={oos_doc['query_id']}, status may have changed")
        return False

    logger.success(f"[OOS] Dealer reply successfully logged in MongoDB. dealer={dealer_name} ({dealer_phone}), query_id={oos_doc['query_id']}")

    await _complete_dealer_reply_steps(org_id, oos_doc, reply_entry)
    return True


async def _complete_dealer_reply_steps(org_id: str, oos_doc: dict, reply: dict) -> None:
    """Resume the idempotent work attached to one stored dealer reply."""
    source_message_id = reply.get("source_message_id")
    if not source_message_id:
        return
    collection = get_async_mongo_db()["oos_requests"]

    async def claim(step: str) -> bool:
        now = datetime.now(timezone.utc)
        status_key = f"steps.{step}"
        claim_key = f"step_claimed_at.{step}"
        try:
            result = await collection.update_one(
                {
                    "org_id": org_id,
                    "query_id": oos_doc["query_id"],
                    "dealer_replies": {
                        "$elemMatch": {
                            "source_message_id": source_message_id,
                            "$or": [
                                {status_key: {"$in": ["pending", "failed"]}},
                                {status_key: {"$exists": False}},
                                {
                                    status_key: "processing",
                                    claim_key: {"$lt": now - timedelta(minutes=5)},
                                },
                            ],
                        }
                    },
                },
                {
                    "$set": {
                        f"dealer_replies.$.{status_key}": "processing",
                        f"dealer_replies.$.{claim_key}": now,
                    }
                },
            )
        except Exception as exc:
            logger.error(f"[OOS] Could not claim {step} reply work: {exc}")
            return False
        if result.modified_count == 1:
            reply.setdefault("steps", {})[step] = "processing"
            return True
        return False

    async def mark(step: str, status: str) -> None:
        try:
            result = await collection.update_one(
                {
                    "org_id": org_id,
                    "query_id": oos_doc["query_id"],
                    "dealer_replies": {
                        "$elemMatch": {
                            "source_message_id": source_message_id,
                            f"steps.{step}": "processing",
                        }
                    },
                },
                {
                    "$set": {f"dealer_replies.$.steps.{step}": status},
                    "$unset": {f"dealer_replies.$.step_claimed_at.{step}": ""},
                },
            )
        except Exception as exc:
            logger.error(f"[OOS] Could not finish {step} reply work: {exc}")
            return
        if result.modified_count == 1:
            reply.setdefault("steps", {})[step] = status

    steps = reply.setdefault("steps", {})
    if steps.get("pricing") != "done" and await claim("pricing"):
        try:
            from services.pricing_service import record_oos_supplier_reply

            await record_oos_supplier_reply(
                org_id=org_id,
                dealer_phone=reply.get("dealer_phone", ""),
                content=reply.get("content", ""),
                source_message_id=source_message_id,
                oos_doc=oos_doc,
            )
        except Exception as exc:
            logger.error(f"[OOS] Pricing observation failed: {exc}")
            await mark("pricing", "failed")
        else:
            await mark("pricing", "done")

    if steps.get("sheet") != "done" and await claim("sheet"):
        try:
            await log_dealer_response_to_sheet(
                org_id,
                oos_doc,
                reply.get("dealer_name", ""),
                reply.get("dealer_phone", ""),
                reply.get("content", ""),
                source_message_id,
            )
        except Exception as exc:
            logger.error(f"[OOS] Sheet logging failed: {exc}")
            await mark("sheet", "failed")
        else:
            await mark("sheet", "done")

    if steps.get("escalation") != "done" and await claim("escalation"):
        try:
            await create_oos_escalation(org_id, oos_doc, reply)
        except Exception as exc:
            logger.error(f"[OOS] Escalation creation failed: {exc}")
            await mark("escalation", "failed")
        else:
            await mark("escalation", "done")


async def log_dealer_response_to_sheet(
    org_id: str,
    oos_doc: dict,
    dealer_name: str,
    dealer_phone: str,
    response_text: str,
    source_message_id: str,
):
    """Append a row to the Dealer Responses Google Sheet."""
    from api.google_sheets_routes import refresh_google_token

    sheet_doc = _get_sheet_by_purpose(org_id, "dealer_responses")
    if not sheet_doc:
        logger.warning(f"[OOS] No 'dealer_responses' sheet connected for org {org_id}")
        return

    response_sheet_id = sheet_doc["sheet_id"]
    response_tab = sheet_doc.get("tab_name", "Responses")

    if not SHEET_ID_PATTERN.match(response_sheet_id):
        logger.error(f"[OOS] Invalid response sheet_id format")
        return

    try:
        access_token = await refresh_google_token(org_id)
    except Exception as e:
        logger.error(f"[OOS] Token refresh failed for response logging: {e}")
        raise

    now_ist = datetime.now(IST)
    valid_until = now_ist + timedelta(hours=OOS_TTL_HOURS)

    # Sanitize text fields to prevent formula injection
    row_data = [
        now_ist.strftime("%Y-%m-%d %H:%M"),
        oos_doc.get("query_id", ""),
        _sanitize_for_sheet(dealer_name),
        dealer_phone,
        _sanitize_for_sheet(oos_doc.get("raw_query_text", "")),
        _sanitize_for_sheet(response_text),
        "",  # price — human fills
        valid_until.strftime("%Y-%m-%d %H:%M"),
        source_message_id,
    ]

    encoded_tab = urllib.parse.quote(response_tab, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{response_sheet_id}"
        f"/values/{encoded_tab}!A:I:append"
        f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        existing = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{response_sheet_id}/values/{encoded_tab}!I:I",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if existing.status_code != 200:
            raise RuntimeError(f"OOS Sheet idempotency lookup failed: {existing.status_code}")
        if any(row and row[0] == source_message_id for row in existing.json().get("values", [])):
            return
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"values": [row_data]}
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"OOS Sheet append failed: {resp.status_code}")


async def create_oos_escalation(org_id: str, oos_doc: dict, new_reply: dict):
    """
    Create an AI escalation in the team chat with aggregated dealer responses.
    Uses the stale oos_doc (before this reply was pushed) + new_reply to avoid
    re-fetching and to correctly count.
    """
    from services.team_service import create_ai_escalation

    # oos_doc is from before the $push, so we manually append new_reply
    all_replies = list(oos_doc.get("dealer_replies", []))
    if not any(
        reply.get("source_message_id") == new_reply.get("source_message_id")
        for reply in all_replies
    ):
        all_replies.append(new_reply)

    dealer_lines = []
    for reply in all_replies:
        name = reply.get("dealer_name") or "Unknown"
        phone = reply.get("dealer_phone", "")
        text = reply.get("content", "")
        replied_at = reply.get("replied_at", "")
        if isinstance(replied_at, datetime):
            replied_at = replied_at.astimezone(IST).strftime("%H:%M %d-%b")
        dealer_lines.append(f"  - {name} ({phone}): \"{text}\" [{replied_at}]")

    # Show dealers who haven't responded yet
    sent_phones = {sd.get("dealer_phone") for sd in oos_doc.get("sent_dealers", [])}
    replied_phones = {r.get("dealer_phone") for r in all_replies}
    for sd in oos_doc.get("sent_dealers", []):
        if sd.get("dealer_phone") in (sent_phones - replied_phones):
            dealer_lines.append(f"  - {sd.get('dealer_name', 'Unknown')} ({sd.get('dealer_phone', '')}): (no response yet)")

    inquiry_time = oos_doc.get("created_at", "")
    if isinstance(inquiry_time, datetime):
        inquiry_time = inquiry_time.astimezone(IST).strftime("%H:%M %d-%b-%Y")

    escalation_text = (
        f"Customer Number: {oos_doc.get('customer_number', 'N/A')}\n"
        f"Customer Inquiry: {oos_doc.get('raw_query_text', 'N/A')}\n"
        f"Inquiry Time: {inquiry_time}\n"
        f"Dealer Responses:\n" + "\n".join(dealer_lines)
    )

    await create_ai_escalation(org_id, {
        "id": f"oos:{oos_doc.get('query_id')}:{new_reply.get('source_message_id')}",
        "user_name": oos_doc.get("customer_name", "Customer"),
        "customer_number": oos_doc.get("customer_number", ""),
        "customer_text": escalation_text,
        "inferred_intent": "dealer_pricing",
        "status": "pending_human_review",
        "role": "oos_dealer_response",
        "sender_name": "OOS System",
        "metadata": {
            "oos_query_id": oos_doc.get("query_id"),
            "dealer_phone": new_reply.get("dealer_phone"),
            "dealer_name": new_reply.get("dealer_name"),
            "dealer_reply_text": new_reply.get("content"),
            "total_replies": len(all_replies),
            "total_dealers_contacted": len(oos_doc.get("sent_dealers", [])),
        }
    })


async def process_pending_oos_reply_steps() -> None:
    """Retry stored dealer-reply side effects after a transient failure or restart."""
    cursor = get_async_mongo_db()["oos_requests"].find(
        {
            "dealer_replies": {
                "$elemMatch": {
                    "source_message_id": {"$exists": True, "$ne": None},
                    "$or": [
                        {"steps.pricing": {"$ne": "done"}},
                        {"steps.sheet": {"$ne": "done"}},
                        {"steps.escalation": {"$ne": "done"}},
                    ],
                }
            }
        }
    ).limit(50)
    async for document in cursor:
        org_id = document.get("org_id")
        if not org_id:
            continue
        for reply in document.get("dealer_replies", []):
            steps = reply.get("steps") or {}
            if reply.get("source_message_id") and any(
                steps.get(step) != "done" for step in ("pricing", "sheet", "escalation")
            ):
                await _complete_dealer_reply_steps(org_id, document, reply)


async def process_pending_oos_sends():
    """
    Scheduler job: pick up oos_requests with status='pending_send' and send them
    if we're now within business hours. Processes in batches.
    """
    if not is_within_dealer_hours():
        return

    async_db = get_async_mongo_db()
    cursor = async_db["oos_requests"].find(
        {"status": "pending_send"}
    ).limit(PENDING_SEND_BATCH)

    async for doc in cursor:
        org_id = doc.get("org_id", "")
        try:
            await send_oos_to_dealers(doc, org_id)
        except Exception as e:
            logger.error(f"[OOS] Error sending pending query_id={doc.get('query_id')}: {e}")


async def process_expired_oos_requests():
    """
    Scheduler job: find OOS requests that have expired (24h TTL) without full
    resolution, and create a 'no response' escalation.
    """
    from services.team_service import create_ai_escalation

    async_db = get_async_mongo_db()
    now = datetime.now(timezone.utc)

    cursor = async_db["oos_requests"].find({
        "status": {"$in": ["dealers_contacted", "pending_send", "sending", "partial_replies"]},
        "expires_at": {"$lte": now}
    }).limit(PENDING_SEND_BATCH)

    async for doc in cursor:
        org_id = doc.get("org_id", "")
        query_id = doc.get("query_id", "")

        try:
            replies = doc.get("dealer_replies", [])
            sent = doc.get("sent_dealers", [])

            if replies:
                await create_oos_escalation(org_id, doc, replies[-1])
            else:
                inquiry_time = doc.get("created_at", "")
                if isinstance(inquiry_time, datetime):
                    inquiry_time = inquiry_time.astimezone(IST).strftime("%H:%M %d-%b-%Y")

                escalation_text = (
                    f"Customer Number: {doc.get('customer_number', 'N/A')}\n"
                    f"Customer Inquiry: {doc.get('raw_query_text', 'N/A')}\n"
                    f"Inquiry Time: {inquiry_time}\n"
                    f"Status: NO DEALER RESPONDED within 24 hours\n"
                    f"Dealers contacted: {len(sent)}"
                )

                await create_ai_escalation(org_id, {
                    "user_name": doc.get("customer_name", "Customer"),
                    "customer_number": doc.get("customer_number", ""),
                    "customer_text": escalation_text,
                    "inferred_intent": "oos_no_response",
                    "status": "pending_human_review",
                    "role": "oos_expired",
                    "sender_name": "OOS System",
                    "metadata": {"oos_query_id": query_id}
                })

            # Mark as expired atomically
            await async_db["oos_requests"].update_one(
                {"query_id": query_id, "status": {"$ne": "expired"}},
                {"$set": {"status": "expired"}}
            )
        except Exception as e:
            logger.error(f"[OOS] Error expiring query_id={query_id}: {e}")


async def ensure_oos_indexes():
    """Create required indexes for oos_requests. Call once on app startup."""
    async_db = get_async_mongo_db()
    col = async_db["oos_requests"]
    await col.create_index([("org_id", 1), ("status", 1), ("sent_dealers.dealer_phone", 1)])
    await col.create_index([("org_id", 1), ("status", 1), ("sent_dealers.message_ids", 1)])
    await col.create_index([("org_id", 1), ("status", 1), ("target_dealers.phone", 1)])
    await col.create_index([("org_id", 1), ("conversation_id", 1), ("status", 1), ("extracted_attributes.brand", 1)])
    await col.create_index([("status", 1), ("expires_at", 1)])
    logger.info("[OOS] Indexes ensured on oos_requests")
