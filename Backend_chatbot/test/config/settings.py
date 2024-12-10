from pathlib import Path
import os
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Base settings
BASE_DIR = Path(__file__).resolve().parent.parent

# API Settings
API_SETTINGS = {
    "google_analytics": {
        "property_id": os.getenv("PROPERTY_ID", "451093576"),
        "credentials_path": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json"),
        "requests_per_minute": 5,
        "max_retries": 3,
        "retry_delay": 0.0,
        "timeout": 10.0,
        "lock_timeout": 5.0
    },
    "instagram": {
        "account_id": os.getenv("INSTAGRAM_ACCOUNT_ID", "17841438674306985"),
        "access_token": os.getenv("ACCESS_TOKEN"),
        "app_id": os.getenv("APP_ID", "509681741462833"),
        "app_secret": os.getenv("SECRET_TOKEN", "117677cf3fa4108613b044bcbff9e6f3"),
        "requests_per_hour": 200,
        "max_retries": 3,
        "retry_delay": 2.0,
        "timeout": 30.0,
        "lock_timeout": 5.0
    },
    "meta_ads": {
        "account_id": os.getenv("AD_ACCOUNT_ID", "act_2353530418276809"),
        "access_token": os.getenv("ACCESS_TOKEN"),
        "requests_per_hour": 200,
        "max_retries": 3,
        "retry_delay": 2.0,
        "timeout": 30.0,
        "lock_timeout": 5.0
    },
    "clarity": {
        "project_id": os.getenv("CLARITY_PROJECT_ID"),
        "api_key": os.getenv("CLARITY_TOKEN"),
        "requests_per_hour": 100,
        "max_retries": 3,
        "retry_delay": 1.5,
        "timeout": 30.0,
        "lock_timeout": 5.0
    }
}

# LLM Settings
LLM_SETTINGS = {
    "light_model": os.getenv("LLM_MODEL_LIGHT", "gemini-1.5-flash-002"),
    "heavy_model": os.getenv("LLM_MODEL_HEAVY", "gemini-exp-1121"),
    "temperature": 0.0,
    "timeout": 30.0
}

# Cache settings
CACHE_SETTINGS = {
    "ttl": 3600,  # 1 hour in seconds
    "max_size": 1000,
    "cleanup_threshold": 0.8,  # Cleanup at 80% capacity
    "min_cleanup_size": 0.7    # Clean up to 70% capacity
}

# Rate limiting settings
RATE_LIMIT_SETTINGS = {
    "default_rpm": 2,
    "retry_delay": 1.0,
    "max_retries": 3,
    "backoff_factor": 2.0,
    "timeout": 30.0
}

# Analytics settings
ANALYTICS_SETTINGS = {
    "date_range": {
        "max_days": 730,  # Maximum 2 years
        "default_days": 30  # Default to last 30 days
    },
    "analysis": {
        "significant_change": 0.15,  # 15% change threshold
        "trend_confidence": 0.95,    # 95% confidence level
        "anomaly_std": 2.0          # 2 standard deviations for anomaly detection
    }
}

# Logging settings
LOGGING_SETTINGS = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": os.path.join(BASE_DIR, "logs", "analytics.log"),
    "max_size": 10 * 1024 * 1024,  # 10MB
    "backup_count": 5
}

# Ensure logs directory exists
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

# Error handling settings
ERROR_SETTINGS = {
    "max_retries": 3,
    "retry_delay": 1.0,
    "backoff_factor": 2.0,
    "timeout": 30.0,
    "retriable_status_codes": [408, 429, 500, 502, 503, 504]
}