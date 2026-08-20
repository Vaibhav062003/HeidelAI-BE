"""
intent_agent_tools.py

Production-grade tools for the Purchase Intent background worker.
Called by intent_queue_worker.py when the AI agent detects purchase intent.

Responsibilities:
  - save_intent_to_sheet():    Append detected intent to "Purchase Intents" tab in org's Google Sheet
  - schedule_intent_followup(): Schedule a follow-up WhatsApp message via automation_scheduler

These are NOT Agno/LangChain tools — they are plain async Python functions
called directly by the worker after structured extraction.
"""

import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from loguru import logger
from zoneinfo import ZoneInfo

from database import get_mongo_db
from services.automation_scheduler import schedule_followup

IST = ZoneInfo("Asia/Kolkata")

# Sheet tab where purchase intents are logged
INTENT_TAB_NAME = "Purchase Intents"
INTENT_TAB_HEADERS = [
    "Detected At (IST)", "Customer Name", "Phone",
    "Product", "Tentative Date", "Confidence", "Conversation ID",
]

# Validate Google Sheet IDs (same pattern as oos_service.py)
import re
_SHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# =============================================================================
# GOOGLE SHEETS — Save Intent
# =============================================================================

async def save_intent_to_sheet(
    org_id: str,
    conversation_id: str,
    customer_name: str,
    customer_phone: str,
    product_name: str,
    tentative_date: str,
    confidence: str = "medium",
) -> bool:
    """
    Append a detected purchase intent row to the org's Google Sheet.

    Strategy: Find ANY active Google Sheet connection for the org.
    Auto-creates the "Purchase Intents" tab if it doesn't exist.

    Returns True on success, False on failure (never raises).
    """
    try:
        sheet_id, access_token = await _resolve_sheet_and_token(org_id)
        if not sheet_id or not access_token:
            return False

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            # Ensure the tab exists (idempotent)
            await _ensure_intent_tab(client, sheet_id, headers)

            # Append the row
            now_ist = datetime.now(IST)
            row_data = [
                now_ist.strftime("%Y-%m-%d %H:%M"),
                _sanitize(customer_name),
                customer_phone,
                _sanitize(product_name),
                tentative_date,
                confidence,
                conversation_id,
            ]

            encoded_tab = urllib.parse.quote(INTENT_TAB_NAME, safe="")
            url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
                f"/values/{encoded_tab}!A:G:append"
            )
            resp = await client.post(
                url,
                params={
                    "valueInputOption": "USER_ENTERED",
                    "insertDataOption": "INSERT_ROWS",
                },
                headers=headers,
                json={"values": [row_data]},
            )

            if resp.status_code in (200, 201):
                logger.info(
                    f"[IntentSheet] ✅ Saved intent for {conversation_id}: "
                    f"product={product_name}, date={tentative_date}"
                )
                return True

            logger.error(
                f"[IntentSheet] Sheet append failed: status={resp.status_code}, "
                f"body={resp.text[:200]}"
            )
            return False

    except Exception as e:
        logger.error(f"[IntentSheet] Failed to save intent for org={org_id}: {e}")
        return False


# =============================================================================
# FOLLOW-UP SCHEDULING
# =============================================================================

async def schedule_intent_followup(
    org_id: str,
    conversation_id: str,
    product_name: str,
    tentative_date_iso: str,
) -> Optional[str]:
    """
    Schedule a follow-up WhatsApp message using the existing automation_scheduler.

    Timing logic:
      - If tentative date is TODAY  → fire at the exact time mentioned
      - If tentative date is FUTURE → fire exactly 1 day prior at 10:00 AM IST
      - If tentative date is PAST   → fire in 5 minutes (graceful fallback)

    Returns schedule_id on success, None on failure (never raises).
    """
    try:
        # Cancel any previous intent follow-ups for this conversation.
        # This handles product changes ("Actually I want Galaxy Book instead")
        # and timeline updates ("I'll buy it at 6pm instead of 5pm").
        from services.automation_scheduler import cancel_conversation_followups as cancel_conv_followups
        cancelled = await asyncio.to_thread(
            cancel_conv_followups,
            org_id=org_id,
            conversation_id=conversation_id,
            reason="new_intent_detected",
        )
        if cancelled:
            logger.info(
                f"[IntentFollowup] Cancelled {cancelled} previous follow-up(s) "
                f"for {conversation_id} before scheduling new one"
            )

        target_dt = _parse_tentative_date(tentative_date_iso)
        if target_dt is None:
            logger.warning(
                f"[IntentFollowup] Could not parse date '{tentative_date_iso}' "
                f"for {conversation_id}"
            )
            return None

        now = datetime.now(timezone.utc)
        now_ist = now.astimezone(IST)
        target_ist = target_dt.astimezone(IST)

        if now_ist.date() == target_ist.date():
            # Same day: fire at the exact time mentioned
            fire_at = target_dt
        elif target_ist.date() > now_ist.date():
            # Future: fire 1 day prior at 10:00 AM IST
            day_before_ist = target_ist - timedelta(days=1)
            fire_at = day_before_ist.replace(
                hour=10, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)
        else:
            # Past date: fire in 5 minutes as graceful fallback
            fire_at = now + timedelta(minutes=5)

        delay_seconds = max(int((fire_at - now).total_seconds()), 60)  # minimum 60s

        # Sanitize product name for the message
        safe_product = product_name.strip() if product_name else "the product"

        message_config = {
            "text": f"Hi, are you still interested in {safe_product}?",
        }

        # schedule_followup is SYNC — wrap in to_thread
        schedule_id = await asyncio.to_thread(
            schedule_followup,
            org_id=org_id,
            flow_id="__purchase_intent_followup__",
            conversation_id=conversation_id,
            delay_seconds=delay_seconds,
            message_config=message_config,
            max_retries=3,
        )

        fire_at_ist = fire_at.astimezone(IST)
        logger.info(
            f"[IntentFollowup] ✅ Scheduled follow-up {schedule_id} for {conversation_id}: "
            f"product='{safe_product}', fires_at={fire_at_ist.strftime('%Y-%m-%d %H:%M IST')} "
            f"(in {delay_seconds}s)"
        )
        return schedule_id

    except Exception as e:
        logger.error(
            f"[IntentFollowup] Failed to schedule for {conversation_id}: {e}"
        )
        return None


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

