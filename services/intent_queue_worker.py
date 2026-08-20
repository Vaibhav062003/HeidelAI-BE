import asyncio
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger
from pymongo import ReturnDocument

from database import get_mongo_db
from agents.intent_agent.agent import extract_purchase_intent
from services.intent_agent_tools import save_intent_to_sheet, schedule_intent_followup

db = get_mongo_db()

# === Constants ===
COLLECTION = "purchase_intent_queue"
POLL_INTERVAL_SECONDS = 15
STUCK_JOB_THRESHOLD_MINUTES = 10
MAX_BATCH_SIZE = 20
MAX_CONCURRENT = 5
MESSAGE_CONTEXT_LIMIT = 20

_polling_task: Optional[asyncio.Task] = None

# =============================================================================
# STARTUP / SHUTDOWN
# =============================================================================

async def startup_intent_worker() -> None:
    global _polling_task
    await asyncio.to_thread(_ensure_indexes)
    await asyncio.to_thread(_recover_stuck_jobs)
    _polling_task = asyncio.create_task(_polling_loop())
    logger.success("Purchase intent queue worker started.")

async def shutdown_intent_worker() -> None:
    global _polling_task
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    logger.success("Purchase intent queue worker stopped.")

# =============================================================================
# PUBLIC API — ENQUEUE
# =============================================================================

def enqueue_intent_check(
    org_id: str,
    conversation_id: str,
    customer_phone: str,
    customer_name: str,
    message_content: str,
) -> str:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=300)  # 5-minute cooldown per conversation
    
    # IDEMPOTENCY: Only dedup against 'pending' jobs.
    # If a job is already 'processing', it has captured its context snapshot,
    # so a new message arriving now won't be in that analysis.
    # Allow a fresh job so the newer message gets analyzed later (Bug #4 fix).
    existing = db[COLLECTION].find_one({
        "conversation_id": conversation_id,
        "status": "pending",
        "created_at": {"$gt": cutoff}
    })
    
    if existing:
        return existing["job_id"]
        
    job_id = f"intent_{uuid.uuid4().hex}"
    doc = {
        "job_id": job_id,
        "org_id": org_id,
        "conversation_id": conversation_id,
        "customer_phone": customer_phone,
        "customer_name": customer_name,
        "message_content": message_content,
        "status": "pending",
        "retry_count": 0,
        "max_retries": 3,
        "created_at": now,
        "processed_at": None,
        "completed_at": None,
        "error": None,
    }
    
    db[COLLECTION].insert_one(doc)
    logger.info(f"Enqueued intent check {job_id} for {conversation_id}")
    return job_id

# =============================================================================
# INTERNAL — POLLING LOOP
# =============================================================================

async def _polling_loop() -> None:
    logger.info("Intent polling loop started.")
    while True:
        try:
            await _process_due_jobs()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Intent polling loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

