"""
automation_scheduler.py

Single-file WhatsApp follow-up automation scheduler.
Uses asyncio polling loop + MongoDB as source of truth.
No APScheduler, no Celery, no Redis.

Public API:
  - startup_scheduler()               → called from lifespan.py on app start
  - shutdown_scheduler()              → called from lifespan.py on app stop
  - schedule_followup(...)            → called from automation_service.py on publish
  - cancel_flow_followups(...)        → called from automation_service.py on unpublish/republish
  - cancel_conversation_followups(...)→ called from wa_service.py when customer replies
  - list_followups_for_flow(...)      → called from automation_routes.py
  - list_followups_for_org(...)       → called from automation_routes.py
  - cancel_followup(...)              → called from automation_routes.py
"""

import asyncio
import uuid
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from database import get_mongo_db
from services.utils import serialize_mongo
from pymongo import ReturnDocument
from loguru import logger


db = get_mongo_db()

# === Constants ===
COLLECTION = "automation_scheduled_followups"
POLL_INTERVAL_SECONDS = 30
STUCK_JOB_THRESHOLD_MINUTES = 10
MAX_BATCH_SIZE = 50

# Module-level task handle so we can cancel on shutdown
_polling_task: Optional[asyncio.Task] = None


# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================

async def startup_scheduler() -> None:
    """
    Called from lifespan.py on application startup.
    - Creates MongoDB indexes for the followups collection
    - Recovers any stuck jobs (status=executing from crashed processes)
    - Starts the background polling loop
    """
    global _polling_task

    await asyncio.to_thread(_ensure_indexes)
    await asyncio.to_thread(_recover_stuck_jobs)

    _polling_task = asyncio.create_task(_polling_loop())
    logger.success("WhatsApp follow-up scheduler started (poll interval = 30s).")


async def shutdown_scheduler() -> None:
    """
    Called from lifespan.py on application shutdown.
    Cancels the polling loop gracefully.
    """
    global _polling_task

    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass

    logger.success("WhatsApp follow-up scheduler stopped.")


# =============================================================================
# PUBLIC API — SCHEDULING
# =============================================================================

def schedule_followup(
    org_id: str,
    flow_id: str,
    conversation_id: str,
    delay_seconds: int,
    message_config: dict,
    max_retries: int = 3,
) -> str:
    """
    Insert a pending follow-up job into automation_scheduled_followups.

    Args:
        org_id:          Organization ID
        flow_id:         Automation flow ID
        conversation_id: e.g. "whatsapp_+919876543210"
        delay_seconds:   Seconds to wait before sending
        message_config:  {"text": "Hi {{customer_name}}..."}
        max_retries:     Max retry attempts on failure

    Returns:
        schedule_id (str)
    """
    now = datetime.now(timezone.utc)
    fire_at = now + timedelta(seconds=delay_seconds)

    schedule_id = f"sch_{uuid.uuid4().hex}"

    doc = {
        "schedule_id": schedule_id,
        "org_id": org_id,
        "flow_id": flow_id,
        "conversation_id": conversation_id,
        "message_config": message_config,
        "delay_seconds": delay_seconds,
        "fire_at": fire_at,
        "status": "pending",
        "retry_count": 0,
        "max_retries": max_retries,
        "created_at": now,
        "executed_at": None,
        "completed_at": None,
        "cancelled_at": None,
        "cancel_reason": None,
        "error": None,
    }

    db[COLLECTION].insert_one(doc)
    logger.info(f"Scheduled follow-up {schedule_id} for {conversation_id} firing at {fire_at.isoformat()}")
    return schedule_id


# =============================================================================
# PUBLIC API — CANCELLATION
# =============================================================================

def cancel_flow_followups(org_id: str, flow_id: str, reason: str) -> int:
    """
    Cancel all pending follow-up jobs for a given flow.
    Called on unpublish or re-publish.

    Returns:
        Number of jobs cancelled
    """
    now = datetime.now(timezone.utc)

    result = db[COLLECTION].update_many(
        {
            "org_id": org_id,
            "flow_id": flow_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancel_reason": reason,
            }
        },
    )

    count = result.modified_count
    if count:
        logger.info(f"Cancelled {count} follow-up job(s) for flow {flow_id} — reason: {reason}")
    return count


def cancel_conversation_followups(org_id: str, conversation_id: str, reason: str) -> int:
    """
    Cancel all pending follow-up jobs for a specific conversation.
    Called when a customer sends a new message.

    Returns:
        Number of jobs cancelled
    """
    now = datetime.now(timezone.utc)

    result = db[COLLECTION].update_many(
        {
            "org_id": org_id,
            "conversation_id": conversation_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancel_reason": reason,
            }
        },
    )

    count = result.modified_count
    if count:
        logger.info(
            f"Cancelled {count} follow-up job(s) for conversation {conversation_id} — reason: {reason}"
        )
    return count


