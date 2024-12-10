from typing import Dict, Any, List, Optional
from datetime import datetime
from handlers.date_handler import DateRangeHandler
from handlers.analytics_handler import AnalyticsHandler
from handlers.rate_handler import APIRateLimiter
from utils.retry import with_retry, RetryConfig
from utils.cache import cache_analytics
from config.settings import API_SETTINGS, LLM_SETTINGS
import pandas as pd
import asyncio
from concurrent.futures import ThreadPoolExecutor

class QueryHandler:
    def __init__(self, llm_light, llm_heavy):
        self.date_handler = DateRangeHandler()
        self.analytics_handler = AnalyticsHandler(llm_light, llm_heavy)
        self.rate_limiter = APIRateLimiter()
        self.llm_light = llm_light
        self.llm_heavy = llm_heavy
        self.executor = ThreadPoolExecutor(max_workers=3)
        
    @with_retry(RetryConfig(max_retries=3, delay=1.0))
    async def process_query(self, query: str) -> Dict[str, Any]:
        """Main query processing method with enhanced error handling and analytics"""
        try:
            # Classify query intent
            intent = await self._classify_query_intent(query)
            
            # Extract date range
            date_range = self.date_handler.get_date_range(query)
            
            # Process based on intent
            result = await self._process_by_intent(query, intent, date_range)
            
            # Enrich with insights
            enriched_result = await self._enrich_with_insights(result, intent)
            
            return {
                "status": "success",
                "intent": intent,
                "data": enriched_result,
                "date_range": date_range,
                "metadata": self._generate_metadata(query, intent)
            }
            
        except Exception as e:
            return await self._handle_error(e, query)

    async def _classify_query_intent(self, query: str) -> Dict[str, Any]:
        """Classify query intent using LLM"""
        try:
            intent_prompt = f"""Classify the following analytics query intent:
            Query: {query}
            
            Classify into:
            1. Data source (google_analytics, instagram, meta_ads, clarity, chatbot)
            2. Analysis type (trend, comparison, prediction, anomaly, summary)
            3. Metric focus (traffic, engagement, conversion, revenue, social)
            4. Time scope (historical, current, predictive)
            
            Return as JSON."""
            
            response = await self.llm_light.agenerate([{
                "role": "user",
                "content": intent_prompt
            }])
            
            return self._parse_intent_response(response.generations[0].text)
            
        except Exception as e:
            # Current fallback is too simple and could miss critical errors
            return {
                "data_source": "google_analytics",  # default fallback
                "analysis_type": "summary",
                "metric_focus": "traffic",
                "time_scope": "current",
                "error": str(e)
            }

    async def _process_by_intent(
        self, 
        query: str, 
        intent: Dict[str, str],
        date_range: Dict[str, str]
    ) -> Dict[str, Any]:
        """Process query based on classified intent"""
        try:
            # Parallel processing for different data sources
            tasks = []
            
            if intent["data_source"] in ["google_analytics", "all"]:
                tasks.append(self._get_ga_data(query, date_range))
            if intent["data_source"] in ["instagram", "all"]:
                tasks.append(self._get_instagram_data(query, date_range))
            if intent["data_source"] in ["meta_ads", "all"]:
                tasks.append(self._get_meta_ads_data(query, date_range))
            if intent["data_source"] in ["clarity", "all"]:
                tasks.append(self._get_clarity_data(query, date_range))
            if intent["data_source"] in ["chatbot", "all"]:
                tasks.append(self._get_chatbot_data(query, date_range))
                
            # Execute all tasks concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results based on analysis type
            processed_results = await self._process_analysis(
                results, 
                intent["analysis_type"],
                intent["metric_focus"]
            )
            
            return processed_results
            
        except Exception as e:
            raise Exception(f"Error processing intent: {str(e)}")

    async def _get_ga_data(self, query: str, date_range: Dict[str, str]) -> Dict[str, Any]:
        """Get Google Analytics data with rate limiting"""
        return await self.analytics_handler.handle_ga_query(
            query,
            date_range["start_date"],
            date_range["end_date"]
        )

    async def _get_instagram_data(self, query: str, date_range: Dict[str, str]) -> Dict[str, Any]:
        """Get Instagram data with rate limiting"""
        return await self.analytics_handler.handle_instagram_query(
            query,
            date_range["start_date"],
            date_range["end_date"]
        )

    async def _get_meta_ads_data(self, query: str, date_range: Dict[str, str]) -> Dict[str, Any]:
        """Get Meta Ads data with rate limiting"""
        return await self.analytics_handler.handle_meta_ads_query(
            query,
            date_range["start_date"],
            date_range["end_date"]
        )

    async def _process_analysis(
        self,
        results: List[Dict[str, Any]],
        analysis_type: str,
        metric_focus: str
    ) -> Dict[str, Any]:
        """Process results based on analysis type"""
        try:
            if analysis_type == "trend":
                return await self._analyze_trends(results, metric_focus)
            elif analysis_type == "comparison":
                return await self._analyze_comparisons(results, metric_focus)
            elif analysis_type == "prediction":
                return await self._analyze_predictions(results, metric_focus)
            elif analysis_type == "anomaly":
                return await self._analyze_anomalies(results, metric_focus)
            else:  # summary
                return await self._create_summary(results, metric_focus)
                
        except Exception as e:
            raise Exception(f"Error in analysis processing: {str(e)}")

    async def _enrich_with_insights(
        self,
        result: Dict[str, Any],
        intent: Dict[str, str]
    ) -> Dict[str, Any]:
        """Enrich results with AI-generated insights"""
        try:
            insight_prompt = self._generate_insight_prompt(result, intent)
            
            insights_response = await self.llm_heavy.agenerate([{
                "role": "user",
                "content": insight_prompt
            }])
            
            return {
                **result,
                "insights": insights_response.generations[0].text,
                "recommendations": await self._generate_recommendations(result, intent)
            }
            
        except Exception as e:
            return {
                **result,
                "insights_error": str(e)
            }

    async def _generate_recommendations(
        self,
        result: Dict[str, Any],
        intent: Dict[str, str]
    ) -> List[str]:
        """Generate actionable recommendations based on analysis"""
        try:
            recommendation_prompt = f"""Based on the following analytics data and intent:
            Data: {result}
            Intent: {intent}
            
            Generate specific, actionable recommendations focusing on:
            1. Performance optimization
            2. User engagement
            3. Conversion improvement
            4. Resource allocation
            5. Risk mitigation
            
            Format as a list of clear, actionable items."""
            
            response = await self.llm_heavy.agenerate([{
                "role": "user",
                "content": recommendation_prompt
            }])
            
            return response.generations[0].text.split("\n")
            
        except Exception as e:
            return [f"Error generating recommendations: {str(e)}"]

    def _generate_metadata(self, query: str, intent: Dict[str, str]) -> Dict[str, Any]:
        """Generate query metadata for tracking and optimization"""
        return {
            "timestamp": datetime.now().isoformat(),
            "query_type": intent["analysis_type"],
            "data_sources": intent["data_source"],
            "processing_time": None,  # Will be filled by decorator
            "cache_hit": False,  # Will be filled by cache decorator
            "query_complexity": self._calculate_query_complexity(query)
        }

    async def _handle_error(self, error: Exception, query: str) -> Dict[str, Any]:
        """Handle errors with graceful degradation"""
        error_message = str(error)
        
        # Try to provide partial results if possible
        partial_results = await self._attempt_partial_results(error, query)
        
        return {
            "status": "error",
            "error": error_message,
            "partial_results": partial_results,
            "date_range": self.date_handler.default_range,
            "recommendations": [
                "Consider simplifying the query",
                "Check data availability for the specified date range",
                "Verify API access and credentials"
            ]
        }

    def _calculate_query_complexity(self, query: str) -> str:
        """Calculate query complexity for optimization"""
        factors = {
            "length": len(query),
            "data_sources": len([source for source in ["analytics", "instagram", "ads", "clarity"] 
                               if source in query.lower()]),
            "date_complexity": "last" in query.lower() or "between" in query.lower(),
            "comparison_required": "compare" in query.lower() or "vs" in query.lower()
        }
        
        complexity_score = (
            factors["length"] / 100 +
            factors["data_sources"] * 2 +
            factors["date_complexity"] * 1.5 +
            factors["comparison_required"] * 2
        )
        
        if complexity_score < 3:
            return "low"
        elif complexity_score < 6:
            return "medium"
        else:
            return "high"

    async def _attempt_partial_results(
        self,
        error: Exception,
        query: str
    ) -> Optional[Dict[str, Any]]:
        """Attempt to provide partial results in case of errors"""
        try:
            # Try to get at least basic GA data
            date_range = self.date_handler.get_date_range(query)
            basic_ga_data = await self._get_ga_data(query, date_range)
            
            return {
                "available_data": {
                    "google_analytics": basic_ga_data
                },
                "limited_analysis": True,
                "error_context": str(error)
            }
            
        except Exception:
            return None