async def _process_due_jobs() -> None:
    now = datetime.now(timezone.utc)
    
    due_jobs = await asyncio.to_thread(
        lambda: list(
            db[COLLECTION]
            .find({"status": "pending"})
            .sort("created_at", 1)
            .limit(MAX_BATCH_SIZE)
        )
    )
    
    if not due_jobs:
        return
        
    claimed_jobs = []
    for job in due_jobs:
        claimed = await asyncio.to_thread(
            lambda j=job: db[COLLECTION].find_one_and_update(
                {
                    "job_id": j["job_id"],
                    "status": "pending",
                },
                {
                    "$set": {
                        "status": "processing",
                        "processed_at": now,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if claimed:
            claimed_jobs.append(claimed)
            
    if not claimed_jobs:
        return
        
    logger.info(f"Claimed {len(claimed_jobs)} intent job(s) for processing.")
    
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    await asyncio.gather(
        *[_process_single_job(job, sem) for job in claimed_jobs],
        return_exceptions=True,
    )

async def _process_single_job(job: dict, semaphore: asyncio.Semaphore) -> None:
    async with semaphore:
        job_id = job["job_id"]
        org_id = job["org_id"]
        conversation_id = job["conversation_id"]
        customer_name = job.get("customer_name", "")
        customer_phone = job.get("customer_phone", "")
        retry_count = job.get("retry_count", 0)
        max_retries = job.get("max_retries", 3)
        
        try:
            col = db[f"messages_{org_id}"]
            docs = await asyncio.to_thread(
                lambda: list(
                    col.find({"conversation_id": conversation_id})
                    .sort("timestamp", -1)
                    .limit(MESSAGE_CONTEXT_LIMIT)
                )
            )
            
            if len(docs) < 3:
                logger.info(f"[{job_id}] Skipped job for {conversation_id} (not enough context).")
                await asyncio.to_thread(
                    lambda: db[COLLECTION].update_one(
                        {"job_id": job_id},
                        {
                            "$set": {
                                "status": "skipped",
                                "completed_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                )
                return
                
            docs.reverse()
            messages_str = "\n".join([f"{d.get('role', 'unknown')}: {d.get('content', '')}" for d in docs])
            
            ist_now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
            
            result = await extract_purchase_intent(messages_str, ist_now)
            
            # Branch 1: LLM returned None → transient error (network, malformed response).
            # Do NOT cancel existing follow-ups on transient failures (Bug #2 fix).
            if result is None:
                logger.warning(f"[{job_id}] LLM returned None for {conversation_id} — treating as transient error.")

            # Branch 2: AI explicitly detected purchase intent.
            elif result.has_intent and result.confidence != "low":
                logger.success(
                    f"[{job_id}] Intent detected for {conversation_id}: "
                    f"{result.product_name} on {result.tentative_date}"
                )
                
                await save_intent_to_sheet(
                    org_id=org_id,
                    conversation_id=conversation_id,
                    customer_name=customer_name,
                    customer_phone=customer_phone,
                    product_name=result.product_name or "",
                    tentative_date=result.tentative_date or "",
                    confidence=result.confidence,
                )
                
                await schedule_intent_followup(
                    org_id=org_id,
                    conversation_id=conversation_id,
                    product_name=result.product_name or "",
                    tentative_date_iso=result.tentative_date or "",
                )

            # Branch 3: AI EXPLICITLY said no intent (has_intent=False or low confidence).
            # Only now is it safe to cancel existing follow-ups.
            else:
                logger.info(f"[{job_id}] No intent detected for {conversation_id} (reason: {result.reasoning}).")
                # Cancel only intent follow-ups, not unrelated automations (Bug #1 fix).
                try:
                    from services.automation_scheduler import cancel_conversation_followups
                    cancelled = await asyncio.to_thread(
                        cancel_conversation_followups,
                        org_id=org_id,
                        conversation_id=conversation_id,
                        reason="no_intent_detected",
                        flow_id="__purchase_intent_followup__",
                    )
                    if cancelled:
                        logger.info(f"[{job_id}] Cancelled {cancelled} stale intent follow-up(s) for {conversation_id}")
                except Exception as cancel_err:
                    logger.warning(f"[{job_id}] Failed to cancel stale follow-ups: {cancel_err}")
                
            await asyncio.to_thread(
                lambda: db[COLLECTION].update_one(
                    {"job_id": job_id},
                    {
                        "$set": {
                            "status": "completed",
                            "completed_at": datetime.now(timezone.utc),
                        }
                    },
                )
            )
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[{job_id}] Execution failed: {error_msg}")
            
            if retry_count < max_retries:
                await asyncio.to_thread(
                    lambda: db[COLLECTION].update_one(
                        {"job_id": job_id},
                        {
                            "$set": {
                                "status": "pending",
                                "error": error_msg,
                            },
                            "$inc": {"retry_count": 1},
                        },
                    )
                )
            else:
                await asyncio.to_thread(
                    lambda: db[COLLECTION].update_one(
                        {"job_id": job_id},
                        {
                            "$set": {
                                "status": "failed",
                                "error": error_msg,
                                "completed_at": datetime.now(timezone.utc),
                            }
                        },
                    )
                )

# =============================================================================
# INTERNAL — HELPERS
# =============================================================================

def _ensure_indexes() -> None:
    col = db[COLLECTION]
    col.create_index([("status", 1), ("created_at", 1)])
    col.create_index([("org_id", 1), ("conversation_id", 1)])
    col.create_index("job_id", unique=True)
    col.create_index("completed_at", expireAfterSeconds=7*86400)
    logger.info(f"Indexes ensured for {COLLECTION}.")

def _recover_stuck_jobs() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_JOB_THRESHOLD_MINUTES)
    result = db[COLLECTION].update_many(
        {
            "status": "processing",
            "processed_at": {"$lt": cutoff},
        },
        {
            "$set": {"status": "pending"},
            "$inc": {"retry_count": 1},
        },
    )
    if result.modified_count:
        logger.warning(f"Recovered {result.modified_count} stuck intent job(s).")
