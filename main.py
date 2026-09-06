# === Built-in Modules ===
import os, sys

# === Third-party Modules ===
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from fastapi.responses import JSONResponse
from core.limiter import limiter
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
    logger.success("Instagram credentials configured")

ENV = os.getenv("ENV", "prod")
logger.success(f"Running in {ENV} environment.")

def get_allowed_origins(environment: str, capacitor_origin_flag: str) -> list[str]:
    if environment.strip().lower() not in {"dev", "development", "test", "testing", "staging"}:
        return [
            "https://www.heidelai.com",
            "https://heidelai.com",
        ]

    origins = [
        "https://www.heidelai.com", 
        "https://heidelai.com",
        "https://www.development.heidelai.com", 
        "https://development.heidelai.com",
        "https://www.testing.heidelai.com", 
        "https://testing.heidelai.com",
        "http://localhost", 
        "http://localhost:3000", 
    ]
    if capacitor_origin_flag.strip().lower() == "true":
        origins.append("https://localhost")
    return origins


alowed_origins = get_allowed_origins(
    ENV,
    os.getenv("ALLOW_CAPACITOR_LOCALHOST_ORIGIN", ""),
)

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
    logger.warning(f"Rate limit exceeded: {request.method} {request.url.path}")
    return JSONResponse(                                        
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "detail": "Too many requests, please slow down.",
        }
    )            

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
