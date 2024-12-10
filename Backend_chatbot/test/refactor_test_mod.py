import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.cloud import firestore
from google.oauth2 import service_account
from langchain import hub
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.tools import tool
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

import logging

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load Environment Variables
load_dotenv()

# Configuration Variables
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL_LIGHT = os.getenv("LLM_MODEL_LIGHT")
LLM_MODEL_HEAVY = os.getenv("LLM_MODEL_HEAVY")
PROPERTY_ID = os.getenv("PROPERTY_ID")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE", 'credentials.json')

# Set Environment Variables for Langchain
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = LANGCHAIN_API_KEY
os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT")

# Initialize Clients
try:
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
    db = firestore.Client(credentials=credentials)
    client = BetaAnalyticsDataClient()
    logger.info("Initialized Firestore and BetaAnalyticsDataClient successfully.")
except Exception as e:
    logger.error(f"Error initializing services: {e}")
    raise

# Initialize Sentence Transformer Model
model = SentenceTransformer('all-MiniLM-L6-v2')
logger.info("SentenceTransformer model loaded successfully.")

def initialize_llm() -> Tuple[ChatGoogleGenerativeAI, ChatGoogleGenerativeAI]:
    """Initialize Light and Heavy LLM models."""
    try:
        llm_light = ChatGoogleGenerativeAI(model=LLM_MODEL_LIGHT, temperature=0)
        llm_heavy = ChatGoogleGenerativeAI(model=LLM_MODEL_HEAVY, temperature=0)
        logger.info("LLM models initialized successfully.")
        return llm_light, llm_heavy
    except Exception as e:
        logger.error(f"Error initializing LLM models: {e}")
        raise

class ChatbotConversationsSchema(BaseModel):
    """Schema for chatbot conversation tools."""
    optional_arg: Optional[str] = Field(None, description="Optional argument (not used)") 

@tool
def getDateRange(date_range: str) -> dict:
    """
    Returns the start date and end date for the specified date range.
    """
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d')

    date_ranges = {
        "today": {"start_date": today_str, "end_date": today_str},
        "yesterday": {
            "start_date": (today - timedelta(days=1)).strftime('%Y-%m-%d'),
            "end_date": (today - timedelta(days=1)).strftime('%Y-%m-%d')
        },
        "last week": {
            "start_date": (today - timedelta(days=7)).strftime('%Y-%m-%d'),
            "end_date": (today - timedelta(days=1)).strftime('%Y-%m-%d')
        },
        "this year": {
            "start_date": datetime(today.year, 1, 1).strftime('%Y-%m-%d'),
            "end_date": today_str
        },
        "last year": {
            "start_date": datetime(today.year - 1, 1, 1).strftime('%Y-%m-%d'),
            "end_date": datetime(today.year - 1, 12, 31).strftime('%Y-%m-%d')
        }
    }

    if date_range.lower() in ["last month", "this month"]:
        first_day_of_current_month = datetime(today.year, today.month, 1)
        if date_range.lower() == "last month":
            last_month_end = first_day_of_current_month - timedelta(days=1)
            last_month_start = datetime(last_month_end.year, last_month_end.month, 1)
            selected_range = {
                "start_date": last_month_start.strftime('%Y-%m-%d'),
                "end_date": last_month_end.strftime('%Y-%m-%d')
            }
        else:  # this month
            selected_range = {
                "start_date": first_day_of_current_month.strftime('%Y-%m-%d'),
                "end_date": today_str
            }
    else:
        selected_range = date_ranges.get(date_range.lower(), date_ranges["this year"])

    logger.debug(f"Selected date range: {selected_range}")
    return selected_range

@tool
def topNMostVisitedPages(n: int = 10) -> list:
    """
    Fetches the top n most visited pages of the website.
    """
    try:
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[Dimension(name="unifiedScreenClass")],
            metrics=[Metric(name="screenPageViews")],
            date_ranges=[DateRange(start_date="2024-01-01", end_date="today")],
        )
        response = client.run_report(request)
        logger.info("Fetched top N most visited pages.")

        excluded_pages = ['Home - Egenie4u']
        pages = [
            row.dimension_values[0].value
            for i, row in enumerate(response.rows)
            if row.dimension_values[0].value not in excluded_pages and i < n
        ]

        logger.debug(f"Top {n} most visited pages: {pages}")
        return pages
    except Exception as e:
        logger.error(f"Error fetching top N most visited pages: {e}")
        return []

