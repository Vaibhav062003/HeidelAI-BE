"""
Shakti Motors — Booking Confirmation Worker
============================================
Client-specific feature for Shakti Motors (Jawa/Yezdi/BSA dealership).

HOW IT WORKS (Event-Driven — NO polling):
1. Customer clicks "Book a Test Ride" or "Book a Service" in WhatsApp bot.
2. Bot sends a CTA URL linking to a Google Form.
3. Customer fills the form (fields: Name, Phone, Model, Preferred Date, Outlet).
4. Google Form auto-saves to a connected Google Sheet.
5. HeidelAI's Google Sheets scheduler detects the new row via webhook/reconciliation.
6. After sync, `_trigger_google_sheet_automations()` calls `process_shakti_bookings()`.
7. This worker reads new rows, deduplicates, and sends a WhatsApp booking confirmation
   back to the customer's phone number.

TO ENABLE:
  1. Set SHAKTI_ORG_ID to the client's HeidelAI org_id.
  2. Ask the client to connect their booking Google Sheet in HeidelAI.
  3. Ensure the Google Form saves to that same sheet.
  4. Column headers in the sheet must include variations of:
     "Name", "Phone/Mobile", "Model/Bike", "Date/Preferred Date", "Outlet/Showroom"

GOOGLE FORM SETUP:
  The Google Form should collect these fields (mapped to sheet columns):
  - Customer Name
  - Phone Number (with country code or 10-digit Indian)
  - Bike Model (dropdown: Jawa 42, Jawa 42 FJ, etc.)
  - Preferred Date
  - Preferred Outlet (dropdown: Ravet, Pimpri Chinchwad, Mundhwa, Baner)
  - Booking Type (Test Ride / Service)
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from database import get_mongo_db
from loguru import logger

db = get_mongo_db()

# =============================================================================
# CONSTANTS — Change SHAKTI_ORG_ID to the client's actual org_id
# =============================================================================
SHAKTI_ORG_ID = "org_2zV2OOhodLocEnTybzR8t9MIYCE"  # Must match shakti_bot_service.py

TRACKING_COLLECTION = "shakti_booking_tracking"

# Column header keywords — fuzzy match (case-insensitive, partial match)
NAME_KEYWORDS = ["name", "customer name", "full name"]
PHONE_KEYWORDS = ["phone", "mobile", "contact", "whatsapp"]
MODEL_KEYWORDS = ["model", "bike", "vehicle"]
DATE_KEYWORDS = ["date", "preferred date", "booking date", "appointment"]
OUTLET_KEYWORDS = ["outlet", "showroom", "dealer", "location", "branch"]
TYPE_KEYWORDS = ["type", "booking type", "service type", "purpose"]

# Service-specific columns
REGISTRATION_KEYWORDS = ["registration", "reg no", "vehicle number", "plate"]
MILEAGE_KEYWORDS = ["mileage", "odometer", "km", "kms driven"]
SERVICE_KIND_KEYWORDS = ["service kind", "repair type", "service category", "issue"]


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


def _get_wa_connection(org_id: str) -> Optional[dict]:
    """Get WhatsApp Business connection for the org."""
    from services.wa_service import resolve_outbound_whatsapp_connection
    org = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1})
    if not org or not org.get("wa_id"):
        return None
    return resolve_outbound_whatsapp_connection(org_id, org["wa_id"], None)


# =============================================================================
# CONFIRMATION MESSAGE SENDER
# =============================================================================

async def _send_booking_confirmation(
    org_id: str,
    phone: str,
    customer_name: str,
    message_text: str,
    wa_connection: dict,
) -> dict:
    """
    Send a WhatsApp booking confirmation message to the customer.
    Creates a conversation if one doesn't exist.
    """
    from services.wa_service import (
        send_whatsapp_message,
        create_or_update_whatsapp_conversation,
    )

    wa_id = wa_connection["wa_id"]
    phone_id = wa_connection.get("phone_id")
    business_phone = wa_connection.get("phone")

    # Step 1: Create or update conversation
    conversation_id = await create_or_update_whatsapp_conversation(
        org_id=org_id,
        recipient_id=wa_id,
        customer_phone_no=phone,
        customer_name=customer_name or phone,
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


def _build_confirmation_message(
    customer_name: str,
    booking_type: str,
    model: str,
    preferred_date: str,
    outlet: str,
    registration: str = "",
    mileage: str = "",
    service_kind: str = "",
) -> str:
    """Build a personalized booking confirmation WhatsApp message."""

    is_service = any(kw in booking_type.lower() for kw in ["service", "repair", "maintenance"])
    type_label = "Service" if is_service else "Test Ride"
    name_part = customer_name.title() if customer_name else "there"

    lines = [
        f"✅ *Booking Confirmed — {type_label}*",
        f"",
        f"Hi {name_part}, your {type_label.lower()} booking has been received!",
        f"",
    ]

    if model:
        lines.append(f"🏍️ *Bike:* {model}")

    # Service-specific fields
    if is_service:
        if registration:
            lines.append(f"🔢 *Reg No:* {registration}")
        if mileage:
            lines.append(f"📏 *Mileage:* {mileage}")
        if service_kind:
            lines.append(f"🔧 *Service Type:* {service_kind}")

    if preferred_date:
        lines.append(f"📅 *Preferred Date:* {preferred_date}")
    if outlet:
        lines.append(f"📍 *Outlet:* {outlet}")

    lines.append(f"")

    if is_service:
        lines.append(f"Our service team will confirm your appointment shortly.")
    else:
        lines.append(f"Our team will reach out to confirm your test ride slot shortly.")

    lines.extend([
        f"",
        f"Type 'menu' to explore more options.",
    ])

    return "\n".join(lines)


# =============================================================================
# CORE — Called by the sync pipeline after every sheet sync
# =============================================================================

async def process_shakti_bookings(org_id: str, sheet_id: str):
    """
    Main entry point. Called from:
      1. `_trigger_google_sheet_automations()` in google_sheets_scheduler.py
      2. `startup_shakti_booking_worker()` in lifespan.py (one-time catch-up)

    Reads all synced rows from MongoDB, checks for new (unprocessed) bookings,
    sends confirmation WhatsApp messages, and marks them as processed.
    """
    # Only process for Shakti Motors
    if org_id != SHAKTI_ORG_ID:
        return

    rows_col = db[f"google_sheet_rows_{org_id}"]
    tracking_col = db[TRACKING_COLLECTION]

    rows = await asyncio.to_thread(
        lambda: list(rows_col.find({"sheet_id": sheet_id}))
    )

    if not rows:
        return

    logger.info(f"[ShaktiBooking] Processing {len(rows)} row(s) from sheet {sheet_id}")

    # Log column headers from first row for debugging
    if rows:
        first_row_data = rows[0].get("row_data", {})
        logger.info(f"[ShaktiBooking] Sheet column headers: {list(first_row_data.keys())}")

    # Get WhatsApp connection once for all sends
    wa_connection = await asyncio.to_thread(_get_wa_connection, SHAKTI_ORG_ID)
    if not wa_connection:
        logger.error(f"[ShaktiBooking] No WhatsApp connection for org {SHAKTI_ORG_ID}")
        return

    sent_count = 0
    skipped_count = 0

    for row_doc in rows:
        row_number = row_doc["row_number"]
        row_data = row_doc.get("row_data", {})

        # --- Extract fields ---
        customer_name = _find_column(row_data, NAME_KEYWORDS) or ""
        raw_phone = _find_column(row_data, PHONE_KEYWORDS) or ""
        model = _find_column(row_data, MODEL_KEYWORDS) or ""
        preferred_date = _find_column(row_data, DATE_KEYWORDS) or ""
        outlet = _find_column(row_data, OUTLET_KEYWORDS) or ""
        booking_type = _find_column(row_data, TYPE_KEYWORDS) or ""

        # Service-specific fields
        registration = _find_column(row_data, REGISTRATION_KEYWORDS) or ""
        mileage = _find_column(row_data, MILEAGE_KEYWORDS) or ""
        service_kind = _find_column(row_data, SERVICE_KIND_KEYWORDS) or ""

        # Auto-detect booking type if column is missing:
        # If registration/mileage/service_kind columns have data, it's a Service form.
        if not booking_type:
            if registration or mileage or service_kind:
                booking_type = "Service"
            else:
                booking_type = "Test Ride"

        phone = _normalize_phone(raw_phone)
        if not phone:
            skipped_count += 1
            continue

        # --- Deduplication: Check if we already sent confirmation for this row ---
        dedup_key = {
            "org_id": org_id,
            "sheet_id": sheet_id,
            "row_number": row_number,
        }

        existing = await asyncio.to_thread(
            lambda dk=dedup_key: tracking_col.find_one(dk)
        )
        if existing and existing.get("status") == "sent":
            skipped_count += 1
            continue

        # --- Build and send confirmation ---
        message_text = _build_confirmation_message(
            customer_name=customer_name,
            booking_type=booking_type,
            model=model,
            preferred_date=preferred_date,
            outlet=outlet,
            registration=registration,
            mileage=mileage,
            service_kind=service_kind,
        )

        try:
            result = await _send_booking_confirmation(
                org_id=org_id,
                phone=phone,
                customer_name=customer_name,
                message_text=message_text,
                wa_connection=wa_connection,
            )
            logger.success(
                f"[ShaktiBooking] ✅ Sent confirmation to {phone} (row {row_number}). "
                f"message_id={result.get('message_id', 'N/A')}"
            )

            # Mark as sent
            await asyncio.to_thread(
                lambda dk=dedup_key: tracking_col.update_one(
                    dk,
                    {"$set": {
                        "status": "sent",
                        "sent_at": datetime.now(timezone.utc),
                        "phone": phone,
                        "customer_name": customer_name,
                        "booking_type": booking_type,
                        "model": model,
                    }},
                    upsert=True,
                )
            )
            sent_count += 1

        except Exception as e:
            logger.error(f"[ShaktiBooking] Failed to send to {phone} (row {row_number}): {e}")
            await asyncio.to_thread(
                lambda dk=dedup_key: tracking_col.update_one(
                    dk,
                    {"$set": {"status": "failed", "error": str(e)}},
                    upsert=True,
                )
            )

    logger.info(
        f"[ShaktiBooking] Done — sent={sent_count}, skipped={skipped_count}, "
        f"total_rows={len(rows)}"
    )


# =============================================================================
# STARTUP / SHUTDOWN — Called from lifespan.py
# =============================================================================

def _ensure_indexes():
    """Create indexes for tracking and deduplication."""
    col = db[TRACKING_COLLECTION]
    col.create_index(
        [("org_id", 1), ("sheet_id", 1), ("row_number", 1)],
        unique=True,
    )
    col.create_index("sent_at")


async def startup_shakti_booking_worker():
    """
    Called once at server boot from lifespan.py.
    Creates indexes and runs a one-time catch-up scan.
    """
    if SHAKTI_ORG_ID == "REPLACE_WITH_CLIENT_ORG_ID":
        logger.info("[ShaktiBooking] Skipped — SHAKTI_ORG_ID not configured.")
        return

    try:
        await asyncio.to_thread(_ensure_indexes)
    except Exception as e:
        logger.error(f"[ShaktiBooking] Index creation failed: {e}")

    # Run a catch-up scan for any connected sheet
    conn = await asyncio.to_thread(
        lambda: db.google_sheets_connections.find_one({
            "org_id": SHAKTI_ORG_ID,
        })
    )
    if conn:
        logger.info(f"[ShaktiBooking] Running startup catch-up scan for sheet {conn['sheet_id']}...")
        await process_shakti_bookings(SHAKTI_ORG_ID, conn["sheet_id"])
    else:
        logger.warning(
            f"[ShaktiBooking] No sheet connection found for org={SHAKTI_ORG_ID}. "
            f"Will process when a sheet is synced."
        )
