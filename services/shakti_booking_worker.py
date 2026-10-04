"""
Shakti Motors — Booking Confirmation Worker (v2 — Fault-Tolerant)
================================================================
Processes Google Form submissions (Test Ride / Service) that are synced
to a Google Sheet and stored in MongoDB.  Sends a WhatsApp confirmation
to the customer's phone number.

Safety Guarantees:
  1. Sheet ID whitelist — only processes known booking form sheets.
  2. Activation-date gate — ignores all rows synced before first deployment.
  3. Phone cross-validation — only sends to phones with an active bot session.
  4. Deduplication — (org_id, sheet_id, row_number) prevents double-sends.
  5. Rate limit — max 1 confirmation per phone per 5 minutes.
  6. Audit log — every action logged with 14-day TTL.
  7. No startup scan — runs ONLY when the scheduler triggers a fresh sync.
  8. Graceful degradation — every DB/API call wrapped in try/except.

This module is called by google_sheets_scheduler._trigger_google_sheet_automations().
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger
from database import get_mongo_db, get_async_mongo_db

# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

SHAKTI_ORG_ID = "org_2zV2OOhodLocEnTybzR8t9MIYCE"

# ONLY process these specific Google Sheet IDs
SHAKTI_BOOKING_SHEET_IDS = {
    "1fkE1t0DfbyjBwPEfVp1JhL2046u0SFhSmyEfcKFwP0k",   # Service bookings
    "10oxcem2YBuRTxPpIiGocQJbWa9u--I7qKTz_HF7mItw",    # Test ride bookings
}

TRACKING_COLLECTION = "shakti_booking_tracking"
CONFIG_COLLECTION   = "shakti_config"
AUDIT_COLLECTION    = "shakti_audit_log"
TRACKING_TTL_DAYS   = 14

# Column header keywords — fuzzy match (case-insensitive, partial match)
NAME_KEYWORDS         = ["name", "customer name", "full name"]
PHONE_KEYWORDS        = ["phone", "mobile", "contact", "whatsapp"]
MODEL_KEYWORDS        = ["model", "bike", "vehicle"]
DATE_KEYWORDS         = ["date", "preferred date", "booking date", "appointment"]
OUTLET_KEYWORDS       = ["outlet", "showroom", "dealer", "location", "branch"]
TYPE_KEYWORDS         = ["type", "booking type", "service type", "purpose"]
REGISTRATION_KEYWORDS = ["registration", "reg no", "vehicle number", "plate"]
MILEAGE_KEYWORDS      = ["mileage", "odometer", "km", "kms driven"]
SERVICE_KIND_KEYWORDS = ["service kind", "repair type", "service category", "issue"]

db       = get_mongo_db()
async_db = get_async_mongo_db()


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _find_column(row_data: dict, keywords: list[str]) -> Optional[str]:
    """Find a column value by fuzzy-matching column headers against keywords."""
    for header, value in row_data.items():
        header_lower = header.strip().lower()
        for kw in keywords:
            if kw in header_lower:
                return str(value).strip() if value else ""
    return None


def _normalize_phone(raw: str) -> str:
    """Strips non-digits and prepends '91' for 10-digit Indian numbers.
    Returns empty string if unusable.
    Format: '91XXXXXXXXXX' (no plus) to match bot session phone format.
    """
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return ""
    if len(digits) == 10:
        return f"91{digits}"
    if digits.startswith("0") and len(digits) == 11:
        return f"91{digits[1:]}"
    # Strip leading '+' equivalent — phone already has country code
    return digits


def _get_wa_connection(org_id: str) -> Optional[dict]:
    """Get WhatsApp Business connection for the org (sync)."""
    try:
        from services.wa_service import resolve_outbound_whatsapp_connection
        org = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1})
        if not org or not org.get("wa_id"):
            return None
        return resolve_outbound_whatsapp_connection(org_id, org["wa_id"], None)
    except Exception as e:
        logger.error(f"[ShaktiBooking] Failed to get WA connection: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# CONFIRMATION MESSAGE
# ═══════════════════════════════════════════════════════════════════════

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
    name_part  = customer_name.title() if customer_name else "there"

    lines = [
        f"✅ *Booking Confirmed — {type_label}*",
        "",
        f"Hi {name_part}, your {type_label.lower()} booking has been received!",
        "",
    ]

    if model:
        lines.append(f"🏍️ *Bike:* {model}")
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

    lines.append("")

    if is_service:
        lines.append("Our service team will confirm your appointment shortly.")
    else:
        lines.append("Our team will reach out to confirm your test ride slot shortly.")

    lines.extend(["", "Type 'menu' to explore more options."])
    return "\n".join(lines)


async def _send_booking_confirmation(
    org_id: str,
    phone: str,
    customer_name: str,
    message_text: str,
    wa_connection: dict,
) -> dict:
    """Send a WhatsApp booking confirmation message to the customer."""
    from services.wa_service import (
        send_whatsapp_message,
        create_or_update_whatsapp_conversation,
    )

    wa_id        = wa_connection["wa_id"]
    phone_id     = wa_connection.get("phone_id")
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
            "content":         message_text,
            "sender_id":       wa_id,
        },
        org_id=org_id,
        mode="reply",
    )
    return result


# ═══════════════════════════════════════════════════════════════════════
# CORE — Called by the scheduler after every sheet sync
# ═══════════════════════════════════════════════════════════════════════

async def process_shakti_bookings(org_id: str, sheet_id: str):
    """
    Main entry point.  Called from _trigger_google_sheet_automations()
    in google_sheets_scheduler.py after every sheet sync.

    Safety chain:
      1. org_id check → 2. sheet_id whitelist → 3. activation date gate →
      4. freshness filter → 5. per-row dedup → 6. rate limit →
      7. session cross-validation → 8. send → 9. track → 10. audit
    """
    # ── 1. Org check ──────────────────────────────────────────────────
    if org_id != SHAKTI_ORG_ID:
        return

    # ── 2. Sheet ID whitelist ─────────────────────────────────────────
    if sheet_id not in SHAKTI_BOOKING_SHEET_IDS:
        return

    try:
        tracking_col = db[TRACKING_COLLECTION]
        config_col   = db[CONFIG_COLLECTION]
        rows_col     = db[f"google_sheet_rows_{org_id}"]

        # ── 3. Activation date gate ──────────────────────────────────
        activation_doc = await asyncio.to_thread(
            lambda: config_col.find_one({"_id": "shakti_activation"})
        )
        now = datetime.now(timezone.utc)

        if not activation_doc:
            # First run ever — record activation date and skip ALL existing rows
            logger.info("[ShaktiBooking] First run — recording activation date and skipping historical rows")
            await asyncio.to_thread(
                lambda: config_col.insert_one({
                    "_id": "shakti_activation",
                    "activated_at": now,
                })
            )
            return

        activated_at = activation_doc["activated_at"]
        if activated_at.tzinfo is None:
            activated_at = activated_at.replace(tzinfo=timezone.utc)

        # ── 4. Fetch only rows synced AFTER activation ───────────────
        rows = await asyncio.to_thread(
            lambda: list(rows_col.find({
                "sheet_id": sheet_id,
                "synced_at": {"$gte": activated_at},
            }))
        )

        if not rows:
            return

        logger.info(f"[ShaktiBooking] Processing {len(rows)} row(s) from sheet {sheet_id}")

        # Log column headers from first row for debugging
        first_row_data = rows[0].get("row_data", {})
        logger.info(f"[ShaktiBooking] Sheet column headers: {list(first_row_data.keys())}")

        # ── Get WhatsApp connection once ─────────────────────────────
        wa_connection = await asyncio.to_thread(_get_wa_connection, SHAKTI_ORG_ID)
        if not wa_connection:
            logger.error(f"[ShaktiBooking] No WhatsApp connection for org {SHAKTI_ORG_ID}")
            return

        sent_count    = 0
        skipped_count = 0

        for row_doc in rows:
            try:
                row_number = row_doc.get("row_number")
                row_data   = row_doc.get("row_data", {})

                if not row_data or row_number is None:
                    skipped_count += 1
                    continue

                # ── Extract fields ───────────────────────────────────
                customer_name = _find_column(row_data, NAME_KEYWORDS) or ""
                raw_phone     = _find_column(row_data, PHONE_KEYWORDS) or ""
                model         = _find_column(row_data, MODEL_KEYWORDS) or ""
                preferred_date = _find_column(row_data, DATE_KEYWORDS) or ""
                outlet        = _find_column(row_data, OUTLET_KEYWORDS) or ""
                booking_type  = _find_column(row_data, TYPE_KEYWORDS) or ""
                registration  = _find_column(row_data, REGISTRATION_KEYWORDS) or ""
                mileage       = _find_column(row_data, MILEAGE_KEYWORDS) or ""
                service_kind  = _find_column(row_data, SERVICE_KIND_KEYWORDS) or ""

                # Auto-detect booking type if column is missing
                if not booking_type:
                    if registration or mileage or service_kind:
                        booking_type = "Service"
                    else:
                        booking_type = "Test Ride"

                phone = _normalize_phone(raw_phone)
                if not phone:
                    logger.warning(f"[ShaktiBooking] No valid phone in row {row_number}")
                    skipped_count += 1
                    continue

                # ── 5. Deduplication ─────────────────────────────────
                dedup_key = {
                    "org_id":     org_id,
                    "sheet_id":   sheet_id,
                    "row_number": row_number,
                }
                existing = await asyncio.to_thread(
                    lambda dk=dedup_key: tracking_col.find_one(dk)
                )
                if existing:
                    skipped_count += 1
                    continue

                # ── 6. Rate limit (1 per phone per 5 min) ───────────
                five_min_ago = now - timedelta(minutes=5)
                recent = await asyncio.to_thread(
                    lambda p=phone: tracking_col.find_one({
                        "phone": p,
                        "sent_at": {"$gte": five_min_ago},
                    })
                )
                if recent:
                    logger.warning(f"[ShaktiBooking] Rate limit: phone {phone} (row {row_number})")
                    skipped_count += 1
                    continue

                # ── 7. Session cross-validation ──────────────────────
                session_doc = await async_db[f"shakti_sessions_{org_id}"].find_one(
                    {"phone_number": phone}
                )
                if not session_doc:
                    logger.warning(
                        f"[ShaktiBooking] No bot session for phone {phone} — "
                        f"skipping row {row_number} to prevent unsolicited message"
                    )
                    # Audit the skip
                    await _audit_booking(
                        org_id, phone, "skipped_no_session", sheet_id,
                        row_number, booking_type, customer_name,
                    )
                    skipped_count += 1
                    continue

                # ── 8. Build & send confirmation ─────────────────────
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

                # ── 9. Track (dedup + rate limit) ────────────────────
                current_time = datetime.now(timezone.utc)
                expires_at   = current_time + timedelta(days=TRACKING_TTL_DAYS)
                await asyncio.to_thread(
                    lambda dk=dedup_key: tracking_col.update_one(
                        dk,
                        {"$set": {
                            "status":        "sent",
                            "sent_at":       current_time,
                            "expires_at":    expires_at,
                            "phone":         phone,
                            "customer_name": customer_name,
                            "booking_type":  booking_type,
                            "model":         model,
                        }},
                        upsert=True,
                    )
                )

                # ── 10. Audit ────────────────────────────────────────
                await _audit_booking(
                    org_id, phone, "booking_confirmation_sent", sheet_id,
                    row_number, booking_type, customer_name,
                )
                sent_count += 1

            except Exception as e:
                logger.error(f"[ShaktiBooking] Failed row {row_doc.get('row_number')}: {e}")
                await _audit_booking(
                    org_id, phone if 'phone' in dir() else "",
                    "booking_confirmation_failed", sheet_id,
                    row_doc.get("row_number"), booking_type if 'booking_type' in dir() else "",
                    customer_name if 'customer_name' in dir() else "",
                    error=str(e),
                )

        logger.info(
            f"[ShaktiBooking] Done — sent={sent_count}, skipped={skipped_count}, "
            f"total_rows={len(rows)}"
        )

    except Exception as e:
        logger.error(f"[ShaktiBooking] Critical error: {e}")


async def _audit_booking(
    org_id, phone, action, sheet_id, row_number, booking_type, customer_name, error=None
):
    """Write a booking audit record. Never raises."""
    now = datetime.now(timezone.utc)
    doc = {
        "org_id":        org_id,
        "phone":         phone,
        "action":        action,
        "sheet_id":      sheet_id,
        "row_number":    row_number,
        "booking_type":  booking_type,
        "customer_name": customer_name,
        "created_at":    now,
        "expires_at":    now + timedelta(days=TRACKING_TTL_DAYS),
    }
    if error:
        doc["error"] = error
    try:
        await async_db[AUDIT_COLLECTION].insert_one(doc)
    except Exception as e:
        logger.error(f"[ShaktiBooking] Audit log failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# INDEX SETUP — called from the scheduler
# ═══════════════════════════════════════════════════════════════════════

async def ensure_shakti_booking_indexes():
    """Create TTL and query indexes. Safe to call multiple times."""
    try:
        tracking = async_db[TRACKING_COLLECTION]
        await tracking.create_index(
            [("org_id", 1), ("sheet_id", 1), ("row_number", 1)],
            unique=True, name="dedup_idx",
        )
        await tracking.create_index(
            "expires_at", expireAfterSeconds=0, name="tracking_ttl",
        )
        await tracking.create_index(
            [("phone", 1), ("sent_at", -1)], name="rate_limit_idx",
        )
        logger.info("[ShaktiBooking] Indexes ensured")
    except Exception as e:
        logger.warning(f"[ShaktiBooking] Index creation (safe to ignore if exists): {e}")
