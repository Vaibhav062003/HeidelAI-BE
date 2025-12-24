# === Built-in Modules ===
import os

# === Third-party Modules ===
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# === Internal Modules ===
from api import router
from core.lifespan import lifespan
from config.settings import INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET
from core.logger import get_logger

logger = get_logger(__name__)

# INSTAGRAM LOGIN CODE EXCHANGE
if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET:
    logger.error("Instagram credentials not properly configured!")
else:
    logger.success(f"Using Instagram App ID: {INSTAGRAM_APP_ID[:5]}... (truncated)")

ENV = os.getenv("ENV", "prod")
logger.success(f"Running in {ENV} environment.")

app = FastAPI(
    lifespan=lifespan, 
    redoc_url="/redoc" if ENV != "prod" else None,
    # docs_url="/docs" if ENV != "prod" else None,
    title="HeidelAI Backend API",
    )

app.include_router(router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.heidelai.com", 
        "https://heidelai.com",
        "https://www.deveopment.heidelai.com", 
        "https://development.heidelai.com",
        "https://www.testing.heidelai.com", 
        "https://testing.heidelai.com",
        "http://localhost", 
        "http://localhost:3000", 
        # Add www subdomain
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]  
)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
