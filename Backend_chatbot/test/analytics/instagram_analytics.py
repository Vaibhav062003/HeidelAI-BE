import aiohttp
from typing import Dict, Any
from utils.retry import with_retry, RetryConfig

class InstagramAnalytics:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.instagram.com/v21.0"
        
    @with_retry(RetryConfig(max_retries=3, delay=1.0))
    async def fetch_account_metrics(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Fetch Instagram account metrics"""
        try:
            endpoint = f"{self.base_url}/me/insights"
            params = {
                "metric": "impressions,reach,profile_views",
                "period": "day",
                "access_token": self.access_token
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(endpoint, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:  # Rate limit
                        raise Exception("Rate limit exceeded")
                    else:
                        raise Exception(f"Instagram API error: {response.status}")
        except aiohttp.ClientError as e:
            raise Exception(f"Network error: {str(e)}")
                    
    @with_retry(RetryConfig(max_retries=3, delay=1.0))
    async def fetch_media_insights(self, media_id: str) -> Dict[str, Any]:
        """Fetch insights for a specific media post"""
        endpoint = f"{self.base_url}/{media_id}/insights"
        params = {
            "metric": "engagement,impressions,reach",
            "access_token": self.access_token
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, params=params) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise Exception(f"Instagram API error: {response.status}")