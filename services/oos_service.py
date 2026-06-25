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
    return WINDOW_START <= datetime.now(IST).time() <= WINDOW_END


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
    Read dealer directory rows from the connected 'dealer_directory' Google Sheet.
    Returns list of {name, phone, brands, category} dicts.
    """
    from api.google_sheets_routes import refresh_google_token

    if not sheet_id:
        sheet_doc = _get_sheet_by_purpose(org_id, "dealer_directory")
        if not sheet_doc:
            logger.warning(f"[OOS] No sheet with purpose='dealer_directory' for org {org_id}")
            return []
        sheet_id = sheet_doc["sheet_id"]
        tab_name = tab_name or sheet_doc.get("tab_name", "Sheet1")

    tab_name = tab_name or "Sheet1"

    if not SHEET_ID_PATTERN.match(sheet_id):
        logger.error(f"[OOS] Invalid sheet_id format: {sheet_id[:20]}")
        return []

    try:
        access_token = await refresh_google_token(org_id)
    except Exception as e:
        logger.error(f"[OOS] Failed to get Google token for org {org_id}: {e}")
        return []

    encoded_tab = urllib.parse.quote(tab_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{encoded_tab}!A1:Z1000"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code == 401:
            access_token = await refresh_google_token(org_id)
            resp = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})

    if resp.status_code != 200:
        logger.error(f"[OOS] Google Sheets API error: {resp.status_code}")
        return []

    rows = resp.json().get("values", [])
    if len(rows) < 2:
        return []

    headers = [h.strip().lower() for h in rows[0]]
    dealers = []
    for row in rows[1:]:
        row_dict = {}
        for i, header in enumerate(headers):
            row_dict[header] = row[i].strip() if i < len(row) and row[i] else ""
        if row_dict.get("phone"):
            dealers.append({
                "name": row_dict.get("name", ""),
                "phone": row_dict.get("phone", ""),
                "brands": row_dict.get("brands", ""),
                "category": row_dict.get("category", ""),
            })

    return dealers


def generate_dealer_message(oos_doc: dict) -> str:
    """Generate the formatted inquiry message to send to dealers."""
    attrs = oos_doc.get("extracted_attributes", {})
    parts = []
    if attrs.get("category"):
        parts.append(f"Category: {attrs['category'].strip().title()}")
    if attrs.get("brand"):
        parts.append(f"Brand: {attrs['brand'].strip().upper()}")
    if attrs.get("model_name"):
        parts.append(f"Model: {attrs['model_name'].strip()}")

    specs = attrs.get("specs", {})
    if specs:
        for k, v in specs.items():
            if v is None:
                continue
            label = k.replace("_", " ").title()
            if label.lower() == "ram gb":
                label, v = "RAM", f"{v}GB"
            elif label.lower() == "storage gb":
                label, v = "Storage", f"{v}GB"
            elif label.lower() == "budget max":
                label = "Max Budget"
                try:
                    v = f"Rs. {float(v):,.0f}"
                except (ValueError, TypeError):
                    v = f"Rs. {v}"
            parts.append(f"{label}: {v}")

    spec_summary = "\n".join(f"  - {p}" for p in parts)
    raw_query = oos_doc.get("raw_query_text", "")

    message = f"New Requirement Inquiry:\nQuery: \"{raw_query}\"\n"
    if parts:
        message += f"Specifications:\n{spec_summary}\n"
    message += "\nPlease reply with availability and pricing."
    return message


async def send_oos_to_dealers(oos_doc: dict, org_id: str):
    """
    Fetch dealers from Google Sheet, generate message, and send via WhatsApp.
    Only sends if within business hours (11 AM - 7:30 PM IST).
    Uses atomic status transition to prevent double-sends.
    """
    from services.wa_service import send_whatsapp_message

    if not is_within_dealer_hours():
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
        return  # already being processed

    # Fetch dealers from Google Sheet, filter by brand
    brand = oos_doc.get("extracted_attributes", {}).get("brand")
    all_dealers = await fetch_dealer_directory(org_id)

    # Filter by brand if known
    if brand:
        brand_lower = brand.strip().lower()
        target_dealers = [
            d for d in all_dealers
            if brand_lower in [b.strip().lower() for b in d.get("brands", "").split(",")]
        ]
    else:
        target_dealers = all_dealers

    target_dealers = target_dealers[:MAX_DEALERS_PER_SEND]

    if not target_dealers:
        await async_db["oos_requests"].update_one(
            {"query_id": oos_doc["query_id"]},
            {"$set": {"status": "no_dealers"}}
        )
        return

    # Generate dealer message
    dealer_message = generate_dealer_message(oos_doc)

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

    sent_dealers = []
    sem = asyncio.Semaphore(5)  # max 5 concurrent WhatsApp sends

    async def _send_one(dealer: dict):
        dealer_phone = dealer.get("phone", "")
        if not dealer_phone:
            return
        async with sem:
            try:
                res = await send_whatsapp_message({
                    "conversation_id": f"dealer_{dealer_phone}",
                    "content": dealer_message,
                    "sender_id": wa_id,
                    "recipient_override": dealer_phone,
                }, org_id, "ai")
                message_ids = res.get("message_ids", []) if res else []
                sent_dealers.append({
                    "dealer_phone": dealer_phone,
                    "dealer_name": dealer.get("name", ""),
                    "message_ids": message_ids,
                    "sent_at": datetime.now(timezone.utc),
                })
            except Exception as e:
                logger.error(f"[OOS] Failed to send to dealer {dealer_phone}: {e}")

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
        logger.info(f"[OOS] Contacted {len(sent_dealers)} dealers for query_id={oos_doc['query_id']}")


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

    # Statuses where we expect dealer replies (only after message was actually sent)
    active_statuses = ["dealers_contacted", "partial_replies", "sending"]

    # Try matching by message_id first
    oos_doc = None
    if replied_message_id:
        oos_doc = await oos_col.find_one({
            "org_id": org_id,
            "status": {"$in": active_statuses},
            "sent_dealers.message_ids": replied_message_id
        })

    # Then by dealer phone in sent_dealers
    if not oos_doc:
        oos_doc = await oos_col.find_one(
            {
                "org_id": org_id,
                "status": {"$in": active_statuses},
                "sent_dealers.dealer_phone": dealer_phone
            },
            sort=[("created_at", -1)]
        )

    # Also check if this phone is in target_dealers (covers the case where
    # the same number is both customer and dealer — message was sent to them
    # and they reply in the same thread before status transitions)
    if not oos_doc:
        oos_doc = await oos_col.find_one(
            {
                "org_id": org_id,
                "status": {"$in": active_statuses},
                "target_dealers.phone": dealer_phone
            },
            sort=[("created_at", -1)]
        )

    if not oos_doc:
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

    logger.info(f"[OOS] Dealer reply logged from {dealer_phone} for query_id={oos_doc['query_id']}")

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