def cancel_followup(schedule_id: str, org_id: str) -> bool:
    """
    Cancel a single follow-up job by schedule_id (org-scoped for safety).

    Returns:
        True if cancelled, False if not found / already terminal
    """
    now = datetime.now(timezone.utc)

    result = db[COLLECTION].update_one(
        {
            "schedule_id": schedule_id,
            "org_id": org_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "cancelled",
                "cancelled_at": now,
                "cancel_reason": "manual_cancel",
            }
        },
    )

    return result.modified_count > 0


# =============================================================================
# PUBLIC API — LISTING
# =============================================================================

def list_followups_for_flow(
    org_id: str,
    flow_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict]:
    """List follow-up jobs for a specific flow."""
    query: dict = {"org_id": org_id, "flow_id": flow_id}
    if status:
        query["status"] = status

    docs = list(
        db[COLLECTION]
        .find(query, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_serialize_followup(d) for d in docs]


def list_followups_for_org(
    org_id: str,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """List follow-up jobs for an entire org (optionally filtered by status)."""
    query: dict = {"org_id": org_id}
    if status:
        query["status"] = status

    docs = list(
        db[COLLECTION]
        .find(query, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_serialize_followup(d) for d in docs]


# =============================================================================
# INTERNAL — POLLING LOOP
# =============================================================================

async def _polling_loop() -> None:
    """
    Background asyncio task that runs forever, polling every POLL_INTERVAL_SECONDS.
    Finds due pending jobs and processes them concurrently.
    """
    logger.info("Polling loop started.")

    while True:
        try:
            await _process_due_jobs()
        except Exception as e:
            logger.error(f"Polling loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _process_due_jobs() -> None:
    """
    Find all jobs with status='pending' and fire_at <= now,
    atomically claim them, then execute concurrently.
    """
    now = datetime.now(timezone.utc)

    # Find due pending jobs (read phase — we'll atomically claim each one)
    due_jobs = await asyncio.to_thread(
        lambda: list(
            db[COLLECTION]
            .find(
                {"status": "pending", "fire_at": {"$lte": now}},
                {"_id": 0}
            )
            .limit(MAX_BATCH_SIZE)
        )
    )

    if not due_jobs:
        return

    logger.info(f"Found {len(due_jobs)} due follow-up job(s) to process.")

    # Atomically claim each job (prevents double execution)
    claimed_jobs = []
    for job in due_jobs:
        claimed = await asyncio.to_thread(
            lambda j=job: db[COLLECTION].find_one_and_update(
                {
                    "schedule_id": j["schedule_id"],
                    "status": "pending",  # Guard: only claim if still pending
                },
                {
                    "$set": {
                        "status": "executing",
                        "executed_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if claimed:
            claimed_jobs.append(claimed)

    if not claimed_jobs:
        return

    logger.info(f"Claimed {len(claimed_jobs)} job(s) for execution.")

    # Execute all claimed jobs concurrently
    await asyncio.gather(
        *[_execute_job(job) for job in claimed_jobs],
        return_exceptions=True,
    )


async def _execute_job(job: dict) -> None:
    """
    Execute a single follow-up job.
    On success → status="completed"
    On failure → retry if retry_count < max_retries, else status="failed"
    """
    schedule_id = job["schedule_id"]
    org_id = job["org_id"]
    conversation_id = job["conversation_id"]
    message_config = job.get("message_config", {})
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)

    try:
        # Step 1: Validate conversation still exists and is open
        conv_collection = db[f"conversations_{org_id}"]
        conversation = await asyncio.to_thread(lambda: conv_collection.find_one({"conversation_id": conversation_id}))

        if not conversation:
            logger.warning(f"[{schedule_id}] Conversation {conversation_id} not found — marking completed (no-op).")
            await asyncio.to_thread(_mark_completed, schedule_id)
            return

        if conversation.get("status") == "closed":
            logger.info(f"[{schedule_id}] Conversation {conversation_id} is closed — skipping follow-up.")
            await asyncio.to_thread(_mark_completed, schedule_id)
            return

        customer_name = conversation.get("customer_name", "")

        # Step 2: Get wa_id from organizations collection
        org = await asyncio.to_thread(lambda: db.organizations.find_one({"org_id": org_id}, {"wa_id": 1}))
        if not org or not org.get("wa_id"):
            raise Exception(f"No wa_id found for org {org_id}")

        wa_id = org["wa_id"]

        # Step 3: Send the message (handles both plain text and templates)
        from services.wa_service import send_whatsapp_message, send_whatsapp_template_message

        template_name = message_config.get("template_name")
        
        if template_name:
            # --- TEMPLATE PATH ---
            from services.google_sheet_automation import _resolve_recursive
            
            # Resolve variables in components
            components = message_config.get("components", [])
            resolved_components = _resolve_recursive(components, {
                "customer_name": customer_name,
            })

            template_dict = {
                "name": template_name,
                "language": {"code": message_config.get("language_code", "en")},
                "components": resolved_components
            }

            result = await send_whatsapp_template_message(
                recipient_id=conversation_id.split("_")[-1], # extract phone from whatsapp_91...
                template=template_dict,
                wa_id=wa_id,
                org_id=org_id,
                mode="reply",
            )
        else:
            # --- PLAIN TEXT PATH ---
            raw_text = message_config.get("text", "")
            resolved_text = _resolve_variables(raw_text, {"customer_name": customer_name})

            result = await send_whatsapp_message(
                message={
                    "conversation_id": conversation_id,
                    "content": resolved_text,
                    "sender_id": wa_id,
                },
                org_id=org_id,
                mode="reply",
            )

        logger.success(
            f"[{schedule_id}] Follow-up sent to {conversation_id}. message_id={result.get('message_id')}"
        )

        await asyncio.to_thread(_mark_completed, schedule_id)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[{schedule_id}] Execution failed: {error_msg}")

        if retry_count < max_retries:
            # Retry in 5 minutes
            retry_fire_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            await asyncio.to_thread(
                lambda: db[COLLECTION].update_one(
                    {"schedule_id": schedule_id},
                    {
                        "$set": {
                            "status": "pending",
                            "fire_at": retry_fire_at,
                            "error": error_msg,
                        },
                        "$inc": {"retry_count": 1},
                    },
                )
            )
            logger.info(
                f"[{schedule_id}] Scheduled retry {retry_count + 1}/{max_retries} at {retry_fire_at.isoformat()}"
            )
        else:
            await asyncio.to_thread(
                lambda: db[COLLECTION].update_one(
                    {"schedule_id": schedule_id},
                    {
                        "$set": {
                            "status": "failed",
                            "error": error_msg,
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
            )
            logger.error(f"[{schedule_id}] Exhausted retries — marked as failed.")


# =============================================================================
# INTERNAL — HELPERS
# =============================================================================

def _mark_completed(schedule_id: str) -> None:
    db[COLLECTION].update_one(
        {"schedule_id": schedule_id},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}},
    )


def _ensure_indexes() -> None:
    """Create indexes on automation_scheduled_followups for query performance."""
    col = db[COLLECTION]
    col.create_index([("status", 1), ("fire_at", 1)])
    col.create_index([("org_id", 1), ("flow_id", 1)])
    col.create_index([("org_id", 1), ("conversation_id", 1)])
    col.create_index("schedule_id", unique=True)
    logger.info("automation_scheduled_followups indexes ensured.")


def _recover_stuck_jobs() -> None:
    """
    On startup, reset any jobs stuck in 'executing' state from a previous crash.
    Jobs that have been executing for > STUCK_JOB_THRESHOLD_MINUTES are reset to 'pending'.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_JOB_THRESHOLD_MINUTES)

    result = db[COLLECTION].update_many(
        {
            "status": "executing",
            "executed_at": {"$lt": cutoff},
        },
        {
            "$set": {"status": "pending"},
            "$inc": {"retry_count": 1},
        },
    )

    if result.modified_count:
        logger.warning(
            f"Recovered {result.modified_count} stuck follow-up job(s) from previous crash."
        )


def _resolve_variables(text: str, variables: dict) -> str:
    """
    Replace {{variable_name}} placeholders in text.
    e.g. "Hi {{customer_name}}!" + {"customer_name": "Rahul"} → "Hi Rahul!"
    """
    if not text:
        return text

    def replacer(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))

    return re.sub(r"\{\{([^}]+)\}\}", replacer, text)


def _serialize_followup(doc: dict) -> dict:
    """Convert datetime fields to ISO strings for JSON serialization."""
    datetime_fields = [
        "fire_at", "created_at", "executed_at",
        "completed_at", "cancelled_at",
    ]
    result = dict(doc)
    for field in datetime_fields:
        val = result.get(field)
        if isinstance(val, datetime):
            result[field] = val.isoformat()
    return result