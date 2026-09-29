"""
google_sheets_scheduler.py — Webhook-driven Google Sheets sync scheduler.

Architecture:
  - Primary sync is triggered by Drive webhook notifications (see google_sheets_routes.py).
  - This scheduler runs two background jobs as safety nets:
    1. Channel Renewal (every 30 minutes): renews watch channels nearing expiration
    2. Reconciliation (every 60 minutes): full sync of all polling-enabled sheets
       to catch missed notifications, lapsed channels, and edge cases.

  The old 5-second polling loop has been replaced. If GOOGLE_WEBHOOK_BASE_URL is not
  configured, reconciliation still runs to provide non-real-time sync capability.
"""

import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional

from config.settings import ENVIRONMENT, GOOGLE_API_KEY, GOOGLE_WEBHOOK_BASE_URL
from database import get_mongo_db
from loguru import logger
import uuid

db = get_mongo_db()

# Background job intervals
CHANNEL_RENEWAL_INTERVAL_SECONDS = 30 * 60   # 30 minutes
RECONCILIATION_INTERVAL_SECONDS = 60 * 60    # 60 minutes

_channel_renewal_task: Optional[asyncio.Task] = None
_reconciliation_task: Optional[asyncio.Task] = None
_shared_client: Optional[httpx.AsyncClient] = None


# ─── Startup / Shutdown ───────────────────────────────────────────────────────

async def startup_google_scheduler() -> None:
    global _channel_renewal_task, _reconciliation_task, _shared_client
    await _ensure_indexes()

    # Initialize shared client for connection pooling
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    # Start channel renewal job (only if webhook URL is configured)
    if GOOGLE_WEBHOOK_BASE_URL:
        _channel_renewal_task = asyncio.create_task(_channel_renewal_loop())
        logger.success(
            f"[Scheduler] Channel renewal job started "
            f"(interval={CHANNEL_RENEWAL_INTERVAL_SECONDS}s)"
        )

        # Create watch channels for any sheets that don't have one yet
        asyncio.create_task(_bootstrap_watch_channels())
    else:
        logger.warning(
            "[Scheduler] GOOGLE_WEBHOOK_BASE_URL not set — "
            "webhook channels disabled, relying on reconciliation only."
        )

    # Start reconciliation job (always, as safety net)
    _reconciliation_task = asyncio.create_task(_reconciliation_loop())
    logger.success(
        f"[Scheduler] Reconciliation job started "
        f"(interval={RECONCILIATION_INTERVAL_SECONDS}s, ENV={ENVIRONMENT or 'default'})"
    )


async def shutdown_google_scheduler() -> None:
    global _channel_renewal_task, _reconciliation_task, _shared_client

    for task_ref, name in [
        (_channel_renewal_task, "channel renewal"),
        (_reconciliation_task, "reconciliation"),
    ]:
        if task_ref and not task_ref.done():
            task_ref.cancel()
            try:
                await task_ref
            except asyncio.CancelledError:
                pass

    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None

    logger.success("[Scheduler] Google Sheets scheduler stopped.")


# ─── Channel Renewal Loop ────────────────────────────────────────────────────

async def _channel_renewal_loop() -> None:
    """Periodically renew watch channels that are nearing expiration."""
    logger.info("[Scheduler] Channel renewal loop started.")
    while True:
        try:
            from services.google_drive_webhook import renew_expiring_channels
            renewed = await renew_expiring_channels()
            if renewed > 0:
                logger.info(f"[Scheduler] Renewed {renewed} watch channel(s)")
        except Exception as e:
            logger.error(f"[Scheduler] Channel renewal loop error: {e}")
        await asyncio.sleep(CHANNEL_RENEWAL_INTERVAL_SECONDS)


# ─── Bootstrap Watch Channels ────────────────────────────────────────────────

async def _bootstrap_watch_channels() -> None:
    """
    On startup, create watch channels for connected sheets that don't have one.
    This handles cases where:
      - The server restarted and channels expired during downtime
      - Sheets were connected before webhooks were enabled
    """
    try:
        # Find all sheets with polling enabled
        sheets = await asyncio.to_thread(
            lambda: list(db.google_sheets_connections.find(
                {"polling_enabled": True},
                {"org_id": 1, "sheet_id": 1, "_id": 0}
            ))
        )

        if not sheets:
            return

        # Find which sheets already have active (non-expired) channels
        now = datetime.now(timezone.utc)
        active_channels = await asyncio.to_thread(
            lambda: set(
                (doc["org_id"], doc["sheet_id"])
                for doc in db.google_drive_watch_channels.find(
                    {"expires_at": {"$gt": now}},
                    {"org_id": 1, "sheet_id": 1, "_id": 0}
                )
            )
        )

        missing = [
            s for s in sheets
            if (s["org_id"], s["sheet_id"]) not in active_channels
        ]

        if not missing:
            logger.info(
                f"[Scheduler] All {len(sheets)} connected sheets have active watch channels"
            )
            return

        logger.info(
            f"[Scheduler] Bootstrapping watch channels for {len(missing)} "
            f"sheet(s) without active channels"
        )

        from services.google_drive_webhook import create_watch_channel
        from api.google_sheets_routes import refresh_google_token

        for s in missing:
            try:
                access_token = await refresh_google_token(s["org_id"])
                await create_watch_channel(s["sheet_id"], s["org_id"], access_token)
            except Exception as e:
                logger.warning(
                    f"[Scheduler] Failed to bootstrap watch channel for "
                    f"sheet {s['sheet_id']} org {s['org_id']}: {e}"
                )

    except Exception as e:
        logger.error(f"[Scheduler] Bootstrap watch channels error: {e}")