@tool
def fetchDeviceModelsData(n: int = 10) -> str:
    """
    Fetches the data for users' device models.
    """
    try:
        today_str = datetime.today().strftime('%Y-%m-%d')
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[Dimension(name="deviceModel")],
            metrics=[Metric(name="activeUsers")],
            date_ranges=[DateRange(start_date="2024-01-01", end_date=today_str)],
            limit=n
        )
        response = client.run_report(request)

        device_data = "\n".join(
            f"{row.dimension_values[0].value} - {row.metric_values[0].value}"
            for row in response.rows
        )
        device_data_str = "Device Model - Active Users\n" + device_data
        logger.info("Fetched device models data.")

        return device_data_str
    except Exception as e:
        logger.error(f"Error fetching device models data: {e}")
        return "Failed to fetch device models data."

@tool
def fetchGoogleAnalyticsData(start_date: str, end_date: str) -> str:
    """
    Fetches Google Analytics data for the specified date range.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_ga4")
        date_range = DateRange(start_date=start_date, end_date=end_date)

        # Event Report
        event_request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[Dimension(name="eventName")],
            metrics=[Metric(name="eventCount")],
            date_ranges=[date_range],
        )
        event_response = client.run_report(event_request)

        output = ""
        for row in event_response.rows:
            event_name = row.dimension_values[0].value
            event_count = row.metric_values[0].value
            event_label = f"{event_name.capitalize()}: {event_count}" if event_name != "scroll" else f"{event_name}: {event_count} times"
            output += event_label + "\n"

        # User Type Report
        user_type_request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            dimensions=[Dimension(name="newVsReturning")],
            metrics=[Metric(name="activeUsers")],
            date_ranges=[date_range]
        )
        user_type_response = client.run_report(user_type_request)

        new_users = 0
        returning_users = 0
        for row in user_type_response.rows:
            user_type = row.dimension_values[0].value
            active_users = int(row.metric_values[0].value)
            if user_type == "new":
                new_users += active_users
            elif user_type == "returning":
                returning_users += active_users

        total_users = new_users + returning_users
        if total_users > 0:
            new_pct = (new_users / total_users) * 100
            returning_pct = (returning_users / total_users) * 100
        else:
            new_pct = returning_pct = 0

        output += (
            f"New Visitors: {new_users}\n"
            f"Returning Visitors: {returning_users}\n"
            f"New Visitors Percentage: {new_pct:.2f}%\n"
            f"Returning Visitors Percentage: {returning_pct:.2f}%\n"
        )

        # Additional Metrics Report
        additional_metrics = [
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
            property=f"properties/{PROPERTY_ID}",
            metrics=additional_metrics,
            date_ranges=[date_range]
        )
        additional_response = client.run_report(additional_request)

        for row in additional_response.rows:
            for i, metric_value in enumerate(row.metric_values):
                metric_name = additional_response.metric_headers[i].name
                output += f"{metric_name}: {metric_value.value}\n"

        # Analyze with LLM
        _, llm_heavy = initialize_llm()
        analysis = llm_heavy.invoke(f"{analysis_prompt}\n{output}")
        logger.info("Analyzed Google Analytics data with LLM.")

        return analysis
    except Exception as e:
        logger.error(f"Error fetching Google Analytics data: {e}")
        return "Failed to fetch Google Analytics data."

@tool
def fetchClarityData(optional_arg: Optional[str] = None) -> str:
    """
    Retrieves data from Clarity for the last 3 days.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_clarity")
        params = {"numOfDays": "3", "dimension1": "PopularPages"}
        headers = {
            "Authorization": f"Bearer {CLARITY_TOKEN}",
            "Content-type": "application/json"
        }
        response = requests.get(
            "https://www.clarity.ms/export-data/api/v1/project-live-insights",
            params=params,
            headers=headers,
        )
        if response.status_code != 200:
            logger.warning("Clarity API limit reached.")
            return "API limit reached. Please try again later."

        data = response.json()
        result = ""

        for metric in data:
            metric_name = metric.get('metricName', 'Unknown Metric')
            information = metric.get('information', [{}])

            if len(information) == 1:
                info = information[0]
                if metric_name == "DeadClickCount":
                    result += f"Dead Clicks:\n"
                    result += f"Total Dead Clicks: {info.get('subTotal', 'N/A')}\n"
                    result += f"Sessions with Dead Clicks: {info.get('sessionsWithMetricPercentage', 'N/A')}%\n"
                    result += f"Sessions without Dead Clicks: {info.get('sessionsWithoutMetricPercentage', 'N/A')}%\n"
                elif metric_name == "ScrollDepth":
                    result += f"Scroll Metrics:\n"
                    result += f"Average Scroll Depth: {info.get('averageScrollDepth', 'N/A')}%\n"
                elif metric_name == "Traffic":
                    result += f"Traffic and Session Metrics:\n"
                    result += f"Total Session Count: {info.get('totalSessionCount', 'N/A')}\n"
                    result += f"Total Bot Session Count: {info.get('totalBotSessionCount', 'N/A')}\n"
                    result += f"Distinct User Count: {info.get('distinctUserCount', 'N/A')}\n"
                    result += f"Pages Per Session Percentage: {info.get('pagesPerSessionPercentage', 'N/A'):.2f}%\n"
                elif metric_name == "EngagementTime":
                    result += f"Engagement Time:\n"
                    result += f"Total Time: {info.get('totalTime', 'N/A')} seconds\n"
                    result += f"Active Time: {info.get('activeTime', 'N/A')} seconds\n"
            elif len(information) > 1 and metric_name == "PageTitle":
                result += f"Page Titles:\n"
                for entry in information:
                    if isinstance(entry, dict):
                        result += f"\"{entry.get('name', 'N/A')}\" - {entry.get('sessionsCount', 'N/A')} sessions\n"

        # Analyze with LLM
        llm_heavy, _ = initialize_llm()
        analysis = llm_heavy.invoke(f"{analysis_prompt}\n{result}")
        logger.info("Analyzed Clarity data with LLM.")

        return analysis
    except Exception as e:
        logger.error(f"Error fetching Clarity data: {e}")
        return "Failed to fetch Clarity data."

