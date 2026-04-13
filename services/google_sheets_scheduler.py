import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_mongo_db
from loguru import logger

db = get_mongo_db()

POLL_INTERVAL_SECONDS = 60
_polling_task: Optional[asyncio.Task] = None


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

async def startup_google_scheduler() -> None:
    global _polling_task
    await _ensure_indexes()
    _polling_task = asyncio.create_task(_polling_loop())
    logger.success(f"Google Sheets scheduler started (poll interval = {POLL_INTERVAL_SECONDS}s).")


async def shutdown_google_scheduler() -> None:
    global _polling_task
    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass
    logger.success("Google Sheets scheduler stopped.")


# ─── Polling Loop ─────────────────────────────────────────────────────────────

async def _polling_loop() -> None:
    logger.info("Google Sheets polling loop started.")
    while True:
        try:
            await _poll_all_sheets()
        except Exception as e:
            logger.error(f"Google Sheets polling loop error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _poll_all_sheets() -> None:
    active_org_ids = [
        doc["org_id"]
        for doc in await asyncio.to_thread(
            lambda: list(db.google_connections.find(
                {"status": "active"},
                {"org_id": 1, "_id": 0}
            ))
        )
    ]

    if not active_org_ids:
        return

    sheets = await asyncio.to_thread(
        lambda: list(db.google_sheets_connections.find(
            {
                "org_id": {"$in": active_org_ids},
                "polling_enabled": True,
            },
            {"org_id": 1, "sheet_id": 1, "_id": 0}
        ))
    )

    if not sheets:
        return

    logger.info(f"Google Sheets scheduler: polling {len(sheets)} sheet(s)")

    await asyncio.gather(
        *[_safe_sync_sheet(s["org_id"], s["sheet_id"]) for s in sheets],
        return_exceptions=True
    )


async def _safe_sync_sheet(org_id: str, sheet_id: str) -> None:
    """
    Sync a single sheet with full error isolation.

    FIX: The previous implementation used upsert=True on find_one_and_update with _id,
    which throws a DuplicateKeyError when the document already exists (even with a valid lease).
    This caused every sync to fail silently after the first run and the lease was never released.

    New approach:
      1. Try to INSERT a new lease document (succeeds only on first acquisition).
      2. If insert fails (lease exists), check if it has expired — if so, replace it.
      3. If a valid non-expired lease exists, skip this sheet.
    """
    lease_key = f"sync_{org_id}_{sheet_id}"
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=5)

    # Step 1: Try to insert a fresh lease
    lease_acquired = False
    try:
        await asyncio.to_thread(
            db.scheduler_leases.insert_one,
            {
                "_id": lease_key,
                "acquired_at": now,
                "expires_at": expiry,
                "org_id": org_id,
                "sheet_id": sheet_id,
            }
        )
        lease_acquired = True
    except Exception:
        # Document exists — check if it's expired
        try:
            result = await asyncio.to_thread(
                db.scheduler_leases.find_one_and_replace,
                {
                    "_id": lease_key,
                    "expires_at": {"$lt": now},   # only replace if expired
                },
                {
                    "_id": lease_key,
                    "acquired_at": now,
                    "expires_at": expiry,
                    "org_id": org_id,
                    "sheet_id": sheet_id,
                }
            )
            lease_acquired = result is not None
        except Exception as e:
            logger.error(f"[Scheduler] Lease check failed for sheet {sheet_id}: {e}")
            return

    if not lease_acquired:
        # Another worker holds a valid lease — skip
        return

    try:
        from api.google_sheets_routes import _sync_sheet
        result = await _sync_sheet(org_id, sheet_id)
        rows = result.get("rows_processed", 0)

        if rows > 0:
            logger.info(f"[Scheduler] Synced {rows} new row(s) from sheet {sheet_id} for org {org_id}")
            await _trigger_google_sheet_automations(org_id, sheet_id)

    except Exception as e:
        logger.error(f"[Scheduler] Failed to sync sheet {sheet_id} for org {org_id}: {str(e)}")
        try:
            await asyncio.to_thread(
                db.google_sheets_connections.update_one,
                {"org_id": org_id, "sheet_id": sheet_id},
                {"$set": {
                    "last_sync_error": str(e),
                    "last_sync_error_at": datetime.now(timezone.utc).isoformat()
                }}
            )
        except Exception:
            pass
    finally:
        # Always release the lease so the next poll cycle can proceed
        try:
            await asyncio.to_thread(
                db.scheduler_leases.delete_one, {"_id": lease_key}
            )
        except Exception as e:
            logger.warning(f"[Scheduler] Could not release lease for {lease_key}: {e}")


async def _trigger_google_sheet_automations(org_id: str, sheet_id: str) -> None:
    try:
        from services.google_sheet_automation import process_new_sheet_rows
        await process_new_sheet_rows(org_id, sheet_id)
    except Exception as e:
        logger.error(f"[Scheduler] Error triggering google_sheet automations for sheet {sheet_id}: {e}")


# ─── MongoDB Indexes ──────────────────────────────────────────────────────────

async def _ensure_indexes() -> None:
    """
    FIX: The automation service (google_sheet_automation.py) also calls _ensure_sent_index()
    which creates the same unique index. Creating the same index twice is idempotent in MongoDB
    only if the options match exactly. To be safe, we use background=True and suppress
    IndexConflict errors — the index will already exist after startup.
    """
    try:
        await asyncio.to_thread(lambda: db.google_connections.create_index([("org_id", 1)], unique=True))
        await asyncio.to_thread(lambda: db.google_connections.create_index([("status", 1)]))

        await asyncio.to_thread(lambda: db.google_sheets_connections.create_index([("org_id", 1), ("sheet_id", 1)], unique=True))
        await asyncio.to_thread(lambda: db.google_sheets_connections.create_index([("org_id", 1), ("polling_enabled", 1)]))

        await asyncio.to_thread(lambda: db.automation_triggers.create_index(
            [("org_id", 1), ("trigger_type", 1), ("status", 1), ("filters.sheet_id", 1)],
            name="google_sheet_trigger_idx",
            sparse=True,
        ))

        # Use background index creation and catch conflicts gracefully
        try:
            await asyncio.to_thread(lambda: db.google_sheet_sent_numbers.create_index(
                [("org_id", 1), ("flow_id", 1), ("phone", 1)],
                unique=True,
                name="dedup_sent_idx",
            ))
        except Exception as idx_err:
            # Index already exists (created by google_sheet_automation._ensure_sent_index)
            logger.debug(f"dedup_sent_idx already exists (safe to ignore): {idx_err}")

        active_orgs = await asyncio.to_thread(lambda: db.google_sheets_connections.distinct("org_id"))
        for org_id in active_orgs:
            col = db[f"google_sheet_rows_{org_id}"]
            await asyncio.to_thread(lambda c=col: c.create_index([("sheet_id", 1), ("row_number", 1)], unique=True))
            await asyncio.to_thread(lambda c=col: c.create_index([("sheet_id", 1), ("synced_at", -1)]))

        # TTL index to auto-expire stale leases
        await asyncio.to_thread(lambda: db.scheduler_leases.create_index("expires_at", expireAfterSeconds=0))

        logger.info("Google Sheets MongoDB indexes ensured.")
    except Exception as e:
        logger.error(f"Error ensuring Google Sheets indexes: {e}")