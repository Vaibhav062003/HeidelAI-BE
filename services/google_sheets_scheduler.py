import asyncio
from datetime import datetime, timezone
from typing import Optional

from database import get_mongo_db
from core.logger import get_logger

logger = get_logger(__name__)
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
    # Only poll sheets for orgs with an active connection
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

    # Run all sheets concurrently; errors are isolated per sheet
    await asyncio.gather(
        *[_safe_sync_sheet(s["org_id"], s["sheet_id"]) for s in sheets],
        return_exceptions=True
    )


async def _safe_sync_sheet(org_id: str, sheet_id: str) -> None:
    """Sync a single sheet with full error isolation."""
    try:
        from services.google_sheets_routes import _sync_sheet
        result = await _sync_sheet(org_id, sheet_id)
        rows = result.get("rows_processed", 0)
        if rows > 0:
            logger.info(f"[Scheduler] Synced {rows} new row(s) from sheet {sheet_id} for org {org_id}")
    except Exception as e:
        logger.error(f"[Scheduler] Failed to sync sheet {sheet_id} for org {org_id}: {str(e)}")
        # Mark last_sync_error for debugging but don't crash
        try:
            db.google_sheets_connections.update_one(
                {"org_id": org_id, "sheet_id": sheet_id},
                {"$set": {
                    "last_sync_error": str(e),
                    "last_sync_error_at": datetime.now(timezone.utc).isoformat()
                }}
            )
        except Exception:
            pass  # Even the error recording failed — truly silent here


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

        # Per-org row storage: unique on (sheet_id, row_number) to prevent duplicate inserts
        # We use a dynamic collection per org, so we create indexes lazily in the sync function.
        # This pre-creates for any orgs that already exist.
        active_orgs = db.google_connections.distinct("org_id")
        for org_id in active_orgs:
            col = db[f"google_sheet_rows_{org_id}"]
            col.create_index([("sheet_id", 1), ("row_number", 1)], unique=True)
            col.create_index([("sheet_id", 1), ("synced_at", -1)])

        logger.info("Google Sheets MongoDB indexes ensured.")
    except Exception as e:
        logger.error(f"Error ensuring Google Sheets indexes: {e}")