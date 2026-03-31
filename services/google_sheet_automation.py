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
from typing import Optional

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
) -> dict:
    """
    Called on flow publish and by the scheduler when new rows are detected.

    Reads rows from the sheet, builds the deduplicated recipient list, then
    hands off to run_bulk_send_job (plain-text mode) for actual delivery.
    After bulk send, creates WhatsApp conversations + stores messages in DB.

    Args:
        org_id:         Organization ID
        flow_id:        Automation flow ID
        trigger_config: {sheet_id, column_name, sheet_name?, row_start?, row_end?}
        message_text:   WhatsApp message body (may contain {{variables}})

    Returns:
        {"sent": int, "skipped": int, "errors": int}
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

    # 1. Get Google access token
    try:
        from api.google_sheets_routes import refresh_google_token
        access_token = await refresh_google_token(org_id)
    except Exception as e:
        logger.error(f"[GoogleSheetAuto] Failed to get Google access token for org {org_id}: {e}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    # 2. Fetch headers (row 1) to find the column index
  
    sheet_doc = await asyncio.to_thread(
        db.google_sheets_connections.find_one, {"org_id": org_id, "sheet_id": sheet_id}
    )
    tab_name = sheet_doc.get("tab_name", "Sheet1") if sheet_doc else "Sheet1"

    headers = await _fetch_headers(sheet_id, tab_name, access_token)
    if not headers:
        logger.error(f"[GoogleSheetAuto] Could not fetch headers for sheet {sheet_id}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    try:
        col_index = headers.index(column_name)
    except ValueError:
        logger.error(
            f"[GoogleSheetAuto] Column '{column_name}' not found in headers: {headers}"
        )
        return {"sent": 0, "skipped": 0, "errors": 1}

    # 3. Fetch data rows within range
    rows_with_data = await _fetch_rows(sheet_id, tab_name, access_token, row_start, row_end)

    # 4. Get WhatsApp connection for this org
    wa_connection = await asyncio.to_thread(_get_wa_connection, org_id)
    if not wa_connection:
        logger.error(f"[GoogleSheetAuto] No WhatsApp connection found for org {org_id}")
        return {"sent": 0, "skipped": 0, "errors": 1}

    wa_id = wa_connection.get("wa_id")
    phone_id = wa_connection.get("phone_id")
    wa_access_token = wa_connection.get("access_token")

    if not all([wa_id, phone_id, wa_access_token]):
        logger.error(
            f"[GoogleSheetAuto] Incomplete WA connection for org {org_id} "
            f"(wa_id={wa_id}, phone_id={phone_id})"
        )
        return {"sent": 0, "skipped": 0, "errors": 1}

    # 5. Ensure deduplication index exists
    await asyncio.to_thread(_ensure_sent_index)

    # 6. Build eligible recipient list with pre-resolved text per recipient
    stats = {"sent": 0, "skipped": 0, "errors": 0}
    # Each entry: {"phone_number": str, "resolved_text": str}
    eligible: list = []

    for row_values in rows_with_data:
        if col_index >= len(row_values):
            continue

        phone_raw = str(row_values[col_index]).strip()
        if not phone_raw:
            continue

        phone = phone_raw
        if not phone:
            logger.warning(f"[GoogleSheetAuto] Invalid phone '{phone_raw}' — skipping")
            stats["skipped"] += 1
            continue

        # Atomic deduplication check:
        # Tries to insert a record for this (org, flow, phone).
        # Proceed only if it's the first time this combination is being processed.
        if not await _try_claim_send(org_id, flow_id, phone):
            stats["skipped"] += 1
            continue

        row_context = {
            (headers[i] if i < len(headers) else f"col_{i + 1}"): (
                row_values[i] if i < len(row_values) else ""
            )
            for i in range(len(headers))
        }

        resolved_text = _resolve_variables(message_text, {
            "phone_number": phone,
            "customer_name": row_context.get("Name", row_context.get("name", phone)),
            "row": row_context,
        })

        eligible.append({"phone_number": phone, "resolved_text": resolved_text})

    if not eligible:
        logger.info(
            f"[GoogleSheetAuto] Flow {flow_id}: no new recipients "
            f"(skipped={stats['skipped']})"
        )
        return stats

    logger.info(
        f"[GoogleSheetAuto] Flow {flow_id}: {len(eligible)} eligible recipient(s) "
        f"(skipped={stats['skipped']})"
    )

    # 7. Send via run_bulk_send_job.
    #    Each recipient gets its own job so the per-recipient resolved_text is used.
    #    run_bulk_send_job uses message_text param (plain-text mode) when provided.
    from services.wa_service import create_bulk_job, run_bulk_send_job, get_bulk_job

    # Use bounded concurrency to process multiple recipients in parallel
    concurrency_limit = 10 
    sem = asyncio.Semaphore(concurrency_limit)
    
    async def _send_one(recipient):
        async with sem:
            phone = recipient["phone_number"]
            resolved_text = recipient["resolved_text"]

            job_id = _uuid.uuid4().hex
            await asyncio.to_thread(
                create_bulk_job,
                org_id=org_id,
                job_id=job_id,
                template_name="",
                language_code="",
                total=1,
            )

            await run_bulk_send_job(
                job_id=job_id,
                org_id=org_id,
                phone_id=phone_id,
                access_token=wa_access_token,
                template_name="",
                language_code="",
                recipients=[Recipient(phone_number=phone)],
                components=None,
                delay_seconds=1.0,
                message_text=resolved_text,   # plain-text mode
            )

            # Read result from in-memory job store
            job = await asyncio.to_thread(get_bulk_job, job_id=job_id, org_id=org_id)
            send_succeeded = job and job.get("sent", 0) > 0

            if send_succeeded:
                message_id = ""
                results = job.get("results", [])
                if results:
                    message_id = results[0].get("message_id", "")

                try:
                    await _store_sent_message(
                        org_id=org_id,
                        wa_id=wa_id,
                        phone=phone,
                        message_text=resolved_text,
                        message_id=message_id,
                    )
                    stats["sent"] += 1
                    await asyncio.to_thread(_mark_sent, org_id, flow_id, phone, sheet_id)
                    logger.success(
                        f"[GoogleSheetAuto] ✅ Sent to {phone} | flow={flow_id} "
                        f"| msg_id={message_id}"
                    )
                except Exception as e:
                    stats["errors"] += 1
                    logger.error(f"[GoogleSheetAuto] DB storage failed for {phone}: {e}")
            else:
                stats["errors"] += 1
                error_detail = ""
                if job and job.get("results"):
                    error_detail = job["results"][0].get("error", "unknown")
                logger.error(f"[GoogleSheetAuto] Send failed for {phone}: {error_detail}")

    tasks = [asyncio.create_task(_send_one(recipient)) for recipient in eligible]
    await asyncio.gather(*tasks)

    logger.info(
        f"[GoogleSheetAuto] Flow {flow_id} complete: "
        f"sent={stats['sent']}, skipped={stats['skipped']}, errors={stats['errors']}"
    )
    return stats


async def process_new_sheet_rows(org_id: str, sheet_id: str):
    """
    Called by the Google Sheets scheduler after new rows are synced.
    Finds all published google_sheet flows that reference this sheet
    and sends messages to any new phone numbers.
    """
    triggers = list(db.automation_triggers.find({
        "org_id": org_id,
        "trigger_type": "google_sheet",
        "status": "active",
        "filters.sheet_id": sheet_id,
    }))

    if not triggers:
        return

    logger.info(
        f"[GoogleSheetAuto] {len(triggers)} active flow(s) to check for "
        f"new rows in sheet {sheet_id}"
    )

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
            logger.warning(
                f"[GoogleSheetAuto] No message text found for flow {flow_id} — skipping"
            )
            continue

        await process_google_sheet_trigger(
            org_id=org_id,
            flow_id=flow_id,
            trigger_config=trigger_config,
            message_text=message_text,
        )


def cancel_flow_sent_records(org_id: str, flow_id: str):
    """
    Remove sent number tracking when a flow is unpublished,
    so re-publishing starts fresh.
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
        db[SENT_COLLECTION].create_index(
            [("org_id", 1), ("flow_id", 1), ("phone", 1)],
            unique=True,
            name="dedup_idx",
        )
    except Exception:
        pass  # Index already exists


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