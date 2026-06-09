# === Built-in Modules ===
import os, traceback, sys

# === Third-party Modules ===
from fastapi.exception_handlers import http_exception_handler
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from loguru import logger
from fastapi.responses import JSONResponse
from core.limiter import limiter, get_remote_address        
from slowapi.errors import RateLimitExceeded                       
from slowapi.middleware import SlowAPIMiddleware                    

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

if ENV == "prod":
    alowed_origins = [
        "https://www.heidelai.com", 
        "https://heidelai.com",
    ]
else:
    alowed_origins = [
        "https://www.heidelai.com", 
        "https://heidelai.com",
        "https://www.development.heidelai.com", 
        "https://development.heidelai.com",
        "https://www.testing.heidelai.com", 
        "https://testing.heidelai.com",
        "http://localhost", 
        "http://localhost:3000", 
    ]

app = FastAPI(
    lifespan=lifespan, 
    redoc_url="/redoc" if ENV != "prod" else None,
    docs_url="/docs" if ENV != "prod" else None,
    title="HeidelAI Backend API",
    )

# === Rate Limit State & Middleware ===                           
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)    

@app.exception_handler(RateLimitExceeded)                        
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    logger.warning(f"Rate limit exceeded: {request.method} {request.url} from {get_remote_address(request)}")
    return JSONResponse(                                        
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": "Too many requests, please slow down.",
        }
    )            

@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    # We only care about hunting down the 403 right now
    if exc.status_code == 403:
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        
        logger.error("🚨 403 FORBIDDEN INTERCEPTED!")
        logger.error(f"Endpoint: {request.method} {request.url}")
        logger.error(f"Detail: {exc.detail}")
        logger.error(f"Traceback:\n{tb_str}")
        
    # Delegate back to FastAPI's default handler to safely preserve all headers and exact behavior
    return await http_exception_handler(request, exc)

app.include_router(router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=alowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]  
)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