async def _resolve_sheet_and_token(org_id: str) -> tuple:
    """
    Find the first active Google Sheet connection for the org and get a valid token.
    Returns (sheet_id, access_token) or (None, None) if unavailable.
    """
    db = get_mongo_db()

    # Handle org_ids that might have the 'org_' prefix in the database
    search_org_ids = [org_id, f"org_{org_id}"] if not org_id.startswith("org_") else [org_id, org_id.replace("org_", "")]
    
    # Try purpose-tagged sheets first, then fall back to any connected sheet
    sheet_doc = await asyncio.to_thread(
        db.google_sheets_connections.find_one,
        {"org_id": {"$in": search_org_ids}},
    )

    if not sheet_doc:
        # Fallback: check the google_sheets collection (used by product sync)
        sheet_doc = await asyncio.to_thread(
            db.google_sheets.find_one,
            {"org_id": {"$in": search_org_ids}, "status": "active"},
        )

    if not sheet_doc:
        logger.warning(f"[IntentSheet] No Google Sheet connected for org {org_id}")
        return None, None

    sheet_id = sheet_doc.get("sheet_id")
    if not sheet_id or not _SHEET_ID_RE.match(sheet_id):
        logger.error(f"[IntentSheet] Invalid sheet_id for org {org_id}")
        return None, None

    try:
        from api.google_sheets_routes import refresh_google_token
        access_token = await refresh_google_token(org_id)
        return sheet_id, access_token
    except Exception as e:
        logger.error(f"[IntentSheet] Token refresh failed for org {org_id}: {type(e).__name__}")
        return None, None


async def _ensure_intent_tab(client: httpx.AsyncClient, sheet_id: str, headers: dict) -> None:
    """
    Check if the 'Purchase Intents' tab exists; create it with headers if not.
    Idempotent — safe to call on every write.
    """
    try:
        resp = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}",
            headers=headers,
            params={"fields": "sheets.properties.title"},
        )
        if resp.status_code != 200:
            logger.warning(f"[IntentSheet] Could not fetch sheet metadata: {resp.status_code}")
            return

        sheets = resp.json().get("sheets", [])
        tab_exists = any(
            s.get("properties", {}).get("title") == INTENT_TAB_NAME
            for s in sheets
        )

        if tab_exists:
            return

        # Create the tab
        logger.info(f"[IntentSheet] Creating '{INTENT_TAB_NAME}' tab in sheet {sheet_id[:10]}...")
        create_resp = await client.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate",
            headers=headers,
            json={
                "requests": [{
                    "addSheet": {
                        "properties": {"title": INTENT_TAB_NAME}
                    }
                }]
            },
        )
        if create_resp.status_code not in (200, 201):
            logger.error(f"[IntentSheet] Failed to create tab: {create_resp.text[:200]}")
            return

        # Write header row
        encoded_tab = urllib.parse.quote(INTENT_TAB_NAME, safe="")
        await client.put(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
            f"/values/{encoded_tab}!A1:G1",
            params={"valueInputOption": "USER_ENTERED"},
            headers=headers,
            json={"values": [INTENT_TAB_HEADERS]},
        )
        logger.info(f"[IntentSheet] ✅ Tab '{INTENT_TAB_NAME}' created with headers")

    except Exception as e:
        logger.warning(f"[IntentSheet] Tab check/create failed (non-fatal): {e}")


def _sanitize(value: str) -> str:
    """Prevent formula injection in Google Sheets."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


def _parse_tentative_date(date_str: str) -> Optional[datetime]:
    """
    Parse an ISO 8601 date/datetime string into a timezone-aware datetime.
    Returns None if parsing fails.
    """
    if not date_str:
        return None

    try:
        # Handle various ISO formats
        cleaned = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            # Assume IST if naive (agent prompt specifies IST context)
            dt = dt.replace(tzinfo=IST)
        return dt
    except (ValueError, TypeError):
        pass

    # Try date-only format (e.g., "2026-08-25")
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        # Default to 10:00 AM IST for date-only
        dt = dt.replace(hour=10, minute=0, tzinfo=IST)
        return dt
    except (ValueError, TypeError):
        return None
