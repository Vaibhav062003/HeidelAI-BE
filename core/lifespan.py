from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
from core.scheduler import start_scheduler
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache
from services.wa_service import ensure_bulk_job_indexes, ensure_whatsapp_call_indexes, recover_stale_bulk_jobs
from services.automation_scheduler import startup_scheduler, shutdown_scheduler
from services.google_sheets_scheduler import startup_google_scheduler, shutdown_google_scheduler
from services.followup_service import ensure_followup_indexes
from services.oos_service import ensure_oos_indexes
from services.sales_offer_service import ensure_sales_offer_indexes
from services.push_notification_service import (
    ensure_push_notification_indexes,
    start_push_notification_workers,
    stop_push_notification_workers,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    await asyncio.to_thread(ensure_bulk_job_indexes)
    await asyncio.to_thread(ensure_whatsapp_call_indexes)
    await asyncio.to_thread(recover_stale_bulk_jobs)
    await asyncio.to_thread(ensure_push_notification_indexes)
    load_agents()
    load_prompts_into_cache()
    await ensure_followup_indexes()
    start_scheduler()
    await startup_scheduler()
    await startup_google_scheduler()
    await ensure_oos_indexes()
    await ensure_sales_offer_indexes()
    await start_push_notification_workers()
    yield
    # Shutdown
    await stop_push_notification_workers()
    await shutdown_scheduler()
    await shutdown_google_scheduler()
    clear_agents()
