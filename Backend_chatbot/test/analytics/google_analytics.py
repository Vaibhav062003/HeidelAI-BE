import aiohttp
from typing import Dict, Any
from utils.retry import with_retry, RetryConfig

@with_retry(RetryConfig(max_retries=3, delay=1.0))
async def fetchGoogleAnalyticsData(start_date: str, end_date: str) -> Dict[str, Any]:
    """Fetch Google Analytics data for the specified date range"""
    # Replace with your actual GA4 API endpoint and credentials
    api_url = "YOUR_GA4_API_ENDPOINT"
    headers = {
        "Authorization": "Bearer YOUR_ACCESS_TOKEN"
    }
    
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "metrics": ["pageviews", "sessions", "users"],
        "dimensions": ["date"]
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=headers, params=params) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"GA API error: {response.status}")