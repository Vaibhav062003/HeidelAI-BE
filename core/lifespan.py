from fastapi import FastAPI
from contextlib import asynccontextmanager
from core.scheduler import start_scheduler
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache
from services.automation_scheduler import startup_scheduler, shutdown_scheduler
from services.google_sheets_scheduler import startup_google_scheduler, shutdown_google_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    load_agents()
    load_prompts_into_cache()
    start_scheduler()
    await startup_scheduler()
    await startup_google_scheduler() 
    yield
    # Shutdown
    await shutdown_scheduler()
    await shutdown_google_scheduler()
    clear_agents()