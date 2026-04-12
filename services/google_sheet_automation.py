"""
google_sheet_automation.py

Handles Google Sheet → WhatsApp automation flows.

Key behaviors:
  - On publish: read all rows in range, send WhatsApp message to each phone number ONCE
  - On new rows (via scheduler): send message to newly added numbers only
  - Deduplication: tracks sent numbers per (org_id, flow_id) in google_sheet_sent_numbers
  - Variables: {{phone_number}}, {{row.ColumnName}}, {{customer_name}} supported in message text
  - Uses run_bulk_send_job (plain-text mode via message_text param) for sending
"""

import re
import uuid as _uuid
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from database import get_mongo_db
from loguru import logger
import asyncio
from pymongo.errors import DuplicateKeyError
from collections import namedtuple

db = get_mongo_db()

Recipient = namedtuple("Recipient", ["phone_number"])

SENT_COLLECTION = "google_sheet_sent_numbers"


# =============================================================================
# PUBLIC API
# =============================================================================

async def process_google_sheet_trigger(
    org_id: str,
    flow_id: str,
    trigger_config: dict,
    message_text: str,
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

    if not message_text or not message_text.strip():
        logger.error(f"[GoogleSheetAuto] Empty message_text for flow {flow_id} — aborting")
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

        headers = await _fetch_headers(sheet_id, tab_name, access_token)
        if not headers:
            logger.error(f"[GoogleSheetAuto] Could not fetch headers for sheet {sheet_id}")
            return {"sent": 0, "skipped": 0, "errors": 1}

        try:
            col_index = headers.index(column_name)
        except ValueError:
            logger.error(f"[GoogleSheetAuto] Column '{column_name}' not found")
            return {"sent": 0, "skipped": 0, "errors": 1}

        raw_rows = await _fetch_rows(sheet_id, tab_name, access_token, row_start, row_end)
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
        phone_raw = str(row_dict.get(column_name, "")).strip()
        if not phone_raw:
            continue

        if not await _try_claim_send(org_id, flow_id, phone_raw):
            stats["skipped"] += 1
            continue

        resolved_text = _resolve_variables(message_text, {
            "phone_number": phone_raw,
            "customer_name": row_dict.get("Name", row_dict.get("name", phone_raw)),
            "row": row_dict,
        })
        eligible.append({"phone_number": phone_raw, "resolved_text": resolved_text})

    if not eligible:
        return stats

    # 5. Send bulk via existing logic
    from services.wa_service import create_bulk_job, run_bulk_send_job, get_bulk_job
    sem = asyncio.Semaphore(10)
    
    async def _send_one(recipient):
        async with sem:
            phone = recipient["phone_number"]
            resolved_text = recipient["resolved_text"]
            job_id = _uuid.uuid4().hex

            await asyncio.to_thread(create_bulk_job, org_id=org_id, job_id=job_id, template_name="", language_code="", total=1)
            await run_bulk_send_job(
                job_id=job_id, org_id=org_id, phone_id=phone_id, access_token=wa_access_token,
                template_name="", language_code="", recipients=[Recipient(phone_number=phone)],
                delay_seconds=1.0, message_text=resolved_text
            )

            job = await asyncio.to_thread(get_bulk_job, job_id=job_id, org_id=org_id)
            if job and job.get("sent", 0) > 0:
                # 1. MARK SENT in deduplication collection IMMEDIATELY (highest priority)
                try:
                    await asyncio.to_thread(_mark_sent, org_id, flow_id, phone, sheet_id)
                except Exception as e:
                    logger.error(f"[GoogleSheetAuto] CRITICAL: Failed to mark {phone} as sent in dedup table: {e}")
                    # Even if this fails, we proceed so we can try to store the message, 
                    # but the risk of re-send exists if the app crashes right now.

                # 2. Store in conversation history (lower priority bookkeeping)
                msg_id = ""
                if job.get("results"): 
                    msg_id = job["results"][0].get("message_id", "")
                
                try:
                    await _store_sent_message(org_id=org_id, wa_id=wa_id, phone=phone, message_text=resolved_text, message_id=msg_id)
                except Exception as e:
                    logger.error(f"[GoogleSheetAuto] Conversation storage failed for {phone}: {e}")
                
                return {"sent": 1, "errors": 0}
            else:
                return {"sent": 0, "errors": 1}

    results = await asyncio.gather(*[asyncio.create_task(_send_one(r)) for r in eligible])
    for res in results:
        if res:
            stats["sent"] += res.get("sent", 0)
            stats["errors"] += res.get("errors", 0)
    return stats


async def process_new_sheet_rows(org_id: str, sheet_id: str):
    """
    Optimized: Reads from MongoDB instead of hits Google Sheets API.
    Only processes rows synced within the last 15 minutes by default.
    Called by the scheduler after new rows are synced.
    """
    triggers = await asyncio.to_thread(
        lambda: list(db.automation_triggers.find({
            "org_id": org_id,
            "trigger_type": "google_sheet",
            "status": "active",
            "filters.sheet_id": sheet_id,
        }))
    )

    if not triggers:
        return

    # Fetch recently synced rows from MongoDB (last 15 minutes)
    # This avoids loading the entire sheet history into memory on every poll.
    min_synced_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    rows_collection = db[f"google_sheet_rows_{org_id}"]
    rows_docs = await asyncio.to_thread(
        lambda: list(rows_collection.find({
            "sheet_id": sheet_id,
            "synced_at": {"$gte": min_synced_at}
        }))
    )
    
    if not rows_docs:
        return

    # Extract the row_data dict from each document
    rows_data = [doc.get("row_data", {}) for doc in rows_docs]

    logger.info(f"[GoogleSheetAuto] Processing {len(rows_data)} rows from DB for {len(triggers)} triggers on sheet {sheet_id}")

    for trigger in triggers:
        flow_id = trigger.get("flow_id")
        trigger_config = trigger.get("filters", {})
        message_text = trigger.get("message_text", "")

        if not message_text:
            message_text = await asyncio.to_thread(
                _get_flow_message_text,
                org_id,
                flow_id,
                trigger.get("start_node_id"),
            )

        if not message_text:
            continue

        await process_google_sheet_trigger(
            org_id=org_id,
            flow_id=flow_id,
            trigger_config=trigger_config,
            message_text=message_text,
            rows_data=rows_data,  # Pass the pre-fetched data
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
):
    """
    Create/update WhatsApp conversation and store the sent message in the DB.
    Called after run_bulk_send_job confirms a successful send.
    """
    from services.wa_service import create_or_update_whatsapp_conversation, store_whatsapp_message
    from schemas.models import MessageRole

    conversation_id = await create_or_update_whatsapp_conversation(
        org_id=org_id,
        recipient_id=wa_id,
        customer_phone_no=phone,
        customer_name=phone,        # Updated when customer replies
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


async def _fetch_headers(sheet_id: str, tab_name: str, access_token: str) -> list:
    """Fetch the first row (headers) from the Google Sheet."""
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{tab_name}!A1:ZZ1"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            return []
        values = resp.json().get("values", [[]])
        return values[0] if values else []


async def _fetch_rows(
    sheet_id: str,
    tab_name: str,
    access_token: str,
    row_start: Optional[int],
    row_end: Optional[int],
) -> list:
    """
    Fetch data rows from the sheet (skipping header row 1).
    row_start/row_end are 1-indexed relative to data rows (row 2 = data row 1).
    """
    sheet_start = (row_start + 1) if row_start else 2
    sheet_end = (row_end + 1) if row_end else 10001

    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        f"/values/{tab_name}!A{sheet_start}:ZZ{sheet_end}"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15
        )
        if resp.status_code != 200:
            logger.error(f"[GoogleSheetAuto] Rows fetch error: {resp.text}")
            return []
        return resp.json().get("values", [])


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
            # and stayed that way for more than 1 hour (handling transient failures).
            expiry_threshold = now - timedelta(hours=1)
            
            # Step 1: Try to reclaim a stale "pending" record
            result = db[SENT_COLLECTION].update_one(
                {
                    "org_id": org_id, 
                    "flow_id": flow_id, 
                    "phone": phone,
                    "$or": [
                        {"status": {"$exists": False}}, 
                        {"status": "pending", "updated_at": {"$lt": expiry_threshold}}
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


def _mark_sent(org_id: str, flow_id: str, phone: str, sheet_id: str):
    """Record that a message was sent to this phone number for this flow."""
    now = datetime.now(timezone.utc)
    db[SENT_COLLECTION].update_one(
        {"org_id": org_id, "flow_id": flow_id, "phone": phone},
        {
            "$set": {
                "org_id": org_id,
                "flow_id": flow_id,
                "phone": phone,
                "sheet_id": sheet_id,
                "status": "sent",
                "sent_at": now,
                "updated_at": now
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
    except Exception as e:
        logger.error(f"[GoogleSheetAuto] Failed to ensure sent index: {e}")


def _get_flow_message_text(
    org_id: str, flow_id: str, start_node_id: Optional[str]
) -> str:
    """
    Walk flow nodes from start_node_id to find the first WhatsApp message text.

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
                # Primary: node.config.content.text
                text = node.get("config", {}).get("content", {}).get("text", "")
                # Fallback: node.content.text
                if not text:
                    text = node.get("content", {}).get("text", "")
                if text:
                    logger.info(
                        f"[GoogleSheetAuto] Found message text in node {node_id} "
                        f"for flow {flow_id}"
                    )
                    return text

            next_ids = [c["target"] for c in connections if c.get("source") == node_id]
            queue.extend(next_ids)

    except Exception as e:
        logger.error(
            f"[GoogleSheetAuto] Error getting message text for flow {flow_id}: {e}"
        )

    return ""


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