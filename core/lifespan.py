from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
from loguru import logger
from pymongo.errors import PyMongoError
from core.scheduler import start_scheduler, stop_scheduler
from database import (
    close_mongo_clients,
    mark_mongo_available,
    mark_mongo_unavailable,
    mongo_retry_allowed,
    probe_mongo,
)
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache
from services.wa_service import (
    close_meta_http_client,
    ensure_bulk_job_indexes,
    ensure_whatsapp_call_indexes,
    ensure_whatsapp_outbound_indexes,
    recover_stale_bulk_jobs,
    run_whatsapp_revocation_reconciler,
)
from services.agent_service import close_agent_http_client
from services.automation_scheduler import startup_scheduler, shutdown_scheduler
from services.google_sheets_scheduler import startup_google_scheduler, shutdown_google_scheduler
from services.intent_queue_worker import startup_intent_worker, shutdown_intent_worker
from services.followup_service import ensure_followup_indexes
from services.oos_service import ensure_oos_indexes
from services.pricing_service import ensure_pricing_indexes
from services.sales_offer_service import ensure_sales_offer_indexes
from services.tally_service import ensure_tally_indexes
from services.stock_service import ensure_stock_indexes
from services.entitlement_service import ensure_entitlement_indexes
from services.transfer_notification_service import ensure_transfer_notification_indexes
from services.shopify_service import ensure_shopify_indexes, repair_legacy_shopify_connections
from services.push_notification_service import (
    ensure_push_notification_indexes,
    start_push_notification_workers,
    stop_push_notification_workers,
)


MONGO_BOOT_TIMEOUT_SECONDS = 90
MONGO_MONITOR_INTERVAL_SECONDS = 10


async def _wait_for_mongo() -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MONGO_BOOT_TIMEOUT_SECONDS
    delay = 2
    while True:
        try:
            await asyncio.to_thread(probe_mongo)
            mark_mongo_available()
            return
        except PyMongoError:
            mark_mongo_unavailable()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            logger.warning("MongoDB unavailable during startup; retrying in {}s", min(delay, int(remaining)))
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 15)


async def _monitor_mongo() -> None:
    while True:
        was_available = mongo_retry_allowed()
        try:
            await asyncio.to_thread(probe_mongo)
        except asyncio.CancelledError:
            raise
        except PyMongoError:
            mark_mongo_unavailable()
            if was_available:
                logger.error("MongoDB became unavailable; database work is paused")
        else:
            mark_mongo_available()
            if not was_available:
                logger.success("MongoDB connectivity restored; database work resumed")
        await asyncio.sleep(MONGO_MONITOR_INTERVAL_SECONDS)


async def _run_startup() -> None:
    jobs = (
        ("bulk job indexes", lambda: asyncio.to_thread(ensure_bulk_job_indexes)),
        ("WhatsApp call indexes", lambda: asyncio.to_thread(ensure_whatsapp_call_indexes)),
        ("WhatsApp outbound indexes", lambda: asyncio.to_thread(ensure_whatsapp_outbound_indexes)),
        ("push notification indexes", lambda: asyncio.to_thread(ensure_push_notification_indexes)),
        ("transfer notification indexes", lambda: asyncio.to_thread(ensure_transfer_notification_indexes)),
        ("follow-up indexes", ensure_followup_indexes),
        ("Shopify indexes", ensure_shopify_indexes),
        ("WhatsApp follow-up worker", startup_scheduler),
        ("Google Sheets worker", startup_google_scheduler),
        ("OOS indexes", ensure_oos_indexes),
        ("pricing indexes", ensure_pricing_indexes),
        ("sales offer indexes", ensure_sales_offer_indexes),
        ("Tally indexes", ensure_tally_indexes),
        ("stock indexes", ensure_stock_indexes),
        ("entitlement indexes", lambda: asyncio.to_thread(ensure_entitlement_indexes)),
        ("push notification workers", start_push_notification_workers),
        ("intent worker", startup_intent_worker),
        ("bulk job recovery", lambda: asyncio.to_thread(recover_stale_bulk_jobs)),
        ("prompt cache loading", lambda: asyncio.to_thread(load_prompts_into_cache)),
        ("Shopify legacy repair", repair_legacy_shopify_connections),
    )
    for name, job in jobs:
        logger.info("Starting {}", name)
        await job()
    start_scheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    revocation_task = None
    monitor_task = None
    try:
        await _wait_for_mongo()
        load_agents()
        await _run_startup()
        await _wait_for_mongo()
        revocation_task = asyncio.create_task(run_whatsapp_revocation_reconciler())
        monitor_task = asyncio.create_task(_monitor_mongo())
        yield
    finally:
        # Shutdown must also run when the application exits exceptionally.
        stop_scheduler()
        await stop_push_notification_workers()
        await shutdown_intent_worker()
        await shutdown_scheduler()
        await shutdown_google_scheduler()
        if monitor_task and not monitor_task.done():
            monitor_task.cancel()
        if revocation_task and not revocation_task.done():
            revocation_task.cancel()
        await asyncio.gather(
            *(task for task in (monitor_task, revocation_task) if task),
            return_exceptions=True,
        )
        await close_agent_http_client()
        await close_meta_http_client()
        clear_agents()
        close_mongo_clients()
