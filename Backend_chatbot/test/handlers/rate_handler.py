from datetime import datetime, timedelta
import asyncio
from typing import Dict, Optional, Any, Callable
from collections import deque
import logging
from config.settings import RATE_LIMIT_SETTINGS

logger = logging.getLogger(__name__)

class APIRateLimiter:
    def __init__(self):
        self.default_rpm = RATE_LIMIT_SETTINGS["default_rpm"]
        self.request_timestamps: Dict[str, deque] = {}
        self.queues: Dict[str, asyncio.Queue] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self.service_limits = {
            "google_analytics": 2,  # 2 requests per minute
            "instagram": 200,       # 200 requests per hour
            "meta_ads": 200,        # 200 requests per hour
            "clarity": 100,         # 100 requests per hour
            "chatbot": 60           # 60 requests per minute
        }
        self.retry_delays = {
            "google_analytics": 1.0,
            "instagram": 2.0,
            "meta_ads": 2.0,
            "clarity": 1.5,
            "chatbot": 1.0
        }
    
    async def acquire(self, api_name: str) -> bool:
        """Acquire permission to make an API call"""
        if api_name not in self.locks:
            self.locks[api_name] = asyncio.Lock()
            self.request_timestamps[api_name] = deque(maxlen=self.service_limits.get(api_name, self.default_rpm))
            self.queues[api_name] = asyncio.Queue()
            
        async with self.locks[api_name]:
            now = datetime.now()
            timestamps = self.request_timestamps[api_name]
            
            # Remove timestamps older than the rate limit window
            window_size = self._get_window_size(api_name)
            while timestamps and (now - timestamps[0]).total_seconds() > window_size:
                timestamps.popleft()
            
            if len(timestamps) < self.service_limits.get(api_name, self.default_rpm):
                timestamps.append(now)
                return True
                
            # Calculate wait time
            wait_time = window_size - (now - timestamps[0]).total_seconds()
            if wait_time > 0:
                try:
                    await asyncio.sleep(wait_time)
                    timestamps.append(now)
                    return True
                except asyncio.CancelledError:
                    return False
            
            return False

    async def execute_with_rate_limit(
        self,
        api_name: str,
        func: Callable,
        *args,
        **kwargs
    ) -> Any:
        """Execute a function with rate limiting"""
        retry_count = RATE_LIMIT_SETTINGS["max_retries"]
        base_delay = self.retry_delays.get(api_name, RATE_LIMIT_SETTINGS["retry_delay"])
        
        for attempt in range(retry_count):
            if await self.acquire(api_name):
                try:
                    return await self._execute_func(func, *args, **kwargs)
                except Exception as e:
                    if attempt == retry_count - 1:
                        raise e
                    delay = base_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(f"Attempt {attempt + 1} failed for {api_name}: {str(e)}")
                    await asyncio.sleep(delay)
            else:
                await asyncio.sleep(base_delay)
        
        raise Exception(f"Failed to execute {api_name} after {retry_count} attempts")

    async def _execute_func(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function handling both async and sync functions"""
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)

    def _get_window_size(self, api_name: str) -> float:
        """Get rate limit window size in seconds"""
        if api_name in ["instagram", "meta_ads", "clarity"]:
            return 3600  # 1 hour window
        return 60  # 1 minute window

    async def bulk_execute(
        self,
        api_name: str,
        functions: list,
        *args,
        **kwargs
    ) -> list:
        """Execute multiple functions with rate limiting"""
        results = []
        for func in functions:
            try:
                result = await self.execute_with_rate_limit(api_name, func, *args, **kwargs)
                results.append({"status": "success", "data": result})
            except Exception as e:
                results.append({"status": "error", "error": str(e)})
        return results

    def get_rate_limit_info(self, api_name: str) -> Dict[str, Any]:
        """Get current rate limit information"""
        if api_name in self.request_timestamps:
            timestamps = self.request_timestamps[api_name]
            now = datetime.now()
            window_size = self._get_window_size(api_name)
            
            # Clean up old timestamps
            while timestamps and (now - timestamps[0]).total_seconds() > window_size:
                timestamps.popleft()
            
            return {
                "requests_made": len(timestamps),
                "limit": self.service_limits.get(api_name, self.default_rpm),
                "window_size": window_size,
                "remaining": self.service_limits.get(api_name, self.default_rpm) - len(timestamps)
            }
        return {
            "requests_made": 0,
            "limit": self.service_limits.get(api_name, self.default_rpm),
            "window_size": self._get_window_size(api_name),
            "remaining": self.service_limits.get(api_name, self.default_rpm)
        }