# ─── Reconciliation Loop ─────────────────────────────────────────────────────

async def _reconciliation_loop() -> None:
    """
    Periodic full sync of all polling-enabled sheets.

    This is the safety net that guarantees eventual correctness even if:
      - Webhook notifications are missed or delayed
      - Watch channels silently expire
      - Google coalesces rapid edits into fewer notifications
    """
    logger.info("[Scheduler] Reconciliation loop started.")
    # Let the web server pass health checks before the first full-sheet sync.
    await asyncio.sleep(300)

    while True:
        try:
            await _poll_all_sheets()
        except Exception as e:
            logger.error(f"[Scheduler] Reconciliation loop error: {e}")
        await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)


async def _poll_all_sheets() -> None:
    """Sync all polling-enabled sheets across all active orgs."""
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

    logger.info(f"[Scheduler] Reconciliation: syncing {len(sheets)} sheet(s)")

    await asyncio.gather(
        *[_safe_sync_sheet(s["org_id"], s["sheet_id"]) for s in sheets],
        return_exceptions=True
    )


async def _safe_sync_sheet(org_id: str, sheet_id: str) -> None:
    """
    Sync a single sheet with full error isolation and lease-based locking.

    Uses a distributed lease (scheduler_leases collection) to prevent concurrent
    syncs of the same sheet — whether triggered by webhook or reconciliation.
    """
    lease_key = f"sync_{org_id}_{sheet_id}"
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(minutes=3)
    sync_version = str(uuid.uuid4())

    # Step 1: Try to insert a fresh lease
    lease_acquired = False
    try:
        await asyncio.to_thread(
            db.scheduler_leases.insert_one,
            {
                "_id": lease_key,
                "acquired_at": now,
                "expires_at": expiry,
                "sync_version": sync_version,
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
                    "sync_version": sync_version,
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
        # Phase 1: Sync sheet data from Google to MongoDB
        try:
            from api.google_sheets_routes import _sync_sheet
            # Pass the shared client to reuse connections
            result = await _sync_sheet(org_id, sheet_id, client=_shared_client)
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

        # Phase 2: ALWAYS trigger automations, even if sync failed.
        # MongoDB already has cached data from previous successful syncs,
        # and the dedup system ensures no double-sends.
        try:
            await _trigger_google_sheet_automations(org_id, sheet_id)
        except Exception as e:
            logger.error(f"[Scheduler] Error triggering automations for sheet {sheet_id}: {e}")

    finally:
        # Only release the lease if our version still owns it (optimistic lock)
        try:
            await asyncio.to_thread(
                db.scheduler_leases.delete_one,
                {"_id": lease_key, "sync_version": sync_version}
            )
        except Exception as e:
            logger.warning(f"[Scheduler] Could not release lease for {lease_key}: {e}")



async def _trigger_google_sheet_automations(org_id: str, sheet_id: str) -> None:
    try:
        from services.google_sheet_automation import process_new_sheet_rows
        await process_new_sheet_rows(org_id, sheet_id)
    except Exception as e:
        logger.error(f"[Scheduler] Error triggering google_sheet automations for sheet {sheet_id}: {e}")

    # Doctor patient follow-up (client-specific, no-ops for other orgs)
    try:
        from services.doctor_followup_worker import process_doctor_followups
        await process_doctor_followups(org_id, sheet_id)
    except Exception as e:
        logger.error(f"[Scheduler] Error processing doctor follow-ups for sheet {sheet_id}: {e}")


# ─── MongoDB Indexes ──────────────────────────────────────────────────────────

async def _ensure_indexes() -> None:
    """
    Ensure all required MongoDB indexes exist for the Google Sheets subsystem.
    Includes indexes for the new google_drive_watch_channels collection.
    """
    try:
        await asyncio.to_thread(lambda: db.google_connections.create_index(
            [("org_id", 1)], unique=True
        ))
        await asyncio.to_thread(lambda: db.google_connections.create_index(
            [("status", 1)]
        ))

        await asyncio.to_thread(lambda: db.google_sheets_connections.create_index(
            [("org_id", 1), ("sheet_id", 1)], unique=True
        ))
        await asyncio.to_thread(lambda: db.google_sheets_connections.create_index(
            [("org_id", 1), ("polling_enabled", 1)]
        ))

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

        active_orgs = await asyncio.to_thread(
            lambda: db.google_sheets_connections.distinct("org_id")
        )
        for org_id in active_orgs:
            col = db[f"google_sheet_rows_{org_id}"]
            await asyncio.to_thread(
                lambda c=col: c.create_index(
                    [("sheet_id", 1), ("row_number", 1)], unique=True
                )
            )
            await asyncio.to_thread(
                lambda c=col: c.create_index(
                    [("sheet_id", 1), ("synced_at", -1)]
                )
            )

        # TTL index to auto-expire stale leases
        await asyncio.to_thread(lambda: db.scheduler_leases.create_index(
            "expires_at", expireAfterSeconds=0
        ))

        # ── Watch channel indexes ─────────────────────────────────────────
        await asyncio.to_thread(lambda: db.google_drive_watch_channels.create_index(
            [("channel_id", 1)], unique=True
        ))
        await asyncio.to_thread(lambda: db.google_drive_watch_channels.create_index(
            [("org_id", 1), ("sheet_id", 1)]
        ))
        await asyncio.to_thread(lambda: db.google_drive_watch_channels.create_index(
            "expires_at", expireAfterSeconds=0
        ))

        logger.info("[Scheduler] MongoDB indexes ensured (including watch channels).")
    except Exception as e:
        logger.error(f"Error ensuring Google Sheets indexes: {e}")