import os, time
from dotenv import load_dotenv
from datetime import datetime
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain import hub
from handlers.date_handler import DateRangeHandler
from handlers.query_handler import QueryHandler
from handlers.analytics_handler import AnalyticsHandler
from utils.cache import AnalyticsCache
from analytics.comparision import ComparisonAnalytics
try:
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
except ImportError:
    print("Failed to import BetaAnalyticsDataClient. Make sure google-analytics-data package is installed.")
    print("Try: pip install google-analytics-data")
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)
import asyncio
from typing import Optional
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from facebook_business.api import FacebookAdsApi

# Load environment variables first
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL_LIGHT = os.getenv("LLM_MODEL_LIGHT")
LLM_MODEL_HEAVY = os.getenv("LLM_MODEL_HEAVY")
PROPERTY_ID = os.getenv("PROPERTY_ID")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'
CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
APP_ID = os.getenv('APP_ID')
APP_SECRET = os.getenv('SECRET_TOKEN')
AD_ACCOUNT_ID = os.getenv('AD_ACCOUNT_ID')

# Initialize Facebook API
FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)

# Set Langchain environment variables
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGCHAIN_API_KEY")
os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT")

class EnhancedChatbot:
    def __init__(self):
        self.llm_light = ChatGoogleGenerativeAI(
            model=LLM_MODEL_LIGHT, 
            temperature=0
        )
        self.llm_heavy = ChatGoogleGenerativeAI(
            model=LLM_MODEL_HEAVY, 
            temperature=0
        )
        self.date_handler = DateRangeHandler()
        self.query_handler = QueryHandler(self.llm_light, self.llm_heavy)
        self.analytics_handler = AnalyticsHandler(
            self.llm_light, 
            self.llm_heavy
        )
        self.comparison_analytics = ComparisonAnalytics()
        self.cache = AnalyticsCache()

    async def initialize_agent(self, session_id: str):
        """Initialize the agent with all necessary tools and configurations"""
        from langchain.tools import StructuredTool
        
        # Convert all functions to StructuredTools
        tools = [
            StructuredTool.from_function(
                func=self.date_handler.get_date_range,
                name="getDateRange",
                description="Extracts and validates date ranges from user queries"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.process_analytics_query,
                name="processAnalyticsQuery",
                description="Processes analytics-related queries"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.handle_ga_query,
                name="handleGAQuery",
                description="Handles Google Analytics specific queries"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.handle_instagram_query,
                name="handleInstagramQuery",
                description="Handles Instagram-related queries"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.handle_chatbot_query,
                name="handleChatbotQuery",
                description="Handles chatbot-related queries"
            ),
            StructuredTool.from_function(
                func=self.comparison_analytics.compare_periods,
                name="comparePeriods",
                description="Compares analytics data between different time periods"
            )
        ]

        # Convert all dictionary-style tools to proper Tool objects
        tools.extend([
            StructuredTool.from_function(
                func=self.analytics_handler.get_top_pages,
                name="topNMostVisitedPages",
                description="Fetches the top n most visited pages of the website"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_device_data,
                name="fetchDeviceModelsData",
                description="Fetches the data for users' device models"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_ga_data,
                name="fetchGoogleAnalyticsData",
                description="Fetches Google Analytics data for the specified date range"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_clarity_data,
                name="fetchClarityData",
                description="Retrieves data from Clarity of the last 3 days"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_chatbot_data,
                name="fetchChatbotConversations",
                description="Returns the customer chatbot conversations"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_instagram_account_data,
                name="fetchInstagramAccountData",
                description="Fetch Instagram account metrics"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_instagram_demographics,
                name="fetchInstagramDemographicsData",
                description="Fetches Instagram account demographics insights"
            ),
            StructuredTool.from_function(
                func=self.analytics_handler.get_instagram_media_data,
                name="fetchInstagramMediaData",
                description="Fetches insights for Instagram posts"
            )
        ])

        # Create memory for chat history
        memory = ChatMessageHistory(session_id=session_id)

        # Pull the prompt template with the correct ID
        prompt = hub.pull("mod_prompt_main_admin")

        # Create the agent
        agent = create_structured_chat_agent(
            llm=self.llm_heavy,
            tools=tools,
            prompt=prompt
        )

        # Create the agent executor
        self.agent_executor = AgentExecutor.from_agent_and_tools(
            agent=agent,
            tools=tools,
            handle_parsing_errors=True,
            verbose=True,
            max_iterations=5,
            return_intermediate_steps=True
        )

        # Add chat history capability
        self.agent_with_chat_history = RunnableWithMessageHistory(
            self.agent_executor,
            lambda _session_id: memory,
            input_messages_key="input",
            history_messages_key="chat_history"
        )

        return self.agent_with_chat_history

    async def process_user_input(self, user_input: str, session_id: str) -> str:
        try:
            # Extract date information and analytics intent
            query_result = await self.query_handler.process_query(user_input)
            
            # Use agent_with_chat_history instead of agent_executor
            response = await self.agent_with_chat_history.invoke(
                {
                    "input": user_input,
                    "date": query_result["date_range"],
                    "session_id": session_id
                },
                config={"configurable": {"session_id": session_id}}
            )
            
            return response["output"]
            
        except Exception as e:
            return f"An error occurred: {str(e)}"

    async def handle_comparison_query(self, query: str, query_result: dict) -> str:
        try:
            comparison_result = await self.comparison_analytics.compare_periods(
                query,
                query_result["date_range"],
                self.analytics_handler
            )
            
            # Use heavy LLM to analyze comparison
            analysis = await self.llm_heavy.agenerate([{
                "role": "user",
                "content": f"Analyze this comparison data and provide insights: {comparison_result}"
            }])
            
            return analysis.generations[0].text
            
        except Exception as e:
            return f"Error in comparison analysis: {str(e)}"

async def main():
    chatbot = EnhancedChatbot()
    await chatbot.initialize_agent("admin1")
    
    while True:
        try:
            user_input = input("User: ")
            tic = time.perf_counter()
            
            response = await chatbot.process_user_input(user_input, "admin1")
            
            toc = time.perf_counter()
            print(response)
            print(f"Time taken: {toc - tic:0.4f} seconds")
            
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())