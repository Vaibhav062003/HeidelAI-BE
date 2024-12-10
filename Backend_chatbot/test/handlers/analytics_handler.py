from typing import Dict, Any, List, Tuple
from datetime import datetime, timedelta
from utils.cache import cache_analytics
from .rate_handler import APIRateLimiter
from analytics.google_analytics import fetchGoogleAnalyticsData
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error, mean_absolute_percentage_error
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX
from prophet import Prophet
import pmdarima as pm
import pandas as pd
import numpy as np
from scipy import stats
import os
from facebook_business.api import FacebookAdsApi
import aiohttp
from google.cloud import firestore
from langchain import hub
from defines import getUserMedia, getCreds, getMediaInsights, demographics_insights
#from meta.meta_defines import demographics_insights
import asyncio
from dotenv import load_dotenv
load_dotenv()



API_SETTINGS = {
    "google_analytics": {
        "property_id": os.getenv("PROPERTY_ID"),
        "credentials_path": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        "requests_per_minute": 2,
        "max_retries": 3,
        "retry_delay": 1.0
    },
    "instagram": {
        "account_id": os.getenv("INSTAGRAM_ACCOUNT_ID"),
        "access_token": os.getenv("INSTAGRAM_ACCESS_TOKEN"),
        "app_id": os.getenv("INSTAGRAM_APP_ID"),
        "app_secret": os.getenv("INSTAGRAM_APP_SECRET"),
        "requests_per_hour": 200,
        "max_retries": 3,
        "retry_delay": 2.0
    },
    "meta_ads": {
        "account_id": os.getenv("META_ADS_ACCOUNT_ID"),
        "access_token": os.getenv("META_ADS_ACCESS_TOKEN"),
        "requests_per_hour": 200,
        "max_retries": 3,
        "retry_delay": 2.0
    },
    "clarity": {
        "project_id": os.getenv("CLARITY_PROJECT_ID"),
        "api_key": os.getenv("CLARITY_API_KEY"),
        "requests_per_hour": 100,
        "max_retries": 3,
        "retry_delay": 1.5
    }
}

ANALYTICS_SETTINGS = {
    "analysis": {
        "anomaly_std": 2.0,  # Number of standard deviations for anomaly detection
        "trend_significance": 0.05,  # P-value threshold for trend significance
        "correlation_threshold": 0.5,  # Minimum correlation coefficient to consider
        "seasonality_threshold": 0.1,  # Minimum threshold for seasonal patterns
        "change_point_threshold": 0.1  # Minimum threshold for change point detection
    }
}

