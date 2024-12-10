from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Callable
import json
from functools import wraps
import asyncio
import hashlib
from config.settings import CACHE_SETTINGS

class AnalyticsCache:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AnalyticsCache, cls).__new__(cls)
            cls._instance.initialize()
        return cls._instance
    
    def initialize(self):
        """Initialize cache with settings"""
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = CACHE_SETTINGS["ttl"]
        self.max_size = CACHE_SETTINGS["max_size"]
        self.cleanup_threshold = self.max_size * CACHE_SETTINGS["cleanup_threshold"]
        self._lock = asyncio.Lock()
        
    def generate_cache_key(self, func_name: str, *args, **kwargs) -> str:
        """Generate a unique cache key"""
        key_parts = [func_name]
        key_parts.extend(str(arg) for arg in args)
        key_parts.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
        
        key_string = "_".join(key_parts)
        return hashlib.md5(key_string.encode()).hexdigest()
        
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if it exists and is not expired"""
        if key in self.cache:
            cache_entry = self.cache[key]
            if datetime.now() < cache_entry["expiry"]:
                cache_entry["hits"] += 1
                return cache_entry["data"]
            else:
                del self.cache[key]
        return None
        
    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """Set value in cache with expiry"""
        async with self._lock:
            if len(self.cache) >= self.cleanup_threshold:
                self._cleanup()
                
            self.cache[key] = {
                "data": value,
                "expiry": datetime.now() + timedelta(seconds=ttl or self.ttl),
                "timestamp": datetime.now(),
                "hits": 0
            }
        
    def _cleanup(self):
        """Clean up expired entries and least recently used items"""
        now = datetime.now()
        
        # Remove expired entries
        expired_keys = [
            k for k, v in self.cache.items()
            if now >= v["expiry"]
        ]
        for k in expired_keys:
            del self.cache[k]
            
        # If still over max_size, remove least recently used
        if len(self.cache) >= self.max_size:
            sorted_entries = sorted(
                self.cache.items(),
                key=lambda x: (x[1]["hits"], x[1]["timestamp"])
            )
            entries_to_remove = len(self.cache) - int(self.max_size * CACHE_SETTINGS["min_cleanup_size"])
            for key, _ in sorted_entries[:entries_to_remove]:
                del self.cache[key]

    def clear(self):
        """Clear all cache entries"""
        self.cache.clear()
        
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        now = datetime.now()
        return {
            "size": len(self.cache),
            "max_size": self.max_size,
            "expired_entries": sum(1 for v in self.cache.values() if now >= v["expiry"]),
            "hit_rates": {
                k: v["hits"] for k, v in self.cache.items()
            },
            "memory_usage": self._estimate_memory_usage()
        }
        
    def _estimate_memory_usage(self) -> int:
        """Estimate memory usage of cache in bytes"""
        return sum(
            len(str(entry["data"])) + 128  # 128 bytes overhead per entry
            for entry in self.cache.values()
        )

def cache_analytics(ttl: Optional[int] = None):
    """Decorator to cache analytics function results"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            cache = AnalyticsCache()
            cache_key = cache.generate_cache_key(func.__name__, *args, **kwargs)
            
            # Check cache first
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                return cached_result
                
            # Execute function if not cached
            result = await func(*args, **kwargs)
            
            # Cache the result
            await cache.set(cache_key, result, ttl)
            return result
            
        return wrapper
    return decorator