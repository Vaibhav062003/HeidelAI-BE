import asyncio
from datetime import datetime, timezone
from typing import Optional

from database import get_mongo_db
from loguru import logger

db = get_mongo_db()

POLL_INTERVAL_SECONDS = 60
_polling_task: Optional[asyncio.Task] = None


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

async def startup_google_scheduler() -> None:
    """
    Called from lifespan.py on application startup.
    Creates required MongoDB indexes and starts background polling loop.
    """
    global _polling_task

    _ensure_indexes()

    _polling_task = asyncio.create_task(_polling_loop())
    logger.success(f"Google Sheets scheduler started (poll interval = {POLL_INTERVAL_SECONDS}s).")


async def shutdown_google_scheduler() -> None:
    """Called from lifespan.py on application shutdown."""
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
    """Infinite loop: poll all enabled sheets every POLL_INTERVAL_SECONDS."""
    logger.info("Google Sheets polling loop started.")

    while True:
        try:
            await _poll_all_sheets()
        except Exception as e:
            logger.error(f"Google Sheets polling loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _poll_all_sheets() -> None:
    """
    Find all sheets with polling_enabled=True whose orgs have active Google connections.
    Sync each one independently so one failure doesn't affect others.
    """
    active_org_ids = [
        doc["org_id"]
        for doc in db.google_connections.find(
            {"status": "active"},
            {"org_id": 1, "_id": 0}
        )
    ]

    if not active_org_ids:
        return

    sheets = list(db.google_sheets_connections.find(
        {
            "org_id": {"$in": active_org_ids},
            "polling_enabled": True,
        },
        {"org_id": 1, "sheet_id": 1, "_id": 0}
    ))

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
    After syncing new rows, triggers any google_sheet automation flows
    that reference this sheet.
    """
    try:
        from api.google_sheets_routes import _sync_sheet
        result = await _sync_sheet(org_id, sheet_id)
        rows = result.get("rows_processed", 0)

        if rows > 0:
            logger.info(f"[Scheduler] Synced {rows} new row(s) from sheet {sheet_id} for org {org_id}")

            # Trigger any google_sheet automation flows for this sheet
            await _trigger_google_sheet_automations(org_id, sheet_id)

    except Exception as e:
        logger.error(f"[Scheduler] Failed to sync sheet {sheet_id} for org {org_id}: {str(e)}")
        try:
            db.google_sheets_connections.update_one(
                {"org_id": org_id, "sheet_id": sheet_id},
                {"$set": {
                    "last_sync_error": str(e),
                    "last_sync_error_at": datetime.now(timezone.utc).isoformat()
                }}
            )
        except Exception:
            pass


async def _trigger_google_sheet_automations(org_id: str, sheet_id: str) -> None:
    """
    Find all active google_sheet automation triggers for this sheet
    and process any new phone numbers that haven't been messaged yet.
    """
    try:
        from services.google_sheet_automation import process_new_sheet_rows
        await process_new_sheet_rows(org_id, sheet_id)
    except Exception as e:
        logger.error(f"[Scheduler] Error triggering google_sheet automations for sheet {sheet_id}: {e}")


# ─── MongoDB Indexes ──────────────────────────────────────────────────────────

def _ensure_indexes() -> None:
    """Create indexes for efficient querying and deduplication."""
    try:
        # google_connections indexes
        db.google_connections.create_index([("org_id", 1)], unique=True)
        db.google_connections.create_index([("status", 1)])

        # google_sheets_connections indexes
        db.google_sheets_connections.create_index([("org_id", 1), ("sheet_id", 1)], unique=True)
        db.google_sheets_connections.create_index([("org_id", 1), ("polling_enabled", 1)])

        # automation_triggers index for google_sheet lookups
        db.automation_triggers.create_index(
            [("org_id", 1), ("trigger_type", 1), ("status", 1), ("filters.sheet_id", 1)],
            name="google_sheet_trigger_idx",
            sparse=True,
        )

        # google_sheet_sent_numbers deduplication index
        db.google_sheet_sent_numbers.create_index(
            [("org_id", 1), ("flow_id", 1), ("phone", 1)],
            unique=True,
            name="dedup_sent_idx",
        )

        # Per-org row storage indexes
        active_orgs = db.google_connections.distinct("org_id")
        for org_id in active_orgs:
            col = db[f"google_sheet_rows_{org_id}"]
            col.create_index([("sheet_id", 1), ("row_number", 1)], unique=True)
            col.create_index([("sheet_id", 1), ("synced_at", -1)])

        logger.info("Google Sheets MongoDB indexes ensured.")
    except Exception as e:
        logger.error(f"Error ensuring Google Sheets indexes: {e}")