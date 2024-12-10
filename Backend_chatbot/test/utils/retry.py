import asyncio
from functools import wraps
from typing import Type, Tuple, Optional, Callable, Any, AsyncGenerator
import logging
from datetime import datetime
import aiohttp
import requests
import pymongo
import google.api_core.exceptions
from facebook_business.exceptions import FacebookRequestError
import urllib3
import ssl
import json
import websockets
import socket
from config.settings import RATE_LIMIT_SETTINGS

logger = logging.getLogger(__name__)

class RetryConfig:
    def __init__(
        self,
        max_retries: int = RATE_LIMIT_SETTINGS["max_retries"],
        delay: float = RATE_LIMIT_SETTINGS["retry_delay"],
        backoff_factor: float = 2.0,
        exceptions: Tuple[Type[Exception], ...] = (Exception,),
        timeout: Optional[float] = None
    ):
        self.max_retries = max_retries
        self.delay = delay
        self.backoff_factor = backoff_factor
        self.exceptions = exceptions
        self.timeout = timeout

class RetryState:
    def __init__(self):
        self.attempts = 0
        self.start_time = None
        self.last_error = None
        self.success = False

def with_retry(config: Optional[RetryConfig] = None):
    """Enhanced retry decorator with timeout and detailed logging"""
    if config is None:
        config = RetryConfig()
        
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            state = RetryState()
            state.start_time = datetime.now()
            
            async def execute_with_timeout() -> Any:
                if config.timeout:
                    try:
                        return await asyncio.wait_for(
                            func(*args, **kwargs),
                            timeout=config.timeout
                        )
                    except asyncio.TimeoutError:
                        raise Exception(f"Function timed out after {config.timeout} seconds")
                return await func(*args, **kwargs)
            
            while state.attempts < config.max_retries:
                try:
                    result = await execute_with_timeout()
                    state.success = True
                    
                    # Log success after retries
                    if state.attempts > 0:
                        logger.info(
                            f"Succeeded after {state.attempts + 1} attempts. "
                            f"Total time: {(datetime.now() - state.start_time).total_seconds():.2f}s"
                        )
                    
                    return result
                    
                except config.exceptions as e:
                    state.attempts += 1
                    state.last_error = e
                    
                    if state.attempts < config.max_retries:
                        delay = config.delay * (config.backoff_factor ** (state.attempts - 1))
                        
                        logger.warning(
                            f"Attempt {state.attempts}/{config.max_retries} failed: {str(e)}. "
                            f"Retrying in {delay:.2f}s..."
                        )
                        
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {config.max_retries} retry attempts failed. "
                            f"Total time: {(datetime.now() - state.start_time).total_seconds():.2f}s. "
                            f"Last error: {str(e)}"
                        )
                        raise
                        
            raise state.last_error
            
        return wrapper
    return decorator

def retry_async_callback(
    callback: Callable,
    config: Optional[RetryConfig] = None
) -> Callable:
    """Utility function to retry async callbacks"""
    if config is None:
        config = RetryConfig()
        
    async def retry_wrapper(*args, **kwargs):
        @with_retry(config)
        async def wrapped():
            return await callback(*args, **kwargs)
        return await wrapped()
        
    return retry_wrapper

def is_retriable_error(error: Exception) -> bool:
    """Determine if an error should trigger a retry"""
    retriable_errors = (
        # Network-related errors
        TimeoutError,
        ConnectionError,
        ConnectionResetError,
        ConnectionAbortedError,
        ConnectionRefusedError,
        asyncio.TimeoutError,
        
        # HTTP-specific errors
        aiohttp.ClientError,
        aiohttp.ClientConnectionError,
        aiohttp.ServerConnectionError,
        aiohttp.ClientPayloadError,
        aiohttp.ClientResponseError,
        
        # API rate limiting and server errors
        requests.exceptions.RequestException,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.TooManyRedirects,
        
        # Database connection errors
        pymongo.errors.AutoReconnect,
        pymongo.errors.ConnectionFailure,
        pymongo.errors.NetworkTimeout,
        
        # Google API specific errors
        google.api_core.exceptions.RetryError,
        google.api_core.exceptions.ServiceUnavailable,
        google.api_core.exceptions.DeadlineExceeded,
        google.api_core.exceptions.ResourceExhausted,
        
        # Facebook/Instagram API errors
        facebook_business.exceptions.FacebookRequestError,
        
        # Generic API errors
        urllib3.exceptions.ProtocolError,
        urllib3.exceptions.ReadTimeoutError,
        urllib3.exceptions.ConnectTimeoutError,
        
        # SSL/TLS errors
        ssl.SSLError,
        ssl.SSLWantReadError,
        ssl.SSLWantWriteError,
        
        # JSON parsing errors
        json.JSONDecodeError,
        
        # Websocket errors
        websockets.exceptions.ConnectionClosed,
        websockets.exceptions.ConnectionClosedError,
        
        # DNS errors
        socket.gaierror,
        socket.timeout,
        
        # Generic system errors
        OSError,
        IOError
    )
    
    # Check if error is in retriable_errors tuple
    if isinstance(error, retriable_errors):
        return True
        
    # Check for status codes in HTTP errors
    if hasattr(error, 'status_code'):
        status_code = str(getattr(error, 'status_code'))
        return (
            status_code.startswith('5') or  # Server errors (500-599)
            status_code == '429' or         # Too Many Requests
            status_code == '408'            # Request Timeout
        )
        
    # Check for specific error codes in API responses
    if hasattr(error, 'code'):
        error_code = str(getattr(error, 'code'))
        retriable_codes = {
            'ECONNRESET',         # Connection reset by peer
            'ECONNABORTED',       # Connection aborted
            'ECONNREFUSED',       # Connection refused
            'ETIMEDOUT',          # Operation timed out
            'EHOSTUNREACH',       # Host unreachable
            'ENETUNREACH',        # Network unreachable
            'EADDRINUSE',         # Address already in use
            'RATE_LIMIT_EXCEEDED',
            'RESOURCE_EXHAUSTED',
            'DEADLINE_EXCEEDED',
            'UNAVAILABLE'
        }
        return error_code in retriable_codes
        
    # Check for specific error messages
    error_message = str(error).lower()
    retriable_messages = {
        'timeout',
        'connection reset',
        'connection refused',
        'temporarily unavailable',
        'internal server error',
        'bad gateway',
        'service unavailable',
        'gateway timeout',
        'too many requests',
        'rate limit exceeded',
        'resource exhausted',
        'deadline exceeded',
        'server is busy',
        'try again later'
    }
    
    return any(msg in error_message for msg in retriable_messages)

async def retry_async_generator(
    generator: Callable,
    config: Optional[RetryConfig] = None
) -> AsyncGenerator:
    """Utility function to retry async generators"""
    if config is None:
        config = RetryConfig()

    async def wrapper(*args, **kwargs):
        async for item in generator(*args, **kwargs):
            yield item

    return await retry_async_callback(wrapper, config)