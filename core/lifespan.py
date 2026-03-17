from fastapi import FastAPI
from contextlib import asynccontextmanager
from core.scheduler import start_scheduler
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache
from services.wa_service import ensure_bulk_job_indexes, recover_stale_bulk_jobs

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    ensure_bulk_job_indexes()
    recover_stale_bulk_jobs()
    load_agents()  
    load_prompts_into_cache()
    start_scheduler()
    yield
    # Shutdown
    clear_agents()
