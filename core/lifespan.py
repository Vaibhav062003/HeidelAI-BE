from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
from core.scheduler import start_scheduler
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache
from services.wa_service import ensure_bulk_job_indexes, recover_stale_bulk_jobs

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    await asyncio.to_thread(ensure_bulk_job_indexes)
    await asyncio.to_thread(recover_stale_bulk_jobs)
    load_agents()  
    load_prompts_into_cache()
    start_scheduler()
    yield
    # Shutdown
    clear_agents()
