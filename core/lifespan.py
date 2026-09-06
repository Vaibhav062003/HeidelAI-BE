from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
from loguru import logger
from core.scheduler import start_scheduler, stop_scheduler
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
    try:
        load_agents()
        await _run_startup()
        revocation_task = asyncio.create_task(run_whatsapp_revocation_reconciler())
        yield
    finally:
        # Shutdown must also run when the application exits exceptionally.
        stop_scheduler()
        await stop_push_notification_workers()
        await shutdown_intent_worker()
        await shutdown_scheduler()
        await shutdown_google_scheduler()
        if revocation_task:
            if not revocation_task.done():
                revocation_task.cancel()
            await asyncio.gather(revocation_task, return_exceptions=True)
        await close_agent_http_client()
        await close_meta_http_client()
        clear_agents()
