from fastapi import FastAPI
from contextlib import asynccontextmanager
from core.scheduler import start_scheduler
from services.ai_chatbot import load_agents, clear_agents, load_prompts_into_cache

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan event for the FastAPI application.
    """
    # Startup
    load_agents()  
    load_prompts_into_cache()
    start_scheduler()
    yield
    # Shutdown
    clear_agents()
