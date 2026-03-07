# === Built-in Modules ===
import os, traceback, sys

# === Third-party Modules ===
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from loguru import logger

# === Internal Modules ===
from api import router
from core.lifespan import lifespan
from config.settings import INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET
from services.utils import format_in_ist

logger.remove()
logger.add(sys.stdout, format=format_in_ist, colorize=True)

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

@app.exception_handler(StarletteHTTPException)
async def http_exception_traceback_handler(request: Request, exc: StarletteHTTPException):
    # Hunt down the 403 errors only
    if exc.status_code == 403:
        # Format the traceback into a readable string
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        
        logger.error(f"🚨 403 FORBIDDEN INTERCEPTED!")
        logger.error(f"Endpoint: {request.method} {request.url}")
        logger.error(f"Detail: {exc.detail}")
        logger.error(f"Traceback:\n{tb_str}")
        
    # Return the standard response so the app behaves normally
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
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
