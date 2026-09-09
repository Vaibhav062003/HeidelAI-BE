"""
Doctor Patient Follow-up Worker
================================
Client-specific feature for Dr. Mhaske Hospital.

HOW IT WORKS (Event-Driven — NO polling):
1. Doctor adds/updates a patient row in Google Sheet ("1 - Master Tracker" tab).
2. Google webhook fires → existing sync engine updates `google_sheet_rows_{org_id}` in MongoDB.
3. After sync, `_trigger_google_sheet_automations()` is called (in google_sheets_scheduler.py).
4. We hook into that call via `process_doctor_followups(org_id, sheet_id)`.
5. For each row with a "Next Follow-up Date", we schedule a WhatsApp reminder
   1 day prior at 10:00 AM IST.

TO ENABLE FOR A CLIENT:
  Just replace DOCTOR_ORG_ID below with the client's actual org_id.
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from database import get_mongo_db
from core.logger import logger

db = get_mongo_db()
from services.automation_scheduler import schedule_followup, cancel_conversation_followups

# =============================================================================
# CONSTANTS — Change DOCTOR_ORG_ID to enable for a client
# =============================================================================
DOCTOR_ORG_ID = "2zV2OOhodLocEnTybzR8t9MIYCE"

SHEET_TAB_NAME = "1 - Master Tracker"

# Column headers (must match the Google Sheet exactly)
COL_PATIENT_NAME = "Patient Name"
COL_MOBILE_NO = "Mobile No"
COL_DIAGNOSIS = "Diagnosis"
COL_NEXT_FOLLOWUP_DATE = "Next Follow-up Date"

TRACKING_COLLECTION = "doctor_followup_tracking"
FLOW_ID = "__doctor_followup_reminder__"
IST = ZoneInfo("Asia/Kolkata")


# =============================================================================
# HELPERS
# =============================================================================

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

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


# =============================================================================
# STARTUP — Create indexes + run initial catch-up scan
# =============================================================================

def _ensure_indexes():
    """Create indexes for tracking and automatic TTL cleanup."""
    col = db[TRACKING_COLLECTION]
    # Unique index to prevent duplicate tracking per row
    col.create_index(
        [("org_id", 1), ("sheet_id", 1), ("row_number", 1)],
        unique=True,
    )
    # TTL index: delete records 7 days (604800 seconds) after the target appointment date
    col.create_index("target_date_utc", expireAfterSeconds=7 * 24 * 60 * 60)


async def startup_doctor_followup_worker():
    """
    Called once at server boot from lifespan.py.
    Creates indexes and runs an initial catch-up scan for any rows
    that may have been added while the server was down.
    """
    if DOCTOR_ORG_ID == "REPLACE_WITH_CLIENT_ORG_ID":
        logger.info("[DoctorFollowup] Skipped — DOCTOR_ORG_ID not configured.")
        return

    try:
        await asyncio.to_thread(_ensure_indexes)
    except Exception as e:
        logger.error(f"[DoctorFollowup] Index creation failed: {e}")

    # Find the sheet and run a one-time catch-up scan
    conn = await asyncio.to_thread(
        lambda: db.google_sheets_connections.find_one({
            "org_id": DOCTOR_ORG_ID,
            "tab_name": SHEET_TAB_NAME,
        })
    )
    if conn:
        logger.info("[DoctorFollowup] Running startup catch-up scan...")
        await process_doctor_followups(DOCTOR_ORG_ID, conn["sheet_id"])
    else:
        logger.warning(
            f"[DoctorFollowup] No sheet connection found for org={DOCTOR_ORG_ID}, "
            f"tab='{SHEET_TAB_NAME}'. Will process when a matching sheet is synced."
        )


async def shutdown_doctor_followup_worker():
    """Nothing to clean up — no background tasks or polling loops."""
    logger.info("[DoctorFollowup] Shutdown complete (no background tasks to cancel).")


# =============================================================================
# CORE — Called by the sync pipeline after every sheet sync
# =============================================================================

async def process_doctor_followups(org_id: str, sheet_id: str):
    """
    Main entry point. Called from:
      1. `_trigger_google_sheet_automations()` in google_sheets_scheduler.py (webhook-driven)
      2. `startup_doctor_followup_worker()` in lifespan.py (one-time catch-up)

    Reads all synced rows from MongoDB, checks for 'Next Follow-up Date',
    and schedules/updates WhatsApp reminders.
    """
    # Only process for the configured doctor client
    if org_id != DOCTOR_ORG_ID:
        return

    # Verify this is the correct sheet (the Master Tracker tab)
    conn = await asyncio.to_thread(
        lambda: db.google_sheets_connections.find_one({
            "org_id": org_id,
            "sheet_id": sheet_id,
            "tab_name": SHEET_TAB_NAME,
        })
    )
    if not conn:
        return

    rows_col = db[f"google_sheet_rows_{org_id}"]
    tracking_col = db[TRACKING_COLLECTION]
    now_utc = datetime.now(timezone.utc)
    now_ist = now_utc.astimezone(IST)

    rows = await asyncio.to_thread(
        lambda: list(rows_col.find({"sheet_id": sheet_id}))
    )

    scheduled_count = 0
    skipped_count = 0

    for row_doc in rows:
        row_number = row_doc["row_number"]
        row_data = row_doc.get("row_data", {})

        # --- Extract required fields ---
        patient_name = row_data.get(COL_PATIENT_NAME, "").strip()
        raw_phone = row_data.get(COL_MOBILE_NO, "")
        diagnosis = row_data.get(COL_DIAGNOSIS, "").strip()
        next_date_str = row_data.get(COL_NEXT_FOLLOWUP_DATE, "").strip()

        # Skip rows missing required data
        if not patient_name or not raw_phone or not next_date_str:
            continue

        phone = _normalize_phone(raw_phone)
        if not phone:
            continue

        target_date = _parse_sheet_date(next_date_str)
        if not target_date:
            logger.warning(f"[DoctorFollowup] Unparseable date '{next_date_str}' in row {row_number}")
            continue

        # Skip appointments that are firmly in the past
        if target_date.date() < now_ist.date():
            continue

        # --- Dedup check ---
        existing = await asyncio.to_thread(
            lambda: tracking_col.find_one({
                "org_id": org_id,
                "sheet_id": sheet_id,
                "row_number": row_number,
            })
        )

        # Same row, same date → already scheduled, skip
        if existing and existing.get("next_followup_date_str") == next_date_str:
            skipped_count += 1
            continue

        # --- Calculate fire time: 1 day prior at 10:00 AM IST ---
        day_before = target_date - timedelta(days=1)
        fire_at_ist = day_before.replace(hour=10, minute=0, second=0, microsecond=0)
        fire_at_utc = fire_at_ist.astimezone(timezone.utc)

        # If 10 AM yesterday already passed, use 5-min fallback (appointment is today/tomorrow)
        if fire_at_utc < now_utc:
            fire_at_utc = now_utc + timedelta(minutes=5)

        delay_seconds = max(int((fire_at_utc - now_utc).total_seconds()), 60)

        conversation_id = f"whatsapp_{phone}"
        message_text = (
            f"Hi {patient_name}, this is a reminder from Dr. Mhaske Hospital.\n"
            f"You have a follow-up visit scheduled for {next_date_str} regarding {diagnosis}.\n"
            f"Please visit the hospital as scheduled."
        )

        # --- If date changed, cancel old follow-up first ---
        if existing and existing.get("schedule_id"):
            try:
                await asyncio.to_thread(
                    cancel_conversation_followups,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    reason="appointment_rescheduled",
                    flow_id=FLOW_ID,
                )
                logger.info(f"[DoctorFollowup] Cancelled old follow-up for row {row_number} ({phone})")
            except Exception as e:
                logger.warning(f"[DoctorFollowup] Failed to cancel old follow-up for row {row_number}: {e}")

        # --- Schedule new follow-up ---
        try:
            schedule_id = await asyncio.to_thread(
                schedule_followup,
                org_id=org_id,
                flow_id=FLOW_ID,
                conversation_id=conversation_id,
                delay_seconds=delay_seconds,
                message_config={"text": message_text},
                max_retries=3,
            )
        except Exception as e:
            logger.error(f"[DoctorFollowup] Failed to schedule for row {row_number}: {e}")
            continue

        if schedule_id:
            fire_at_display = fire_at_utc.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
            logger.info(
                f"[DoctorFollowup] ✅ Scheduled {schedule_id} — "
                f"row={row_number}, phone={phone}, fires_at={fire_at_display}"
            )

            # Upsert tracking record
            await asyncio.to_thread(
                lambda: tracking_col.update_one(
                    {"org_id": org_id, "sheet_id": sheet_id, "row_number": row_number},
                    {
                        "$set": {
                            "patient_name": patient_name,
                            "patient_phone": phone,
                            "diagnosis": diagnosis,
                            "next_followup_date_str": next_date_str,
                            "target_date_utc": target_date.astimezone(timezone.utc),
                            "schedule_id": schedule_id,
                            "status": "scheduled",
                            "updated_at": now_utc,
                        },
                        "$setOnInsert": {"created_at": now_utc},
                    },
                    upsert=True,
                )
            )
            scheduled_count += 1

    if scheduled_count:
        logger.info(f"[DoctorFollowup] Processed {scheduled_count} new/updated follow-up(s), skipped {skipped_count} unchanged.")