@tool
def fetchChatbotConversations(optional_arg: Optional[str] = None) -> str:
    """
    Returns the customer chatbot conversations.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_chat_conversation")
        collection_ref = db.collection("HistoryMessages")
        docs = collection_ref.stream()

        combined_messages = ["User history:"]

        for doc in docs:
            doc_data = doc.to_dict()
            messages = doc_data.get('messages', [])

            for message in messages:
                if isinstance(message, bytes):
                    try:
                        message_str = message.decode('utf-8')
                        message_data = json.loads(message_str)
                        content = message_data.get('content', '')
                        sender_type = message_data.get('type', '')
                        combined_messages.append(f"{sender_type.capitalize()}: {content}")
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        logger.warning(f"Error decoding message: {e}")
                        continue

            if len(combined_messages) >= 100:
                combined_messages = combined_messages[:100]
                break

        combined_string = "\n".join(combined_messages)
        logger.debug(f"Combined messages count: {len(combined_messages)}")

        # Analyze with LLM
        _, llm_heavy = initialize_llm()
        analysis = llm_heavy.invoke(f"{analysis_prompt}\n{combined_string}")
        logger.info("Analyzed chatbot conversations with LLM.")

        return analysis
    except Exception as e:
        logger.error(f"Error fetching chatbot conversations: {e}")
        return "Failed to fetch chatbot conversations."

@tool
def fetchInstagramAccountData(optional_arg: Optional[str] = None) -> str:
    """
    Fetch Instagram account metrics.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_instaacc")
        url = f"https://graph.facebook.com/v21.0/{INSTAGRAM_ACCOUNT_ID}/insights"
        params = {
            'metric': 'impressions,reach,total_interactions,profile_links_taps,accounts_engaged,saves,shares',
            'period': 'day',
            'access_token': ACCESS_TOKEN,
            'metric_type': 'total_value',
        }

        response = requests.get(url, params=params)
        if response.status_code == 200:
            data = response.json()
            metrics_str = "Instagram Metrics:\n"
            for item in data.get('data', []):
                metrics_str += f"{item.get('title', 'No Title')} : {item.get('total_value', {}).get('value', 'N/A')}\n"

            # Analyze with LLM
            llm_light, _ = initialize_llm()
            analysis = llm_light.invoke(f"{analysis_prompt}\n{metrics_str}")
            logger.info("Analyzed Instagram account data with LLM.")

            return analysis
        else:
            logger.warning(f"Failed to fetch Instagram data: {response.status_code}")
            return "Failed to fetch Instagram account data."
    except Exception as e:
        logger.error(f"Error fetching Instagram account data: {e}")
        return "Failed to fetch Instagram account data."

