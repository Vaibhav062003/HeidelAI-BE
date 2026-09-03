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


async def _run_noncritical_startup() -> None:
    jobs = (
        ("bulk job recovery", asyncio.to_thread(recover_stale_bulk_jobs)),
        ("prompt cache loading", asyncio.to_thread(load_prompts_into_cache)),
        ("Shopify legacy repair", repair_legacy_shopify_connections()),
    )
    results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
    for (name, _), result in zip(jobs, results):
        if isinstance(result, Exception):
            logger.error(f"Non-critical startup job failed ({name}): {type(result).__name__}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    await asyncio.to_thread(ensure_bulk_job_indexes)
    await asyncio.to_thread(ensure_whatsapp_call_indexes)
    await asyncio.to_thread(ensure_whatsapp_outbound_indexes)
    revocation_task = asyncio.create_task(run_whatsapp_revocation_reconciler())
    await asyncio.to_thread(ensure_push_notification_indexes)
    await asyncio.to_thread(ensure_transfer_notification_indexes)
    load_agents()
    await ensure_followup_indexes()
    await ensure_shopify_indexes()
    bootstrap_task = asyncio.create_task(_run_noncritical_startup())
    start_scheduler()
    await startup_scheduler()
    await startup_google_scheduler()
    await ensure_oos_indexes()
    await ensure_pricing_indexes()
    await ensure_sales_offer_indexes()
    await ensure_tally_indexes()
    await ensure_stock_indexes()
    await asyncio.to_thread(ensure_entitlement_indexes)
    await start_push_notification_workers()
    await startup_intent_worker()
    try:
        yield
    finally:
        # Shutdown must also run when the application exits exceptionally.
        stop_scheduler()
        await stop_push_notification_workers()
        await shutdown_intent_worker()
        await shutdown_scheduler()
        await shutdown_google_scheduler()
        if not bootstrap_task.done():
            bootstrap_task.cancel()
        await asyncio.gather(bootstrap_task, return_exceptions=True)
        if not revocation_task.done():
            revocation_task.cancel()
        await asyncio.gather(revocation_task, return_exceptions=True)
        await close_agent_http_client()
        await close_meta_http_client()
        clear_agents()