class AnalyticsHandler:
    def __init__(self, llm_light, llm_heavy):
        self.llm_light = llm_light
        self.llm_heavy = llm_heavy
        self.rate_limiter = APIRateLimiter()
        self.ga_client = BetaAnalyticsDataClient()
        self.property_id = API_SETTINGS["google_analytics"]["property_id"]
        self.analysis_thresholds = ANALYTICS_SETTINGS["analysis"]
        
        # Initialize Facebook/Instagram API
        FacebookAdsApi.init(
            API_SETTINGS["instagram"]["app_id"],
            API_SETTINGS["instagram"]["app_secret"],
            API_SETTINGS["instagram"]["access_token"]
        )
        
    @cache_analytics
    async def process_analytics_query(self, query: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """Process analytics queries with proper rate limiting and caching"""
        try:
            # Use light LLM to determine analytics type
            analytics_type = await self.llm_light.agenerate([{
                "role": "user",
                "content": f"Classify this analytics query type (google_analytics, instagram, meta_ads, clarity, chatbot): {query}"
            }])
            
            analytics_type = analytics_type.generations[0].text.lower().strip()
            
            handlers = {
                "google_analytics": self.handle_ga_query,
                "instagram": self.handle_instagram_query,
                "meta_ads": self.handle_meta_ads_query,
                "clarity": self.handle_clarity_query,
                "chatbot": self.handle_chatbot_query
            }
            
            handler = handlers.get(analytics_type, self.handle_ga_query)
            return await handler(query, start_date, end_date)
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "error",
                "query": query,
                "date_range": {"start_date": start_date, "end_date": end_date}
            }

    async def handle_ga_query(self, query: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """Handle Google Analytics queries"""
        try:
            analysis_prompt = hub.pull("mod_prompt_ga4")
            
            # Ensure property_id exists
            if not self.property_id:
                raise ValueError("Google Analytics Property ID is not configured")
            
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[Dimension(name="date")],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="newUsers"),
                    Metric(name="sessions"),
                    Metric(name="screenPageViews"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            )
            
            event_response = await self.rate_limiter.execute_with_rate_limit(
                "google_analytics",
                self.ga_client.run_report,
                request
            )

            output = ""
            for row in event_response.rows:
                event_name = row.dimension_values[0].value
                event_count = row.metric_values[0].value
                event_label = f"{event_name.capitalize()}: {event_count}" if event_name != "scroll" else f"{event_name}: {event_count} times"
                output += event_label + "\n"

            # Additional metrics
            metrics = [
                Metric(name="activeUsers"),
                Metric(name="sessions"),
                Metric(name="averageSessionDuration"),
                Metric(name="sessionsPerUser"),
                Metric(name="scrolledUsers"),
                Metric(name="engagedSessions"),
                Metric(name="engagementRate"),
                Metric(name="addToCarts"),
                Metric(name="checkouts"),
                Metric(name="purchaserRate"),
            ]
            
            additional_request = RunReportRequest(
                property=f"properties/{self.property_id}",
                metrics=metrics,
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
            )
            
            additional_response = await self.rate_limiter.execute_with_rate_limit(
                "google_analytics",
                self.ga_client.run_report,
                additional_request
            )

            for row in additional_response.rows:
                for i, metric_value in enumerate(row.metric_values):
                    metric_name = additional_response.metric_headers[i].name
                    output += f"{metric_name}: {metric_value.value}\n"

            # Use heavy LLM for analysis with the existing prompt
            analysis = await self.llm_heavy.agenerate([{
                "role": "user",
                "content": f"{analysis_prompt}\n{output}"
            }])
            
            return {
                "data": output,
                "analysis": analysis.generations[0].text,
                "type": "google_analytics"
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "google_analytics"
            }

    async def _analyze_data(self, data: Dict[str, Any], data_type: str) -> Dict[str, Any]:
        """Comprehensive data analysis"""
        analysis_functions = {
            "google_analytics": self._analyze_ga_data,
            "instagram": self._analyze_instagram_data,
            "meta_ads": self._analyze_meta_ads_data
        }
        
        base_analysis = await analysis_functions.get(data_type, self._analyze_ga_data)(data)
        
        # Enrich analysis with LLM insights
        llm_insights = await self._get_llm_insights(base_analysis, data_type)
        
        return {
            **base_analysis,
            "llm_insights": llm_insights
        }

    async def _analyze_ga_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Advanced Google Analytics data analysis"""
        df = pd.DataFrame(data["timeline"])
        metrics_analysis = {}
        
        for metric in data["metrics"].keys():
            metric_data = df["metrics"].apply(lambda x: x.get(metric, 0))
            
            metrics_analysis[metric] = {
                "statistical_analysis": self._perform_statistical_analysis(metric_data),
                "trend_analysis": self._analyze_trend(metric_data),
                "anomalies": self._detect_anomalies(metric_data),
                "patterns": self._detect_patterns(metric_data, df["date"])
            }

        return {
            "metrics_analysis": metrics_analysis,
            "correlations": self._analyze_correlations(df),
            "user_behavior": self._analyze_user_behavior(data),
            "recommendations": self._generate_recommendations(metrics_analysis)
        }

    def _perform_statistical_analysis(self, data: pd.Series) -> Dict[str, Any]:
        """Detailed statistical analysis of metrics"""
        return {
            "basic_stats": {
                "mean": float(data.mean()),
                "median": float(data.median()),
                "std": float(data.std()),
                "min": float(data.min()),
                "max": float(data.max())
            },
            "distribution": {
                "skewness": float(stats.skew(data)),
                "kurtosis": float(stats.kurtosis(data)),
                "is_normal": stats.normaltest(data).pvalue > 0.05
            },
            "quartiles": {
                "q1": float(data.quantile(0.25)),
                "q2": float(data.quantile(0.50)),
                "q3": float(data.quantile(0.75)),
                "iqr": float(data.quantile(0.75) - data.quantile(0.25))
            }
        }

    def _analyze_trend(self, data: pd.Series) -> Dict[str, Any]:
        """Advanced trend analysis"""
        # Linear regression for trend
        x = np.arange(len(data))
        slope, intercept, r_value, p_value, std_err = stats.linregress(x, data)
        
        # Moving averages
        ma_7 = data.rolling(window=7).mean()
        ma_30 = data.rolling(window=30).mean()
        
        return {
            "trend_direction": "increasing" if slope > 0 else "decreasing",
            "trend_strength": abs(r_value),
            "significance": p_value < 0.05,
            "slope": float(slope),
            "moving_averages": {
                "weekly": ma_7.tolist(),
                "monthly": ma_30.tolist()
            },
            "seasonality": self._detect_seasonality(data)
        }

    def _detect_anomalies(self, data: pd.Series) -> List[Dict[str, Any]]:
        """Advanced anomaly detection"""
        mean = data.mean()
        std = data.std()
        threshold = self.analysis_thresholds["anomaly_std"]
        
        anomalies = []
        for idx, value in enumerate(data):
            z_score = (value - mean) / std
            if abs(z_score) > threshold:
                anomalies.append({
                    "index": idx,
                    "value": float(value),
                    "z_score": float(z_score),
                    "severity": "high" if abs(z_score) > threshold * 1.5 else "medium"
                })
        
        return anomalies

    def _detect_patterns(self, data: pd.Series, dates: pd.Series) -> Dict[str, Any]:
        """Pattern detection in time series data"""
        # Convert dates to datetime if they're strings
        dates = pd.to_datetime(dates)
        
        return {
            "weekly_pattern": self._analyze_weekly_pattern(data, dates),
            "monthly_pattern": self._analyze_monthly_pattern(data, dates),
            "growth_patterns": self._analyze_growth_patterns(data)
        }

    def _analyze_correlations(self, df: pd.DataFrame) -> Dict[str, float]:
        """Analyze correlations between different metrics"""
        metrics_df = pd.DataFrame([m for m in df["metrics"]])
        correlation_matrix = metrics_df.corr()
        
        significant_correlations = {}
        for col1 in correlation_matrix.columns:
            for col2 in correlation_matrix.columns:
                if col1 < col2:  # Avoid duplicates
                    corr = correlation_matrix.loc[col1, col2]
                    if abs(corr) > 0.5:  # Only strong correlations
                        significant_correlations[f"{col1}_vs_{col2}"] = float(corr)
        
        return significant_correlations

    def _analyze_user_behavior(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze user behavior patterns"""
        metrics = data.get("metrics", {})
        timeline = data.get("timeline", [])
        
        return {
            "engagement_metrics": {
                "avg_session_duration": self._calculate_avg_session_duration(metrics),
                "bounce_rate": self._calculate_bounce_rate(metrics),
                "user_retention": self._calculate_user_retention(timeline)
            },
            "user_segments": self._identify_user_segments(metrics),
            "conversion_funnel": self._analyze_conversion_funnel(metrics)
        }

    async def _get_llm_insights(self, analysis: Dict[str, Any], data_type: str) -> str:
        """Get LLM-generated insights based on the analysis"""
        analysis_prompt = self._generate_analysis_prompt(analysis, data_type)
        
        response = await self.llm_heavy.agenerate([{
            "role": "user",
            "content": analysis_prompt
        }])
        
        return response.generations[0].text

    def _generate_analysis_prompt(self, analysis: Dict[str, Any], data_type: str) -> str:
        """Generate a detailed prompt for LLM analysis"""
        return f"""Analyze the following {data_type} data and provide strategic insights:

1. Key Metrics Analysis:
{self._format_metrics_for_prompt(analysis.get('metrics_analysis', {}))}

2. Trends and Patterns:
{self._format_trends_for_prompt(analysis)}

3. Anomalies and Concerns:
{self._format_anomalies_for_prompt(analysis)}

4. User Behavior:
{self._format_user_behavior_for_prompt(analysis.get('user_behavior', {}))}

Please provide:
1. Key insights and their business implications
2. Specific recommendations for improvement
3. Potential areas of concern
4. Opportunities for growth
5. Competitive advantages and disadvantages
"""

    async def handle_instagram_query(self, query: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """Handle Instagram Analytics queries"""
        try:
            # Get credentials
            params = getCreds()
            
            # Asynchronously execute getUserMedia
            media_response = await self.rate_limiter.execute_with_rate_limit(
                "instagram",
                self._get_user_media_async,
                params
            )
            
            if 'error' in media_response.get('json_data', {}):
                return {
                    "error": media_response['json_data']['error']['message'],
                    "type": "instagram"
                }
            
            # Get insights for each media
            insights_data = []
            for media in media_response['json_data']['data']:
                params['latest_media_id'] = media['id']
                params['metric'] = 'engagement,impressions,reach,saved,video_views'
                
                media_insights = await self.rate_limiter.execute_with_rate_limit(
                    "instagram",
                    self._get_media_insights_async,
                    params
                )
                
                if 'error' not in media_insights.get('json_data', {}):
                    insights_data.append({
                        'media_id': media['id'],
                        'media_type': media['media_type'],
                        'timestamp': media['timestamp'],
                        'insights': media_insights['json_data']['data']
                    })
            
            # Get demographic insights
            demographics = await self.rate_limiter.execute_with_rate_limit(
                "instagram",
                self._get_demographics_async,
                params
            )
            
            # Format all the data
            formatted_data = self._format_instagram_response({
                'media': media_response['json_data']['data'],
                'insights': insights_data,
                'demographics': demographics
            })
            
            # Use heavy LLM for analysis
            analysis = await self.llm_heavy.agenerate([{
                "role": "user",
                "content": f"Analyze this Instagram data and provide insights: {formatted_data}"
            }])
            
            return {
                "data": formatted_data,
                "analysis": analysis.generations[0].text,
                "type": "instagram"
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "instagram"
            }

    async def _get_user_media_async(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Asynchronous wrapper for getUserMedia"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, getUserMedia, params)
    
    async def _get_media_insights_async(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Asynchronous wrapper for getMediaInsights"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, getMediaInsights, params)
    
    async def _get_demographics_async(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Asynchronous wrapper for demographics_insights"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, demographics_insights, params)

    def _format_instagram_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Format Instagram API response"""
        try:
            formatted_data = {
                "media_insights": [],
                "demographics": response.get('demographics', ''),
                "summary": {
                    "total_engagement": 0,
                    "total_impressions": 0,
                    "total_reach": 0,
                    "media_count": len(response.get('media', [])),
                    "media_types": {}
                }
            }
            
            # Process media and insights
            for media in response.get('media', []):
                media_type = media.get('media_type', 'unknown')
                
                # Count media types
                formatted_data['summary']['media_types'][media_type] = \
                    formatted_data['summary']['media_types'].get(media_type, 0) + 1
                
                # Find corresponding insights
                media_insights = next(
                    (item for item in response.get('insights', []) if item['media_id'] == media['id']),
                    None
                )
                
                if media_insights:
                    insight_entry = {
                        'id': media['id'],
                        'type': media_type,
                        'timestamp': media['timestamp'],
                        'permalink': media.get('permalink', ''),
                        'caption': media.get('caption', ''),
                        'metrics': {}
                    }
                    
                    # Process metrics
                    for insight in media_insights['insights']:
                        metric_name = insight['name']
                        metric_value = insight['values'][0]['value']
                        insight_entry['metrics'][metric_name] = metric_value
                        
                        # Update summary totals
                        if metric_name in ['engagement', 'impressions', 'reach']:
                            formatted_data['summary'][f'total_{metric_name}'] += metric_value
                    
                    formatted_data['media_insights'].append(insight_entry)
            
            return formatted_data
            
        except Exception as e:
            return {"error": str(e)}

    async def handle_chatbot_query(self, query: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """Handle Chatbot Analytics queries"""
        try:
            # Fetch basic chatbot metrics
            basic_metrics = await self._fetch_chatbot_metrics(start_date, end_date)
            
            # Fetch conversation data
            conversations = await self._fetch_chatbot_conversations(start_date, end_date)
            
            # Analyze conversations
            conversation_analysis = await self._analyze_conversations(conversations)
            
            return {
                "data": {
                    "metrics": basic_metrics,
                    "conversations": conversations,
                    "analysis": conversation_analysis
                },
                "type": "chatbot",
                "date_range": {
                    "start_date": start_date,
                    "end_date": end_date
                }
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "chatbot"
            }

    async def _fetch_chatbot_metrics(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Fetch basic chatbot metrics"""
        try:
            # Convert dates to datetime objects for comparison
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Initialize metrics
            metrics = {
                "total_conversations": 0,
                "avg_conversation_duration": 0,
                "total_messages": 0,
                "avg_user_messages_per_conversation": 0,
                "avg_bot_messages_per_conversation": 0,
                "successful_interactions": 0,
                "failed_interactions": 0,
                "user_satisfaction_rate": 0,
                "peak_usage_hours": [],
                "common_user_intents": {},
                "response_times": {
                    "avg_first_response_time": 0,
                    "avg_response_time": 0
                }
            }
            
            # Here you would typically query your database or analytics service
            # This is a placeholder for the actual implementation
            # Example using Firestore:
            db = firestore.Client()
            conversations_ref = db.collection('conversations')
            query = conversations_ref.where('timestamp', '>=', start).where('timestamp', '<=', end)
            
            conversations = query.stream()
            
            total_duration = 0
            total_user_messages = 0
            total_bot_messages = 0
            satisfaction_scores = []
            
            for conv in conversations:
                conv_data = conv.to_dict()
                metrics["total_conversations"] += 1
                
                # Calculate conversation duration
                if "start_time" in conv_data and "end_time" in conv_data:
                    duration = (conv_data["end_time"] - conv_data["start_time"]).total_seconds()
                    total_duration += duration
                
                # Count messages
                user_messages = len(conv_data.get("user_messages", []))
                bot_messages = len(conv_data.get("bot_messages", []))
                total_user_messages += user_messages
                total_bot_messages += bot_messages
                metrics["total_messages"] += user_messages + bot_messages
                
                # Track success/failure
                if conv_data.get("successful", False):
                    metrics["successful_interactions"] += 1
                else:
                    metrics["failed_interactions"] += 1
                
                # Track satisfaction
                if "satisfaction_score" in conv_data:
                    satisfaction_scores.append(conv_data["satisfaction_score"])
                
                # Track intents
                for intent in conv_data.get("intents", []):
                    metrics["common_user_intents"][intent] = metrics["common_user_intents"].get(intent, 0) + 1
            
            # Calculate averages
            if metrics["total_conversations"] > 0:
                metrics["avg_conversation_duration"] = total_duration / metrics["total_conversations"]
                metrics["avg_user_messages_per_conversation"] = total_user_messages / metrics["total_conversations"]
                metrics["avg_bot_messages_per_conversation"] = total_bot_messages / metrics["total_conversations"]
                
            if satisfaction_scores:
                metrics["user_satisfaction_rate"] = sum(satisfaction_scores) / len(satisfaction_scores)
            
            # Sort and limit common intents
            metrics["common_user_intents"] = dict(sorted(
                metrics["common_user_intents"].items(),
                key=lambda x: x[1],
                reverse=True
            )[:10])
            
            return metrics
            
        except Exception as e:
            raise Exception(f"Error fetching chatbot metrics: {str(e)}")

    async def _fetch_chatbot_conversations(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """Fetch detailed conversation data"""
        try:
            # Convert dates to datetime objects
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            
            # Query your database for conversations
            db = firestore.Client()
            conversations_ref = db.collection('conversations')
            query = conversations_ref.where('timestamp', '>=', start).where('timestamp', '<=', end)
            
            conversations = []
            for conv in query.stream():
                conv_data = conv.to_dict()
                conversations.append({
                    "id": conv.id,
                    "timestamp": conv_data.get("timestamp"),
                    "duration": conv_data.get("duration"),
                    "messages": conv_data.get("messages", []),
                    "intents": conv_data.get("intents", []),
                    "successful": conv_data.get("successful", False),
                    "satisfaction_score": conv_data.get("satisfaction_score")
                })
            
            return conversations
            
        except Exception as e:
            raise Exception(f"Error fetching conversations: {str(e)}")

    async def _analyze_conversations(self, conversations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Analyze conversation data"""
        # Here you would typically implement your conversation analysis logic
        # This is a placeholder for the actual implementation
        # Example using a machine learning model:
        df = pd.DataFrame(conversations)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['duration'] = pd.to_timedelta(df['duration'])
        df['successful'] = df['successful'].astype(int)
        df['satisfaction_score'] = df['satisfaction_score'].astype(float)
        
        # Feature engineering
        df['conversation_length'] = df['duration'].dt.total_seconds()
        df['avg_message_length'] = df['messages'].apply(lambda x: sum(len(msg) for msg in x) / len(x))
        df['sentiment'] = df['messages'].apply(lambda x: sum(1 for msg in x if msg.startswith('@')) / len(x))
        
        # Model training
        X = df[['conversation_length', 'avg_message_length', 'sentiment']]
        y = df['successful']
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        model = SARIMAX(y_train, exog=X_train, order=(1, 1, 1), seasonal_order=(1, 1, 1, 12))
        model_fit = model.fit(disp=False)
        
        # Prediction
        y_pred = model_fit.predict(exog=X_test)
        
        # Evaluation
        mse = mean_squared_error(y_test, y_pred)
        mape = mean_absolute_percentage_error(y_test, y_pred)
        
        return {
            "model_performance": {
                "mean_squared_error": mse,
                "mean_absolute_percentage_error": mape
            }
        }

    async def get_top_pages(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get top performing pages from Google Analytics"""
        try:
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[
                    Dimension(name="pagePath"),
                    Dimension(name="pageTitle")
                ],
                metrics=[
                    Metric(name="screenPageViews"),
                    Metric(name="activeUsers"),
                    Metric(name="averageSessionDuration")
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                limit=10  # Get top 10 pages
            )
            
            response = await self.rate_limiter.execute_with_rate_limit(
                "google_analytics",
                self.ga_client.run_report,
                request
            )
            
            # Format the response
            pages = []
            for row in response.rows:
                pages.append({
                    "path": row.dimension_values[0].value,
                    "title": row.dimension_values[1].value,
                    "pageviews": int(row.metric_values[0].value),
                    "users": int(row.metric_values[1].value),
                    "avg_duration": float(row.metric_values[2].value)
                })
            
            return {
                "top_pages": pages,
                "date_range": {
                    "start_date": start_date,
                    "end_date": end_date
                }
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "google_analytics"
            }

    async def get_device_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get device usage data from Google Analytics"""
        try:
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[
                    Dimension(name="deviceCategory")
                ],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="screenPageViews"),
                    Metric(name="sessions")
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
            )
            
            response = await self.rate_limiter.execute_with_rate_limit(
                "google_analytics",
                self.ga_client.run_report,
                request
            )
            
            # Format the response
            devices = []
            for row in response.rows:
                devices.append({
                    "device": row.dimension_values[0].value,
                    "users": int(row.metric_values[0].value),
                    "pageviews": int(row.metric_values[1].value),
                    "sessions": int(row.metric_values[2].value)
                })
            
            return {
                "devices": devices,
                "date_range": {
                    "start_date": start_date,
                    "end_date": end_date
                }
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "google_analytics"
            }

    async def get_ga_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get Google Analytics data for the specified date range"""
        try:
            request = RunReportRequest(
                property=f"properties/{self.property_id}",
                dimensions=[Dimension(name="date")],
                metrics=[
                    Metric(name="activeUsers"),
                    Metric(name="newUsers"),
                    Metric(name="sessions"),
                    Metric(name="screenPageViews"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            )
            
            response = await self.rate_limiter.execute_with_rate_limit(
                "google_analytics",
                self.ga_client.run_report,
                request
            )
            
            # Format the response
            formatted_data = self._format_ga_response(response)
            
            return {
                "data": formatted_data,
                "type": "google_analytics",
                "date_range": {
                    "start_date": start_date,
                    "end_date": end_date
                }
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "google_analytics"
            }

    async def get_clarity_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get Microsoft Clarity analytics data for the specified date range"""
        try:
            # Make API request to Clarity
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {API_SETTINGS['clarity']['api_key']}"
                }
                
                url = f"https://api.clarity.microsoft.com/v1/projects/{API_SETTINGS['clarity']['project_id']}/analytics"
                params = {
                    "start_date": start_date,
                    "end_date": end_date
                }
                
                async with session.get(url, headers=headers, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            "data": data,
                            "type": "clarity",
                            "date_range": {
                                "start_date": start_date,
                                "end_date": end_date
                            }
                        }
                    else:
                        return {
                            "error": f"Clarity API returned status code {response.status}",
                            "type": "clarity"
                        }
                        
        except Exception as e:
            return {
                "error": str(e),
                "type": "clarity"
            }

    async def get_chatbot_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get chatbot data for the specified date range"""
        try:
            # Fetch basic chatbot metrics
            basic_metrics = await self._fetch_chatbot_metrics(start_date, end_date)
            
            # Fetch conversation data
            conversations = await self._fetch_chatbot_conversations(start_date, end_date)
            
            # Analyze conversations
            conversation_analysis = await self._analyze_conversations(conversations)
            
            return {
                "data": {
                    "metrics": basic_metrics,
                    "conversations": conversations,
                    "analysis": conversation_analysis
                },
                "type": "chatbot",
                "date_range": {
                    "start_date": start_date,
                    "end_date": end_date
                }
            }
            
        except Exception as e:
            return {
                "error": str(e),
                "type": "chatbot"
            }

    async def get_instagram_account_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get Instagram account data for the specified date range"""
        try:
            # Get credentials from API settings
            params = {
                'metric': 'impressions,reach,profile_views,total_interactions,profile_links_taps,accounts_engaged,saves,shares',
                'period': 'day',
                'access_token': API_SETTINGS["instagram"]["access_token"],
                'metric_type': 'total_value'
            }
            
            url = f"https://graph.facebook.com/v21.0/{API_SETTINGS['instagram']['account_id']}/insights"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        formatted_data = {
                            "metrics": {},
                            "type": "instagram",
                            "date_range": {
                                "start_date": start_date,
                                "end_date": end_date
                            }
                        }
                        
                        # Format the metrics
                        for item in data.get('data', []):
                            formatted_data["metrics"][item.get('name')] = {
                                "value": item.get('total_value', {}).get('value'),
                                "title": item.get('title'),
                                "description": item.get('description')
                            }
                        
                        return formatted_data
                    else:
                        return {
                            "error": f"Instagram API returned status code {response.status}",
                            "type": "instagram"
                        }
                        
        except Exception as e:
            return {
                "error": str(e),
                "type": "instagram"
            }

    async def get_instagram_demographics(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get Instagram demographic data for the specified date range"""
        try:
            # Get credentials from API settings
            params = {
                'metric': 'audience_gender_age,audience_locale,audience_country,audience_city',
                'period': 'lifetime',  # Demographics are typically lifetime metrics
                'access_token': API_SETTINGS["instagram"]["access_token"]
            }
            
            url = f"https://graph.facebook.com/v21.0/{API_SETTINGS['instagram']['account_id']}/insights"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        return {
                            "demographics": data.get('data', []),
                            "type": "instagram",
                            "date_range": {
                                "start_date": start_date,
                                "end_date": end_date
                            }
                        }
                    else:
                        return {
                            "error": f"Instagram API returned status code {response.status}",
                            "type": "instagram"
                        }
                        
        except Exception as e:
            return {
                "error": str(e),
                "type": "instagram"
            }

    async def get_instagram_media_data(self, start_date: str, end_date: str) -> Dict[str, Any]:
        """Get Instagram media data for the specified date range"""
        try:
            # Get credentials from API settings
            params = {
                'fields': 'id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,username',
                'access_token': API_SETTINGS["instagram"]["access_token"]
            }
            
            url = f"https://graph.facebook.com/v21.0/{API_SETTINGS['instagram']['account_id']}/media"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Get insights for each media
                        media_insights = []
                        for media in data.get('data', []):
                            insights = await self._get_media_insights(media['id'])
                            if insights:
                                media_insights.append({
                                    'media_id': media['id'],
                                    'media_type': media['media_type'],
                                    'timestamp': media['timestamp'],
                                    'insights': insights
                                })
                        
                        return {
                            "media": data.get('data', []),
                            "insights": media_insights,
                            "type": "instagram",
                            "date_range": {
                                "start_date": start_date,
                                "end_date": end_date
                            }
                        }
                    else:
                        return {
                            "error": f"Instagram API returned status code {response.status}",
                            "type": "instagram"
                        }
                        
        except Exception as e:
            return {
                "error": str(e),
                "type": "instagram"
            }

    async def _get_media_insights(self, media_id: str) -> Dict[str, Any]:
        """Helper method to get insights for a specific media"""
        params = {
            'metric': 'engagement,impressions,reach,saved,video_views',
            'access_token': API_SETTINGS["instagram"]["access_token"]
        }
        
        url = f"https://graph.facebook.com/v21.0/{media_id}/insights"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('data', [])
                return None