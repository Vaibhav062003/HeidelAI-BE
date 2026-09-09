"""
Doctor Patient Follow-up Worker
================================
Client-specific feature for Dr. Mhaske Hospital.

HOW IT WORKS (Event-Driven — NO polling):
1. Doctor adds/updates a patient row in Google Sheet ("1 - Master Tracker" tab).
2. Google webhook fires → existing sync engine updates `google_sheet_rows_{org_id}` in MongoDB.
3. After sync, `_trigger_google_sheet_automations()` is called (in google_sheets_scheduler.py).
4. We hook into that call via `process_doctor_followups(org_id, sheet_id)`.
5. For each row with a follow-up date, we schedule a WhatsApp reminder
   1 day prior at 10:00 AM IST.

TO ENABLE FOR A CLIENT:
  Just replace DOCTOR_ORG_ID below with the client's actual org_id.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from database import get_mongo_db
from loguru import logger

db = get_mongo_db()

# =============================================================================
# CONSTANTS — Change DOCTOR_ORG_ID to enable for a client
# =============================================================================
DOCTOR_ORG_ID = "org_2zV2OOhodLocEnTybzR8t9MIYCE"

SHEET_TAB_NAME = "1 - Master Tracker"

# Column header keywords — we match flexibly (case-insensitive, partial match)
# so the system works regardless of minor header name variations in the sheet.
PATIENT_NAME_KEYWORDS = ["patient name"]
MOBILE_NO_KEYWORDS = ["mobile", "phone", "contact"]
DIAGNOSIS_KEYWORDS = ["diagnosis"]
FOLLOWUP_DATE_KEYWORDS = ["next follow-up date", "next followup date", "next follow up date", "next follow"]

TRACKING_COLLECTION = "doctor_followup_tracking"
FLOW_ID = "__doctor_followup_reminder__"
IST = ZoneInfo("Asia/Kolkata")


# =============================================================================
# HELPERS
# =============================================================================

def _find_column(row_data: dict, keywords: list[str]) -> Optional[str]:
    """
    Find a column value by fuzzy-matching column headers against keywords.
    Returns the value (str) of the first matching column, or None.
    """
    for header, value in row_data.items():
        header_lower = header.strip().lower()
        for kw in keywords:
            if kw in header_lower:
                return str(value).strip() if value else ""
    return None


def _normalize_phone(raw: str) -> str:
    """
    Strips non-digits and prepends '91' for 10-digit Indian numbers.
    Returns empty string if the input is unusable.
    """
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return ""
    if len(digits) == 10:
        return f"91{digits}"
    if digits.startswith("0") and len(digits) == 11:
        return f"91{digits[1:]}"
    return digits


def _parse_sheet_date(date_str: str) -> Optional[datetime]:
    """
    Parses common date formats found in Indian Google Sheets.
    Returns a timezone-aware datetime in IST, or None.
    """
    date_str = str(date_str).strip()
    if not date_str:
        return None

    for fmt in (
        "%d/%m/%Y",    # 25/12/2026
        "%d-%m-%Y",    # 25-12-2026
        "%d.%m.%Y",    # 09.09.2026  (dot-separated)
        "%Y-%m-%d",    # 2026-12-25
        "%d %b %Y",    # 25 Dec 2026
        "%d %B %Y",    # 25 December 2026
        "%d/%m/%y",    # 25/12/26
        "%d-%m-%y",    # 25-12-26
        "%d.%m.%y",    # 09.09.26
        "%m/%d/%Y",    # 12/25/2026 (US format, fallback)
    ):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _get_wa_connection(org_id: str) -> Optional[dict]:
    """
    Get WhatsApp Business connection for the org.
    Returns dict with wa_id, phone_id, access_token, phone.
    """
    from services.wa_service import resolve_outbound_whatsapp_connection
    org = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1})
    if not org or not org.get("wa_id"):
        return None
    return resolve_outbound_whatsapp_connection(org_id, org["wa_id"], None)


# =============================================================================
# DIRECT MESSAGE SENDER — bypasses scheduler, sends immediately or scheduled
# =============================================================================

async def _send_doctor_followup_message(
    org_id: str,
    phone: str,
    patient_name: str,
    message_text: str,
    wa_connection: dict,
) -> dict:
    """
    Send a WhatsApp message directly to a patient's number.
    Creates a conversation if one doesn't exist (just like google_sheet_automation does).
    """
    from services.wa_service import (
        send_whatsapp_message,
        create_or_update_whatsapp_conversation,
    )

    wa_id = wa_connection["wa_id"]
    phone_id = wa_connection.get("phone_id")
    business_phone = wa_connection.get("phone")

    # Step 1: Create or update conversation (upsert=True handles new contacts)
    conversation_id = await create_or_update_whatsapp_conversation(
        org_id=org_id,
        recipient_id=wa_id,
        customer_phone_no=phone,
        customer_name=patient_name or phone,
        last_message=message_text,
        last_sender="agent",
        mode="reply",
        business_phone_id=phone_id,
        business_phone=business_phone,
    )

    # Step 2: Send the message
    result = await send_whatsapp_message(
        message={
            "conversation_id": conversation_id,
            "content": message_text,
            "sender_id": wa_id,
        },
        org_id=org_id,
        mode="reply",
    )

    return result


# =============================================================================
# STARTUP — Create indexes + run initial catch-up scan
# =============================================================================

def _ensure_indexes():
    """Create indexes for tracking and automatic TTL cleanup."""
    col = db[TRACKING_COLLECTION]
    col.create_index(
        [("org_id", 1), ("sheet_id", 1), ("row_number", 1)],
        unique=True,
    )
    col.create_index("target_date_utc", expireAfterSeconds=7 * 24 * 60 * 60)


async def startup_doctor_followup_worker():
    """
    Called once at server boot from lifespan.py.
    Creates indexes, starts the background firing loop,
    and runs an initial catch-up scan.
    """
    global _firing_task

    if DOCTOR_ORG_ID == "REPLACE_WITH_CLIENT_ORG_ID":
        logger.info("[DoctorFollowup] Skipped — DOCTOR_ORG_ID not configured.")
        return

    try:
        await asyncio.to_thread(_ensure_indexes)
    except Exception as e:
        logger.error(f"[DoctorFollowup] Index creation failed: {e}")

    # Start the background loop that fires scheduled messages
    _firing_task = asyncio.create_task(_firing_loop())
    logger.info("[DoctorFollowup] Background firing loop started.")

    # Run a one-time catch-up scan for any connected sheet for this org
    conn = await asyncio.to_thread(
        lambda: db.google_sheets_connections.find_one({
            "org_id": DOCTOR_ORG_ID,
        })
    )
    if conn:
        logger.info(f"[DoctorFollowup] Running startup catch-up scan for sheet {conn['sheet_id']}...")
        await process_doctor_followups(DOCTOR_ORG_ID, conn["sheet_id"])
    else:
        logger.warning(
            f"[DoctorFollowup] No sheet connection found for org={DOCTOR_ORG_ID}. "
            f"Will process when a sheet is synced."
        )


_firing_task: Optional[asyncio.Task] = None


async def shutdown_doctor_followup_worker():
    """Cancel the background firing loop."""
    global _firing_task
    if _firing_task and not _firing_task.done():
        _firing_task.cancel()
        try:
            await _firing_task
        except asyncio.CancelledError:
            pass
    logger.info("[DoctorFollowup] Shutdown complete.")


# =============================================================================
# BACKGROUND LOOP — Fires scheduled messages when their time arrives
# =============================================================================

async def _firing_loop():
    """
    Lightweight loop that checks every 60 seconds for follow-ups ready to fire.
    When fire_at <= now, sends the message directly via WhatsApp API.
    """
    while True:
        try:
            await asyncio.sleep(60)
            await _fire_ready_followups()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[DoctorFollowup] Firing loop error: {e}")
            await asyncio.sleep(60)


async def _fire_ready_followups():
    """Find all tracking records with status=scheduled and fire_at <= now, and send them."""
    tracking_col = db[TRACKING_COLLECTION]
    now_utc = datetime.now(timezone.utc)

    ready_jobs = await asyncio.to_thread(
        lambda: list(tracking_col.find({
            "org_id": DOCTOR_ORG_ID,
            "status": "scheduled",
            "fire_at_utc": {"$lte": now_utc},
        }))
    )

    if not ready_jobs:
        return

    logger.info(f"[DoctorFollowup] Firing {len(ready_jobs)} ready follow-up(s)...")

    # Get WhatsApp connection once for all sends
    wa_connection = await asyncio.to_thread(_get_wa_connection, DOCTOR_ORG_ID)
    if not wa_connection:
        logger.error(f"[DoctorFollowup] No WhatsApp connection for org {DOCTOR_ORG_ID} — cannot send!")
        return

    for job in ready_jobs:
        phone = job.get("patient_phone", "")
        patient_name = job.get("patient_name", "")
        message_text = job.get("message_text", "")
        row_number = job.get("row_number", "?")

        try:
            result = await _send_doctor_followup_message(
                org_id=DOCTOR_ORG_ID,
                phone=phone,
                patient_name=patient_name,
                message_text=message_text,
                wa_connection=wa_connection,
            )
            logger.success(
                f"[DoctorFollowup] ✅ Sent to {phone} (row {row_number}). "
                f"message_id={result.get('message_id', 'N/A')}"
            )

            # Mark as sent
            await asyncio.to_thread(
                lambda: tracking_col.update_one(
                    {"_id": job["_id"]},
                    {"$set": {"status": "sent", "sent_at": datetime.now(timezone.utc)}}
                )
            )
        except Exception as e:
            logger.error(f"[DoctorFollowup] Failed to send to {phone} (row {row_number}): {e}")
            # Mark as failed so it doesn't retry forever
            await asyncio.to_thread(
                lambda: tracking_col.update_one(
                    {"_id": job["_id"]},
                    {"$set": {"status": "failed", "error": str(e)}}
                )
            )


# =============================================================================
# CORE — Called by the sync pipeline after every sheet sync
# =============================================================================

async def process_doctor_followups(org_id: str, sheet_id: str):
    """
    Main entry point. Called from:
      1. `_trigger_google_sheet_automations()` in google_sheets_scheduler.py (webhook-driven)
      2. `startup_doctor_followup_worker()` in lifespan.py (one-time catch-up)

    Reads all synced rows from MongoDB, checks for follow-up dates,
    and schedules follow-ups in our tracking collection.
    The background _firing_loop() picks them up when it's time.
    """
    # Only process for the configured doctor client
    if org_id != DOCTOR_ORG_ID:
        return

    # Verify this is the correct sheet
    conn = await asyncio.to_thread(
        lambda: db.google_sheets_connections.find_one({
            "org_id": org_id,
            "sheet_id": sheet_id,
        })
    )
    if not conn:
        logger.warning(f"[DoctorFollowup] Sheet {sheet_id} not found in connections for org {org_id}")
        return

    actual_tab_name = conn.get("tab_name", "")
    if actual_tab_name != SHEET_TAB_NAME:
        logger.info(f"[DoctorFollowup] Processing sheet with tab '{actual_tab_name}' (expected '{SHEET_TAB_NAME}')")

    rows_col = db[f"google_sheet_rows_{org_id}"]
    tracking_col = db[TRACKING_COLLECTION]
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    rows = await asyncio.to_thread(
        lambda: list(rows_col.find({"sheet_id": sheet_id}))
    )

    logger.info(f"[DoctorFollowup] Processing {len(rows)} row(s) from sheet {sheet_id}")

    # Log the column headers from the first row to help debug column name mismatches
    if rows:
        first_row_data = rows[0].get("row_data", {})
        logger.info(f"[DoctorFollowup] Sheet column headers: {list(first_row_data.keys())}")

    scheduled_count = 0
    skipped_count = 0

    for row_doc in rows:
        row_number = row_doc["row_number"]
        row_data = row_doc.get("row_data", {})

        # --- Extract required fields using fuzzy column matching ---
        patient_name = _find_column(row_data, PATIENT_NAME_KEYWORDS) or ""
        raw_phone = _find_column(row_data, MOBILE_NO_KEYWORDS) or ""
        diagnosis = _find_column(row_data, DIAGNOSIS_KEYWORDS) or ""
        next_date_str = _find_column(row_data, FOLLOWUP_DATE_KEYWORDS) or ""

        # Skip rows missing required data (name + phone + followup date are required)
        if not patient_name or not raw_phone or not next_date_str:
            continue

        phone = _normalize_phone(raw_phone)
        if not phone:
            logger.warning(f"[DoctorFollowup] Row {row_number} — unusable phone: '{raw_phone}'")
            continue

        target_date = _parse_sheet_date(next_date_str)
        if not target_date:
            logger.warning(f"[DoctorFollowup] Row {row_number} — unparseable date: '{next_date_str}'")
            continue

        # Skip appointments that are firmly in the past
        if target_date.date() < now_ist.date():
            continue

        # --- Dedup check ---
        _rn = row_number  # capture for lambda
        existing = await asyncio.to_thread(
            lambda: tracking_col.find_one({
                "org_id": org_id,
                "sheet_id": sheet_id,
                "row_number": _rn,
            })
        )

        # Same row, same date → already scheduled/sent, skip
        if existing and existing.get("next_followup_date_str") == next_date_str:
            skipped_count += 1
            continue

        # --- Calculate fire time: 1 day prior at 10:00 AM IST ---
        day_before = target_date - timedelta(days=1)
        fire_at_ist = day_before.replace(hour=10, minute=0, second=0, microsecond=0)
        fire_at_utc = fire_at_ist.astimezone(timezone.utc)

        # If fire time already passed, send within 2 minutes
        if fire_at_utc < now_utc:
            fire_at_utc = now_utc + timedelta(minutes=2)

        message_text = (
            f"Hi {patient_name}, this is a reminder from Dr. Mhaske Hospital.\n"
            f"You have a follow-up visit scheduled for {next_date_str} regarding {diagnosis}.\n"
            f"Please visit the hospital as scheduled."
        )

        # --- If date changed, cancel old tracking (will be replaced by new upsert) ---
        if existing:
            logger.info(
                f"[DoctorFollowup] Date changed for row {row_number}: "
                f"'{existing.get('next_followup_date_str')}' → '{next_date_str}'"
            )

        # --- Upsert tracking record with fire_at_utc ---
        _td_utc = target_date.astimezone(timezone.utc)
        _nds = next_date_str
        _pn = patient_name
        _ph = phone
        _diag = diagnosis
        _fat = fire_at_utc
        _msg = message_text
        _now = now_utc

        await asyncio.to_thread(
            lambda: tracking_col.update_one(
                {"org_id": org_id, "sheet_id": sheet_id, "row_number": _rn},
                {
                    "$set": {
                        "patient_name": _pn,
                        "patient_phone": _ph,
                        "diagnosis": _diag,
                        "next_followup_date_str": _nds,
                        "target_date_utc": _td_utc,
                        "fire_at_utc": _fat,
                        "message_text": _msg,
                        "status": "scheduled",
                        "updated_at": _now,
                    },
                    "$setOnInsert": {"created_at": _now},
                },
                upsert=True,
            )
        )

        fire_at_display = fire_at_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        logger.info(
            f"[DoctorFollowup] 📋 Queued row={row_number}, phone={phone}, "
            f"followup={next_date_str}, fires_at={fire_at_display}"
        )
        scheduled_count += 1

    logger.info(
        f"[DoctorFollowup] Done — scheduled={scheduled_count}, "
        f"skipped_unchanged={skipped_count}, total_rows={len(rows)}"
    )
