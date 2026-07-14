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


def generate_dealer_message(oos_doc: dict) -> str:
    """Generate the formatted inquiry message to send to dealers."""
    return oos_doc.get("raw_query_text", "")


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

    # Filter by brand if known
    if brand:
        brand_lower = brand.strip().lower()
        target_dealers = [
            d for d in all_dealers
            if brand_lower in [b.strip().lower() for b in d.get("brands", "").split(",")]
        ]
        logger.info(f"[OOS] Filtered {len(all_dealers)} dealers down to {len(target_dealers)} dealers matching brand='{brand}'")
        
        # Lenovo specific consumer / commercial sub-filtering
        if brand_lower == "lenovo":
            raw_query = (oos_doc.get("raw_query_text", "") or "").lower()
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
        logger.warning(f"[OOS] No dealers found to contact for query_id={oos_doc['query_id']}. Transitioning status to 'no_dealers'")
        await async_db["oos_requests"].update_one(
            {"query_id": oos_doc["query_id"]},
            {"$set": {"status": "no_dealers"}}
        )
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

    # Resolve WhatsApp Business Account ID
    db = get_mongo_db()
    wa_connection = await asyncio.to_thread(
        db.whatsapp_connections.find_one, {"org_id": org_id}
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
                res = await send_whatsapp_message({
                    "conversation_id": f"whatsapp_{dealer_phone}",
                    "content": dealer_message,
                    "sender_id": wa_id,
                    "recipient_override": dealer_phone,
                    "customer_name": dealer_name,
                }, org_id, "ai")
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


async def handle_dealer_reply(org_id: str, dealer_phone: str, content: str, message_context: Optional[dict] = None) -> bool:
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

    logger.info(f"[OOS] handle_dealer_reply invoked for dealer_phone={dealer_phone}, content='{content}', replied_message_id={replied_message_id}")

    # Statuses where we expect dealer replies (only after message was actually sent)
    active_statuses = ["dealers_contacted", "partial_replies", "sending"]

    # Try matching by message_id first
    oos_doc = None
    if replied_message_id:
        logger.info(f"[OOS] Attempting match by replied message_id: {replied_message_id}")
        oos_doc = await oos_col.find_one({
            "org_id": org_id,
            "status": {"$in": active_statuses},
            "sent_dealers.message_ids": replied_message_id
        })
        if oos_doc:
            logger.success(f"[OOS] Match succeeded by message_id {replied_message_id} for query_id={oos_doc['query_id']}")

    # Then by dealer phone in sent_dealers
    if not oos_doc:
        logger.info(f"[OOS] Attempting match by dealer phone number: {dealer_phone}")
        oos_doc = await oos_col.find_one(
            {
                "org_id": org_id,
                "status": {"$in": active_statuses},
                "sent_dealers.dealer_phone": dealer_phone
            },
            sort=[("created_at", -1)]
        )
        if oos_doc:
            logger.success(f"[OOS] Match succeeded by dealer_phone {dealer_phone} for query_id={oos_doc['query_id']}")

    # Also check if this phone is in target_dealers (covers the case where
    # the same number is both customer and dealer — message was sent to them
    # and they reply in the same thread before status transitions)
    if not oos_doc:
        logger.info(f"[OOS] Attempting match by target_dealers fallback: {dealer_phone}")
        oos_doc = await oos_col.find_one(
            {
                "org_id": org_id,
                "status": {"$in": active_statuses},
                "target_dealers.phone": dealer_phone
            },
            sort=[("created_at", -1)]
        )
        if oos_doc:
            logger.success(f"[OOS] Match succeeded by target_dealers fallback for phone {dealer_phone}, query_id={oos_doc['query_id']}")

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
        "replied_at": datetime.now(timezone.utc),
    }

    # Atomic update: push reply and set status in one op
    # Accept any active status (covers race where reply arrives while still sending)
    result = await oos_col.update_one(
        {
            "query_id": oos_doc["query_id"],
            "status": {"$in": active_statuses}
        },
        {
            "$push": {"dealer_replies": reply_entry},
            "$set": {"status": "partial_replies"}
        }
    )

    if result.modified_count == 0:
        logger.warning(f"[OOS] Could not append reply for query_id={oos_doc['query_id']}, status may have changed")
        return False

    logger.success(f"[OOS] Dealer reply successfully logged in MongoDB. dealer={dealer_name} ({dealer_phone}), query_id={oos_doc['query_id']}")

    # Log response to Google Sheet (non-blocking, don't fail the reply flow)
    try:
        await log_dealer_response_to_sheet(org_id, oos_doc, dealer_name, dealer_phone, content)
    except Exception as e:
        logger.error(f"[OOS] Sheet logging failed: {e}")

    # Create AI escalation with full context
    try:
        await create_oos_escalation(org_id, oos_doc, reply_entry)
    except Exception as e:
        logger.error(f"[OOS] Escalation creation failed: {e}")

    return True


async def log_dealer_response_to_sheet(org_id: str, oos_doc: dict, dealer_name: str, dealer_phone: str, response_text: str):
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
        return

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
    ]

    encoded_tab = urllib.parse.quote(response_tab, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{response_sheet_id}"
        f"/values/{encoded_tab}!A:H:append"
        f"?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"values": [row_data]}
        )
        if resp.status_code not in (200, 201):
            logger.error(f"[OOS] Sheet append failed: {resp.status_code}")


async def create_oos_escalation(org_id: str, oos_doc: dict, new_reply: dict):
    """
    Create an AI escalation in the team chat with aggregated dealer responses.
    Uses the stale oos_doc (before this reply was pushed) + new_reply to avoid
    re-fetching and to correctly count.
    """
    from services.team_service import create_ai_escalation

    # oos_doc is from before the $push, so we manually append new_reply
    all_replies = oos_doc.get("dealer_replies", []) + [new_reply]

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
    await col.create_index([("conversation_id", 1), ("status", 1), ("extracted_attributes.brand", 1)])
    await col.create_index([("status", 1), ("expires_at", 1)])
    logger.info("[OOS] Indexes ensured on oos_requests")
