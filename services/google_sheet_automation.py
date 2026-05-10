"""
google_sheet_automation.py

Handles Google Sheet → WhatsApp automation flows.

Key behaviors:
  - On publish: sync sheet to MongoDB, then process all rows via the scheduler path
  - On new rows (via scheduler): send message to newly added numbers only
  - Deduplication: tracks sent numbers per (org_id, flow_id) in google_sheet_sent_numbers
  - Variables: {{phone_number}}, {{row.ColumnName}}, {{customer_name}} supported in message text
  - Uses DIRECT WhatsApp Cloud API calls (no bulk job overhead) for maximum speed
"""

import re
import json
import uuid as _uuid
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

from database import get_mongo_db
from loguru import logger
import asyncio
from pymongo.errors import DuplicateKeyError

db = get_mongo_db()

SENT_COLLECTION = "google_sheet_sent_numbers"

# Meta WhatsApp Cloud API version (shared with wa_service)
try:
    from services.wa_service import IG_VERSION
except ImportError:
    IG_VERSION = "v24.0"


# =============================================================================
# PUBLIC API
# =============================================================================

async def process_google_sheet_trigger(
    org_id: str,
    flow_id: str,
    trigger_config: dict,
    message_config: dict,
    rows_data: Optional[List[dict]] = None,
) -> dict:
    """
    Core engine for Google Sheet automations.
    Can be called with raw rows_data (list of dicts) or will fetch from API if None.
    """
    sheet_id = trigger_config.get("sheet_id")
    column_name = trigger_config.get("column_name")
    row_start = trigger_config.get("row_start")
    row_end = trigger_config.get("row_end")

    if not sheet_id or not column_name:
        logger.error(f"[GoogleSheetAuto] Missing sheet_id or column_name for flow {flow_id}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    message_text = message_config.get("text", "")
    has_template = bool(message_config.get("template_name"))
    
    if not has_template and (not message_text or not message_text.strip()):
        logger.error(f"[GoogleSheetAuto] Empty message_text and no template for flow {flow_id} — aborting")
        return {"sent": 0, "skipped": 0, "errors": 1}

    # 1. Get WhatsApp connection early
    wa_connection = await asyncio.to_thread(_get_wa_connection, org_id)
    if not wa_connection:
        logger.error(f"[GoogleSheetAuto] No WhatsApp connection found for org {org_id}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    wa_id = wa_connection.get("wa_id")
    phone_id = wa_connection.get("phone_id")
    wa_access_token = wa_connection.get("access_token")

    if not all([wa_id, phone_id, wa_access_token]):
        logger.error(f"[GoogleSheetAuto] Incomplete WA connection for org {org_id}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    # 2. Prepare data source (API vs provided rows)
    headers = []
    rows_to_process = []
    
    if rows_data is not None:
        # Source is MongoDB (list of dicts)
        rows_to_process = rows_data
    else:
        # Source is Google Sheets API (list of lists)
        try:
            from api.google_sheets_routes import refresh_google_token
            access_token = await refresh_google_token(org_id)
        except Exception as e:
            logger.error(f"[GoogleSheetAuto] Failed to get Google access token: {e}")
            return {"sent": 0, "skipped": 0, "errors": 1}

        sheet_doc = await asyncio.to_thread(db.google_sheets_connections.find_one, {"org_id": org_id, "sheet_id": sheet_id})
        tab_name = sheet_doc.get("tab_name", "Sheet1") if sheet_doc else "Sheet1"

        headers, header_row, col_offset = await _fetch_headers(sheet_id, tab_name, access_token)
        if not headers:
            logger.error(f"[GoogleSheetAuto] Could not fetch headers for sheet {sheet_id}")
            return {"sent": 0, "skipped": 0, "errors": 1}

        try:
            col_index = headers.index(column_name)
        except ValueError:
            logger.error(f"[GoogleSheetAuto] Column '{column_name}' not found in detected headers: {headers}")
            return {"sent": 0, "skipped": 0, "errors": 1}

        raw_rows = await _fetch_rows(
            sheet_id, tab_name, access_token, row_start, row_end,
            header_row=header_row, col_offset=col_offset,
        )
        for rv in raw_rows:
            if col_index < len(rv):
                row_dict = { (headers[i] if i < len(headers) else f"col_{i+1}"): (rv[i] if i < len(rv) else "") for i in range(len(headers)) }
                rows_to_process.append(row_dict)

    # 3. Ensure deduplication index exists
    await asyncio.to_thread(_ensure_sent_index)

    # 4. Build eligible recipients
    stats = {"sent": 0, "skipped": 0, "errors": 0}
    eligible: list = []

    for row_dict in rows_to_process:
        phone_raw = _sanitize_phone(str(row_dict.get(column_name, "")))
            
        if not phone_raw:
            continue

        if not await _try_claim_send(org_id, flow_id, phone_raw):
            stats["skipped"] += 1
            continue

        # Extract customer name from common column names in the sheet
        customer_name = (
            row_dict.get("Name")
            or row_dict.get("name")
            or row_dict.get("Full Name")
            or row_dict.get("full_name")
            or row_dict.get("Customer Name")
            or row_dict.get("customer_name")
            or phone_raw
        )

        if has_template:
            # Safely resolve variables inside the components structure recursively.
            # This avoids JSON parsing errors if variables contain quotes/backslashes.
            components = message_config.get("components", [])
            resolved_components = _resolve_recursive(components, {
                "phone_number": phone_raw,
                "customer_name": customer_name,
                "row": row_dict,
            })
            resolved_text = message_config.get("template_name")  # use template name as reference

            eligible.append({
                "phone_number": phone_raw,
                "resolved_text": resolved_text,
                "customer_name": customer_name,
                "is_template": True,
                "template_name": message_config.get("template_name"),
                "language_code": message_config.get("language_code", "en"),
                "components": resolved_components,
            })
        else:
            resolved_text = _resolve_variables(message_text, {
                "phone_number": phone_raw,
                "customer_name": customer_name,
                "row": row_dict,
            })
            eligible.append({
                "phone_number": phone_raw,
                "resolved_text": resolved_text,
                "customer_name": customer_name,
                "is_template": False,
            })

    if not eligible:
        return stats

    # 5. Send via centralized wa_service functions (handles API call + storage + WebSocket broadcast)
    from services.wa_service import (
        send_whatsapp_message,
        send_whatsapp_template_message,
        create_or_update_whatsapp_conversation,
    )
    sem = asyncio.Semaphore(20)  # 20 concurrent sends for high throughput

    async def _send_one(recipient):
        async with sem:
            phone = recipient["phone_number"]
            resolved_text = recipient["resolved_text"]
            customer_name = recipient.get("customer_name", phone)

            # Ensure conversation exists before sending — the centralized send functions
            # use find_one_and_update WITHOUT upsert, so new contacts from sheets would crash.
            # create_or_update_whatsapp_conversation uses upsert=True and handles name resolution.
            try:
                await create_or_update_whatsapp_conversation(
                    org_id=org_id,
                    recipient_id=wa_id,
                    customer_phone_no=phone,
                    customer_name=customer_name or phone,
                    last_message=resolved_text,
                    last_sender="agent",
                    mode="reply",
                )
            except Exception as e:
                logger.warning(f"[GoogleSheetAuto] Conversation pre-create for {phone}: {e}")

            msg_id = ""
            send_ok = False
            try:
                if recipient.get("is_template"):
                    # Build template dict matching what send_whatsapp_template_message expects
                    template_dict = {
                        "name": recipient.get("template_name"),
                        "language": {"code": recipient.get("language_code", "en")},
                    }
                    if recipient.get("components"):
                        template_dict["components"] = recipient.get("components")

                    result = await send_whatsapp_template_message(
                        recipient_id=phone,
                        template=template_dict,
                        wa_id=wa_id,
                        org_id=org_id,
                        temp_id=None,
                        mode="reply",
                        context_type=None,
                        context=None,
                    )
                    msg_id = result.get("message_id", "")
                    send_ok = True
                else:
                    # Build message dict matching what send_whatsapp_message expects
                    message_dict = {
                        "conversation_id": f"whatsapp_{phone}",
                        "content": resolved_text,
                        "sender_id": wa_id,
                    }
                    result = await send_whatsapp_message(
                        message=message_dict,
                        org_id=org_id,
                        mode="reply",
                    )
                    msg_id = result.get("message_id", "")
                    send_ok = True

            except Exception as e:
                logger.error(f"[GoogleSheetAuto] WA send failed for {phone}: {e}")


            if send_ok:
                # MARK SENT in deduplication collection IMMEDIATELY
                dedup_success = False
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(_mark_sent, org_id, flow_id, phone, sheet_id, msg_id)
                        dedup_success = True
                        break
                    except Exception as e:
                        logger.error(f"[GoogleSheetAuto] Dedup mark_sent failed ({attempt+1}/3) for {phone}: {e}")
                        await asyncio.sleep(0.3)

                if not dedup_success:
                    logger.error(f"[GoogleSheetAuto] CRITICAL: Dedup write permanently failed for {phone} — marking failed for retry")
                    try:
                        await asyncio.to_thread(_mark_failed, org_id, flow_id, phone, sheet_id)
                    except Exception as mark_err:
                        logger.error(f"[GoogleSheetAuto] Failed to mark {phone} as failed: {mark_err}")
                    return {"sent": 0, "errors": 1}

                # Contact upsert — the wa_service functions handle conversation + message storage,
                # but we still need to ensure the contact record exists
                try:
                    final_name = customer_name or phone
                    conversation_id_key = f"whatsapp_{phone}"
                    contacts_collection = db[f"contacts_{org_id}"]
                    await asyncio.to_thread(
                        contacts_collection.update_one,
                        {"phone_number": phone},
                        {
                            "$setOnInsert": {
                                "whatsapp_id": wa_id,
                                "full_name": final_name,
                                "phone_number": phone,
                                "country": None,
                                "profile_url": None,
                                "conversation_id": conversation_id_key,
                                "email": None,
                                "categories": [],
                                "labels": [],
                                "platform": "whatsapp",
                                "created_at": datetime.now(timezone.utc),
                                "source": "google_sheet_automation",
                            }
                        },
                        upsert=True,
                    )
                except Exception as e:
                    logger.warning(f"[GoogleSheetAuto] Contact upsert failed for {phone}: {e}")

                return {"sent": 1, "errors": 0}
            else:
                # Send failed. Move to a retryable failure state.
                try:
                    await asyncio.to_thread(_mark_failed, org_id, flow_id, phone, sheet_id)
                except Exception as e:
                    logger.error(f"[GoogleSheetAuto] Failed to mark {phone} as failed: {e}")
                return {"sent": 0, "errors": 1}

    # Fire all sends concurrently
    results = await asyncio.gather(
        *[asyncio.create_task(_send_one(r)) for r in eligible],
        return_exceptions=True,
    )

    for res in results:
        if isinstance(res, Exception):
            logger.error(f"[GoogleSheetAuto] Unexpected error in _send_one: {res}")
            stats["errors"] += 1
        elif res:
            stats["sent"] += res.get("sent", 0)
            stats["errors"] += res.get("errors", 0)

    logger.info(f"[GoogleSheetAuto] Flow {flow_id}: sent={stats['sent']}, skipped={stats['skipped']}, errors={stats['errors']}")
    return stats


async def process_new_sheet_rows(org_id: str, sheet_id: str, target_flow_id: str = None):
    """
    Reads ALL synced rows from MongoDB for the given sheet and passes them
    to active automation triggers. The automation engine's dedup system
    (google_sheet_sent_numbers) ensures each phone number only receives
    a message once per flow — so it's safe to pass all rows every time.

    If target_flow_id is provided, only that flow will be processed (faster).
    """
    triggers = await asyncio.to_thread(
        lambda: list(db.automation_triggers.find({
            "org_id": org_id,
            "trigger_type": "google_sheet",
            "status": "active",
            "filters.sheet_id": sheet_id,
        }))
    )

    if target_flow_id:
        triggers = [t for t in triggers if t.get("flow_id") == target_flow_id]

    if not triggers:
        return

    # Fetch synced rows for this sheet from MongoDB.
    rows_collection = db[f"google_sheet_rows_{org_id}"]

    for trigger in triggers:
        flow_id = trigger.get("flow_id")
        trigger_config = trigger.get("filters", {})
        message_config = trigger.get("message_config", {})
        message_text = trigger.get("message_text", "")
        
        # Apply row range from trigger config (1-based row numbers)
        row_start = trigger_config.get("row_start")
        row_end = trigger_config.get("row_end")

        # Build specific filter for this trigger's range
        trigger_row_filter = {"sheet_id": sheet_id}
        if row_start is not None or row_end is not None:
            trigger_row_filter["row_number"] = {}
            if row_start is not None:
                trigger_row_filter["row_number"]["$gte"] = int(row_start)
            if row_end is not None:
                trigger_row_filter["row_number"]["$lte"] = int(row_end)

        # Count rows for THIS trigger's specific range
        trigger_total_rows = await asyncio.to_thread(
            lambda f=trigger_row_filter: rows_collection.count_documents(f)
        )
        if trigger_total_rows == 0:
            logger.debug(f"[GoogleSheetAuto] No rows found in range {row_start}-{row_end} for flow {flow_id}")
            continue

        logger.info(f"[GoogleSheetAuto] Processing {trigger_total_rows} rows from DB for flow {flow_id} (Range: {row_start}-{row_end})")

        # If the trigger only has legacy message_text, wrap it
        if not message_config and message_text:
            message_config = {"text": message_text}

        # Safety: verify the flow still exists and is published before processing
        flow_exists = await asyncio.to_thread(
            lambda fid=flow_id: db[f"automation_flows_{org_id}"].find_one(
                {"flow_id": fid, "org_id": org_id},
                {"status": 1, "_id": 0}
            )
        )
        if not flow_exists:
            # Flow was deleted — clean up this orphaned trigger
            logger.warning(f"[GoogleSheetAuto] Flow {flow_id} no longer exists — deactivating orphaned trigger")
            await asyncio.to_thread(
                db.automation_triggers.delete_many,
                {"flow_id": flow_id, "org_id": org_id}
            )
            continue

        if flow_exists.get("status") != "published":
            # Flow is not published — skip
            logger.debug(f"[GoogleSheetAuto] Flow {flow_id} is not published (status={flow_exists.get('status')}) — skipping")
            continue

        if not message_config:
            message_config = await asyncio.to_thread(
                _get_flow_message_config,
                org_id,
                flow_id,
                trigger.get("start_node_id"),
            )

        if not message_config:
            continue

        # --- High-Performance Deduplication Optimization ---
        # Fetch all previously sent phone numbers for THIS flow in a single query.
        try:
            sent_records = await asyncio.to_thread(
                lambda: list(db[SENT_COLLECTION].find(
                    {"flow_id": flow_id, "org_id": org_id, "status": "sent"},
                    {"phone": 1, "_id": 0}
                ))
            )
            already_sent_set = {r["phone"] for r in sent_records if r.get("phone")}
            logger.info(f"[GoogleSheetAuto] Loaded {len(already_sent_set)} sent records into cache for flow {flow_id}")
        except Exception as e:
            logger.error(f"[GoogleSheetAuto] Failed to load dedup cache for flow {flow_id}: {e}")
            already_sent_set = set()

        # Process rows in batches to avoid materializing the entire list
        batch_size = 1000
        for skip in range(0, trigger_total_rows, batch_size):
            batch_docs = await asyncio.to_thread(
                lambda s=skip, b=batch_size, f=trigger_row_filter: list(rows_collection.find(
                    f,
                    {"row_data": 1, "_id": 0}
                ).sort("row_number", 1).skip(s).limit(b))
            )
            
            batch_data = [doc.get("row_data", {}) for doc in batch_docs if doc.get("row_data")]
            if not batch_data:
                break
                
            # Filter out rows that are ALREADY in our in-memory sent set.
            # This is extremely fast and avoids redundant database 'claim' attempts.
            filtered_batch_data = []
            skipped_in_cache = 0
            for row in batch_data:
                phone_clean = _sanitize_phone(str(row.get(trigger_config.get("column_name", ""), "")))
                
                if phone_clean in already_sent_set:
                    skipped_in_cache += 1
                else:
                    filtered_batch_data.append(row)
            
            if skipped_in_cache > 0:
                logger.debug(f"[GoogleSheetAuto] Skipped {skipped_in_cache} rows already present in dedup cache")

            if not filtered_batch_data:
                continue

            await process_google_sheet_trigger(
                org_id=org_id,
                flow_id=flow_id,
                trigger_config=trigger_config,
                message_config=message_config,
                rows_data=filtered_batch_data,
            )


def cancel_flow_sent_records(org_id: str, flow_id: str):
    """
    Remove sent number tracking for a specific flow.
    Can be used to reset tracking so re-publishing starts fresh.
    (Not automatically called on unpublish to prevent re-messaging).
    """
    result = db[SENT_COLLECTION].delete_many({"org_id": org_id, "flow_id": flow_id})
    logger.info(
        f"[GoogleSheetAuto] Cleared {result.deleted_count} sent records for flow {flow_id}"
    )


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

async def _store_sent_message(
    org_id: str,
    wa_id: str,
    phone: str,
    message_text: str,
    message_id: str,
    customer_name: Optional[str] = None,
):
    """
    Create/update WhatsApp conversation and store the sent message in the DB.
    Called after run_bulk_send_job confirms a successful send.

    Uses the SAME conversations_{org_id} collection as the inbox so messages
    appear in the unified conversation list.

    Name resolution priority:
      1. Keep existing conversation name if it's a real name (not a phone number)
      2. Use the name from the Google Sheet row data
      3. Fall back to the phone number
    """
    from services.wa_service import create_or_update_whatsapp_conversation, store_whatsapp_message
    from schemas.models import MessageRole

    # --- Determine the best customer_name to use ---
    conversation_id_key = f"whatsapp_{phone}"
    final_name = customer_name or phone

    # Check if this contact already has a conversation with a real name
    existing_convo = await asyncio.to_thread(
        db[f"conversations_{org_id}"].find_one,
        {"conversation_id": conversation_id_key},
        {"customer_name": 1},
    )
    if existing_convo:
        stored_name = existing_convo.get("customer_name", "")
        # If existing conversation has a real name (not just the phone number),
        # preserve it — don't overwrite "John Doe" with "919876543210"
        if stored_name and stored_name != phone:
            final_name = stored_name

    conversation_id = await create_or_update_whatsapp_conversation(
        org_id=org_id,
        recipient_id=wa_id,
        customer_phone_no=phone,
        customer_name=final_name,
        last_message=message_text,
        last_sender="agent",
        mode="reply",
    )

    # status="delivered" prevents store_whatsapp_message() returning early on "sending"
    await store_whatsapp_message(
        org_id=org_id,
        content=message_text,
        conversation_id=conversation_id,
        customer_phone_no=phone,
        type="text",
        payload=None,
        sender_name="Agent",
        recipient_id=wa_id,
        role=MessageRole.AGENT,
        mode="reply",
        message_id=message_id or f"gs_{_uuid.uuid4().hex}",
        raw_data=None,
        status="delivered",
    )

    # --- Upsert contact in the contacts collection ---
    # This ensures the contact appears in the contacts list with their name
    try:
        contacts_collection = db[f"contacts_{org_id}"]
        await asyncio.to_thread(
            contacts_collection.update_one,
            {"phone_number": phone},
            {
                "$setOnInsert": {
                    "whatsapp_id": wa_id,
                    "full_name": final_name,
                    "phone_number": phone,
                    "country": None,
                    "profile_url": None,
                    "conversation_id": conversation_id_key,
                    "email": None,
                    "categories": [],
                    "labels": [],
                    "platform": "whatsapp",
                    "created_at": datetime.now(timezone.utc),
                    "source": "google_sheet_automation",
                }
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[GoogleSheetAuto] Failed to upsert contact for {phone}: {e}")


async def _fetch_headers(sheet_id: str, tab_name: str, access_token: str) -> Tuple[List[str], int, int]:
    """
    Smart header detection: scans the first 20 rows to find the actual header row.
    Returns (headers_list, header_row_number, col_offset) where:
      - headers_list: list of non-empty header strings
      - header_row_number: 1-indexed row in the sheet (e.g., 3 = row 3)
      - col_offset: 0-indexed column where headers start (e.g., 1 = column B)

    This handles tables that don't start at A1 (e.g., Google Sheets Table objects
    which have a label row above headers, or tables starting at column B/C).
    """
    import urllib.parse
    encoded_tab = urllib.parse.quote(tab_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{encoded_tab}!A1:ZZ20"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            return ([], 1, 0)

    all_rows = resp.json().get("values", [])
    if not all_rows:
        return ([], 1, 0)

    # Find header row: first row with 2+ non-empty cells
    for row_idx, row in enumerate(all_rows):
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if len(non_empty) >= 2:
            col_offset = 0
            for cell in row:
                if cell and str(cell).strip():
                    break
                col_offset += 1
            headers = [str(cell).strip() if cell else "" for cell in row[col_offset:]]
            return (headers, row_idx + 1, col_offset)

    # Fallback: first row with any data
    for row_idx, row in enumerate(all_rows):
        non_empty = [cell for cell in row if cell and str(cell).strip()]
        if non_empty:
            col_offset = 0
            for cell in row:
                if cell and str(cell).strip():
                    break
                col_offset += 1
            headers = [str(cell).strip() if cell else "" for cell in row[col_offset:]]
            return (headers, row_idx + 1, col_offset)

    return ([], 1, 0)


async def _fetch_rows(
    sheet_id: str,
    tab_name: str,
    access_token: str,
    row_start: Optional[int],
    row_end: Optional[int],
    header_row: int = 1,
    col_offset: int = 0,
) -> list:
    """
    Fetch data rows from the sheet, starting from the row after the header.
    row_start/row_end are 1-indexed relative to data rows.
    header_row: the actual sheet row number where headers were found.
    col_offset: number of leading empty columns to skip in each data row.
    """
    # Data starts right after the header row
    data_first_row = header_row + 1
    sheet_start = data_first_row + ((row_start - 1) if row_start else 0)
    sheet_end = data_first_row + ((row_end - 1) if row_end else 9999)

    import urllib.parse
    encoded_tab = urllib.parse.quote(tab_name, safe="")
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{encoded_tab}!A{sheet_start}:ZZ{sheet_end}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            logger.error(f"[GoogleSheetAuto] Rows fetch error: {resp.text}")
            return []

    raw_rows = resp.json().get("values", [])
    if not raw_rows:
        return []

    # Strip leading columns to align data with headers
    if col_offset > 0:
        return [row[col_offset:] if col_offset < len(row) else [] for row in raw_rows]
    return raw_rows


def _get_wa_connection(org_id: str) -> Optional[dict]:
    """Get active WhatsApp connection for an org."""
    org = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1})
    if not org or not org.get("wa_id"):
        return None
    wa_id = org["wa_id"]
    return db.whatsapp_connections.find_one({"wa_id": wa_id, "org_id": org_id})


async def _try_claim_send(org_id: str, flow_id: str, phone: str) -> bool:
    """
    Atomic deduplication: Tries to claim a phone number as 'pending' for a flow.
    Returns True if the claim was successful (first time), False otherwise.
    Uses update_one with upsert=True and $setOnInsert to ensure atomicity.
    """
    def _sync_claim():
        try:
            now = datetime.now(timezone.utc)
            # If the record already exists, only allow re-claiming if it's "pending"
            # and stayed that way for more than 5 minutes (handling crash recovery)
            # or if it previously "failed" — failed sends should be retried.
            expiry_threshold = now - timedelta(minutes=5)

            # Step 1: Try to reclaim a stale "pending" OR a "failed" record
            result = db[SENT_COLLECTION].update_one(
                {
                    "org_id": org_id,
                    "flow_id": flow_id,
                    "phone": phone,
                    "$or": [
                        {"status": {"$exists": False}},
                        {"status": "pending", "updated_at": {"$lt": expiry_threshold}},
                        {"status": "failed"},
                    ]
                },
                {
                    "$set": {
                        "status": "pending",
                        "updated_at": now
                    }
                },
                upsert=False
            )
            if result.modified_count > 0:
                return True

            # Step 2: If no stale record was reclaimed, try to claim by inserting a new one.
            # Catch DuplicateKeyError if the record already exists (e.g., status is "sent").
            try:
                db[SENT_COLLECTION].insert_one({
                    "org_id": org_id,
                    "flow_id": flow_id,
                    "phone": phone,
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now
                })
                return True
            except DuplicateKeyError:
                # Already exists and not stale pending -> skip
                return False
        except Exception as e:
            logger.error(f"[GoogleSheetAuto] Error in _try_claim_send: {e}")
            return False

    return await asyncio.to_thread(_sync_claim)


def _mark_sent(org_id: str, flow_id: str, phone: str, sheet_id: str, message_id: str = ""):
    """Record that a message was sent to this phone number for this flow."""
    now = datetime.now(timezone.utc)
    update_fields = {
        "org_id": org_id,
        "flow_id": flow_id,
        "phone": phone,
        "sheet_id": sheet_id,
        "status": "sent",
        "sent_at": now,
        "updated_at": now,
    }
    if message_id:
        update_fields["message_id"] = message_id
    db[SENT_COLLECTION].update_one(
        {"org_id": org_id, "flow_id": flow_id, "phone": phone},
        {"$set": update_fields},
        upsert=True,
    )


def _mark_failed(org_id: str, flow_id: str, phone: str, sheet_id: str):
    """Mark a send attempt as failed so it can be retried on next run."""
    now = datetime.now(timezone.utc)
    db[SENT_COLLECTION].update_one(
        {"org_id": org_id, "flow_id": flow_id, "phone": phone},
        {
            "$set": {
                "status": "failed",
                "failed_at": now,
                "updated_at": now,
                "sheet_id": sheet_id,
            }
        },
        upsert=True,
    )


def _ensure_sent_index():
    """Create indexes for deduplication collection."""
    try:
        # Align with google_sheets_scheduler._ensure_indexes()
        db[SENT_COLLECTION].create_index(
            [("org_id", 1), ("flow_id", 1), ("phone", 1)],
            unique=True,
            name="dedup_sent_idx",
        )
        # Index for webhook status lookups by message_id
        db[SENT_COLLECTION].create_index(
            [("message_id", 1)],
            name="message_id_idx",
            sparse=True,
        )
    except Exception as e:
        logger.error(f"[GoogleSheetAuto] Failed to ensure sent index: {e}")


# =============================================================================
# STATUS TRACKING (called from webhook)
# =============================================================================

async def update_sheet_sent_status(org_id: str, message_id: str, status: str):
    """
    Propagate WhatsApp delivery status (delivered, read, failed) to the
    google_sheet_sent_numbers dedup collection.

    Called from webhook_routes.py when a status update arrives for a message.
    Only updates if the message_id exists in the collection (i.e., it was
    sent by a Google Sheet automation).
    """
    if not org_id or not message_id or status not in ("delivered", "read", "failed"):
        return

    def _sync_update():
        try:
            result = db[SENT_COLLECTION].update_one(
                {"org_id": org_id, "message_id": message_id},
                {
                    "$set": {
                        "delivery_status": status,
                        "delivery_status_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            if result.modified_count > 0:
                logger.info(f"[GoogleSheetAuto] Updated delivery status to '{status}' for message {message_id}")
        except Exception as e:
            logger.debug(f"[GoogleSheetAuto] Status update skipped for {message_id}: {e}")

    await asyncio.to_thread(_sync_update)


def _get_flow_message_config(
    org_id: str, flow_id: str, start_node_id: Optional[str]
) -> dict:
    """
    Walk flow nodes from start_node_id to find the first WhatsApp message config.

    Handles both node structures the frontend may produce:
      - node.config.content.text  (primary — used by execution engine)
      - node.content.text         (fallback / alternate serialisation)
    """
    if not start_node_id:
        return ""

    try:
        collection = db[f"automation_flows_{org_id}"]
        flow = collection.find_one({"flow_id": flow_id}, {"flow_data": 1})
        if not flow:
            return ""

        nodes = flow.get("flow_data", {}).get("nodes", {})
        connections = flow.get("flow_data", {}).get("connections", [])

        visited: set = set()
        queue: list = [start_node_id]

        while queue:
            node_id = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)

            node = nodes.get(node_id, {})
            if node.get("type") == "message":
                # Primary: node.config.content
                content = node.get("config", {}).get("content", {})
                # Fallback: node.content
                if not content:
                    content = node.get("content", {})
                if content:
                    logger.info(
                        f"[GoogleSheetAuto] Found message config in node {node_id} "
                        f"for flow {flow_id}"
                    )
                    return content

            next_ids = [c["target"] for c in connections if c.get("source") == node_id]
            queue.extend(next_ids)

    except Exception as e:
        logger.error(
            f"[GoogleSheetAuto] Error getting message config for flow {flow_id}: {e}"
        )

    return {}


def _sanitize_phone(phone_raw: str) -> str:
    """
    Robust sanitization: removes leading +, then strips ALL non-digits.
    Ensures Meta API compatibility and consistent deduplication keys.
    """
    if not phone_raw:
        return ""
    # Strip whitespace and leading +
    phone_clean = phone_raw.strip()
    if phone_clean.startswith("+"):
        phone_clean = phone_clean[1:]
    # Remove all remaining non-digits (including any + in the middle)
    return re.sub(r"[^\d]", "", phone_clean)


def _resolve_recursive(data, variables):
    """
    Recursively traverse a dict or list and resolve variables in all string values.
    """
    if isinstance(data, str):
        return _resolve_variables(data, variables)
    elif isinstance(data, list):
        return [_resolve_recursive(item, variables) for item in data]
    elif isinstance(data, dict):
        return {k: _resolve_recursive(v, variables) for k, v in data.items()}
    return data


def _resolve_variables(text: str, variables: dict) -> str:
    """
    Replace {{variable}} and {{row.ColumnName}} placeholders.

    Supported:
      {{phone_number}}      → the phone number being messaged
      {{customer_name}}     → Name column value (or phone if not found)
      {{row.ColumnName}}    → any column value by name
    """
    if not text:
        return text

    def replacer(match):
        key = match.group(1).strip()
        parts = key.split(".", 1)

        if len(parts) == 2 and parts[0] == "row":
            row_data = variables.get("row", {})
            return str(row_data.get(parts[1], match.group(0)))

        val = variables.get(key)
        return str(val) if val is not None else match.group(0)

    return re.sub(r"\{\{([^}]+)\}\}", replacer, text)