@tool
def fetchDemographicsData(optional_arg: Optional[str] = None) -> str:
    """
    Fetches Instagram account demographics insights.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_demographics")
        data = demographics_insights()

        # Analyze with LLM
        llm_heavy, _ = initialize_llm()
        analysis = llm_heavy.invoke(f"{analysis_prompt}\n{data}")
        logger.info("Analyzed demographics data with LLM.")

        return analysis
    except Exception as e:
        logger.error(f"Error fetching demographics data: {e}")
        return "Failed to fetch demographics data."

@tool
def fetchInstagramMediaData(optional_arg: Optional[str] = None) -> str:
    """
    Fetches insights for the most recent Instagram image and reel posts.
    """
    try:
        analysis_prompt = hub.pull("mod_prompt_fetchinsightsinsta")
        params = getCreds()

        response = getUserMedia(params)
        media_data = response.get('json_data', {}).get('data', [])

        # Separate media by type
        most_recent_image = getMostRecentMediaByType(media_data, 'IMAGE')
        most_recent_reel = getMostRecentMediaByType(media_data, 'VIDEO')

        post_insights = ""

        if most_recent_image:
            post_insights += "MOST RECENT IMAGE POST\n"
            post_insights += f"Caption: {most_recent_image.get('caption', 'No caption')}\n"
            post_insights += f"Posted at: {most_recent_image.get('timestamp', 'N/A')}\n"
            params['latest_media_id'] = most_recent_image['id']
            params['metric'] = 'follows,impressions,reach,saved,comments,shares,profile_visits,profile_activity'
            image_insights = getMediaInsights(params)

            post_insights += "\nIMAGE POST INSIGHTS\n"
            for insight in image_insights.get('json_data', {}).get('data', []):
                post_insights += f"{insight.get('title', 'N/A')}: {insight.get('values', [{}])[0].get('value', 'N/A')}\n"

        if most_recent_reel:
            post_insights += "\nMOST RECENT REEL POST\n"
            post_insights += f"Caption: {most_recent_reel.get('caption', 'No caption')}\n"
            post_insights += f"Posted at: {most_recent_reel.get('timestamp', 'N/A')}\n"
            params['latest_media_id'] = most_recent_reel['id']
            params['metric'] = 'saved,comments,shares,reach,plays,ig_reels_avg_watch_time'
            reel_insights = getMediaInsights(params)

            post_insights += "\nREEL POST INSIGHTS\n"
            for insight in reel_insights.get('json_data', {}).get('data', []):
                post_insights += f"{insight.get('title', 'N/A')}: {insight.get('values', [{}])[0].get('value', 'N/A')}\n"

        # Analyze with LLM
        llm_heavy, _ = initialize_llm()
        analysis = llm_heavy.invoke(f"{analysis_prompt}\n{post_insights}")
        logger.info("Analyzed Instagram media data with LLM.")

        return analysis
    except Exception as e:
        logger.error(f"Error fetching Instagram media data: {e}")
        return "Failed to fetch Instagram media data."

def initialize_langchain_agent(llm_heavy: ChatGoogleGenerativeAI, session_id: str) -> RunnableWithMessageHistory:
    """
    Initialize the LangChain agent with the provided LLM and session ID.
    """
    try:
        memory = ChatMessageHistory(session_id=session_id)
        tools = [
            getDateRange,
            topNMostVisitedPages,
            fetchDeviceModelsData,
            fetchGoogleAnalyticsData,
            fetchClarityData,
            fetchChatbotConversations,
            fetchInstagramAccountData,
            fetchDemographicsData,
            fetchInstagramMediaData
        ]
        prompt = hub.pull("mod_prompt_main_admin")
        agent = create_structured_chat_agent(llm_heavy, tools, prompt)

        agent_executor = AgentExecutor.from_agent_and_tools(
            agent=agent,
            tools=tools,
            handle_parsing_errors=True,
            verbose=True
        )

        agent_with_history = RunnableWithMessageHistory(
            agent_executor,
            lambda _: memory,
            input_messages_key="input",
            history_messages_key="chat_history",
        )

        logger.info("LangChain agent initialized successfully.")
        return agent_with_history
    except Exception as e:
        logger.error(f"Error initializing LangChain agent: {e}")
        raise

def get_agent(session_id: str) -> RunnableWithMessageHistory:
    """
    Get the initialized agent for a given session.
    """
    llm_light, llm_heavy = initialize_llm()
    return initialize_langchain_agent(llm_heavy, session_id)

def main():
    """
    Main loop to interact with the user.
    """
    try:
        agent_executor = get_agent("admin1")
        logger.info("Agent executor initialized. Ready to receive user input.")

        while True:
            user_input = input("User: ").strip()
            if user_input.lower() in {"exit", "quit"}:
                logger.info("Exiting the chatbot.")
                break

            tic = time.perf_counter()
            response = agent_executor.invoke(
                {
                    "input": user_input,
                    "date": datetime.now().strftime('%Y-%m-%d'),
                },
                config={"configurable": {"session_id": "admin1"}},
            )
            toc = time.perf_counter()
            logger.info(f"Response: {response.get('output', 'No output')}")
            logger.info(f"Time taken: {toc - tic:0.4f} seconds")

    except KeyboardInterrupt:
        logger.info("Chatbot terminated by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    main()