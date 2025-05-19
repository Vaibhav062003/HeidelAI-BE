import os, time, requests
from langchain import hub
from dotenv import load_dotenv
from datetime import datetime, timedelta
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.chat_message_histories import ChatMessageHistory
from defines import getCreds, getMediaInsights, getUserMedia, getMostRecentMediaByType, demographics_insights
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.tools import tool
from langchain.agents import AgentExecutor, create_structured_chat_agent
# from google.analytics.data_v1beta import BetaAnalyticsDataClient
# from google.analytics.data_v1beta.types import (
#     DateRange,
#     Dimension,
#     Metric,     
#     RunReportRequest,
# )
from typing import Optional
from pydantic import BaseModel, Field
import json
# from google.cloud import firestore
# from google.oauth2 import service_account

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PROPERTY_ID = os.getenv("PROPERTY_ID")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
# os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'
# CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
APP_ID = os.getenv('APP_ID')
APP_SECRET = os.getenv('SECRET_TOKEN')
AD_ACCOUNT_ID = os.getenv('AD_ACCOUNT_ID')

FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)

os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGCHAIN_API_KEY")
os.environ["LANGCHAIN_PROJECT"] =os.getenv("LANGCHAIN_PROJECT")

# client = BetaAnalyticsDataClient()

# # Load the credentials from the JSON file
# credentials = service_account.Credentials.from_service_account_file(os.getenv("SERVICE_ACCOUNT_FILE"))

# # Initialize Firestore using the credentials
# db = firestore.Client(credentials=credentials)

def initialize_llm():
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0, )
    return llm

class PopulateArgSchema(BaseModel):
    input_arg: Optional[str] = Field(
        default="", 
        description="An empty string provided as input, used internally within the function."
    )
def format_insights_as_natural_language(insights_data):
    """Formats the insights data as a natural language string."""
    output_string = ""

    if not insights_data or not isinstance(insights_data, list):
        return "No insights data available or data is not in the expected format."

    for insight in insights_data:
        for key, value in insight.items():
          if isinstance(value, list):
              for item in value:
                  if isinstance(item, dict) and 'action_type' in item and 'value' in item:
                        action_type = item['action_type']
                        action_value = item['value']
                        output_string += f"{action_type}: {action_value}\n"  # Correct formatting
                  elif isinstance(item, dict):
                        for sub_key, sub_value in item.items():
                            output_string += f"  {sub_key}: {sub_value}\n"
                  else:  # List of simple values
                        output_string += f"  {key}: {', '.join(map(str, value))}\n"  # Correct formatting for simple lists
                        break  
          else:
              output_string += f"{key}: {value}\n"
        output_string += "--------------------\n"

    return output_string

@tool
def get_campaign_insights():
     """
     Fetches campaign insights from the Facebook Ads API and formats them into a natural language string.
     """
     try:
        my_account = AdAccount(AD_ACCOUNT_ID)

        campaign_insights = my_account.get_insights(
            fields = [
            'campaign_name',          # Name of the ad campaign
            'clicks',                # Number of clicks on the ad
            'cpc',                   # Cost per click
            'ctr',                   # Click-through rate (clicks/impressions)
            'spend',                 # Total amount spent on the campaign
            'reach',                 # Number of unique people who saw the ad
            'impressions',           # Total number of times the ad was shown
            'frequency',             # Average number of times each person saw the ad
            'conversions',           # Number of desired actions taken
            'cost_per_conversion',   # Average cost for each conversion
            'conversion_values',     # Monetary value of conversions
            # 'website_ctr',          # Click-through rate for website clicks
            'video_play_actions',    # Number of times videos were played
            'video_avg_time_watched_actions',  # Average duration of video views
            'objective',            # Campaign objective (e.g., awareness, conversions)
            'website_purchase_roas', # Return on ad spend for website purchases
            'cost_per_unique_click', # Cost per unique click
            'actions',              # All actions taken on the ad
            'action_values',        # Monetary values of different actions
            # 'outbound_clicks',      # Clicks that led away from Facebook
            # 'cost_per_unique_outbound_click', # Cost per unique outbound click
            # 'account_currency',     # Currency used for the account
            # 'account_id',          # Facebook Ad account ID
            # 'account_name',        # Name of the ad account
            'attribution_setting', # How conversions are attributed
            # 'ad_id',              # Individual ad ID
            # 'ad_name',            # Name of the specific ad
            # 'adset_id',          # Ad set identifier
            # 'adset_name',        # Name of the ad set
            # 'campaign_id',       # Campaign identifier
            # 'ad_click_actions',  # Detailed breakdown of click actions
            # 'ad_impression_actions', # Detailed breakdown of impression actions
            # 'auction_bid',       # Bid amount in auction
            # 'auction_competitiveness', # Level of auction competition
            # 'auction_max_competitor_bid', # Highest competitor bid
            # 'buying_type',       # Type of ad buying (auction/reach)
            'optimization_goal', # What the campaign is optimizing for
            # 'canvas_avg_view_percent', # Average view percentage of canvas ads
            # 'canvas_avg_view_time',    # Average viewing time of canvas ads
            # 'instant_experience_clicks_to_open', # Clicks to open instant experience
            # 'instant_experience_clicks_to_start', # Clicks to start instant experience
            # 'instant_experience_outbound_clicks', # Outbound clicks from instant experience
            # 'catalog_segment_actions', # Actions related to catalog segments
            # 'catalog_segment_value',   # Value from catalog segments/
            'catalog_segment_value_mobile_purchase_roas', # ROAS for mobile catalog purchases
            'catalog_segment_value_omni_purchase_roas',   # ROAS for omnichannel purchases
            # 'catalog_segment_value_website_purchase_roas', # ROAS for website catalog purchases
            'converted_product_app_custom_event_fb_mobile_purchase', # Mobile app purchases
            'converted_product_app_custom_event_fb_mobile_purchase_value', # Value of mobile app purchases
            # 'converted_product_offline_purchase', # Offline purchase conversions
            'converted_product_offline_purchase_value', # Value of offline purchases
            'converted_product_omni_purchase', # Omnichannel purchases
            'converted_product_omni_purchase_values', # Value of omnichannel purchases
            'converted_product_quantity', # Quantity of products converted
            'converted_product_value',    # Total value of converted products
            'converted_product_website_pixel_purchase', # Website pixel purchase conversions
            'converted_product_website_pixel_purchase_value', # Value of website purchases
            'converted_promoted_product_omni_purchase', # Promoted product omnichannel purchases
            'converted_promoted_product_omni_purchase_values', # Value of promoted omnichannel purchases
            'converted_promoted_product_quantity', # Quantity of promoted products converted
            'converted_promoted_product_value',    # Value of promoted product conversions
            'cost_per_15_sec_video_view', # Cost per 15-second video view
            'cost_per_2_sec_continuous_video_view', # Cost per 2-second continuous video view
            'cost_per_action_type',    # Cost breakdown by action type
            'cost_per_ad_click',       # Cost per ad click
            'cost_per_dda_countby_convs', # Cost per data-driven attribution conversion
            'cost_per_inline_link_click', # Cost per inline link click
            'cost_per_inline_post_engagement', # Cost per post engagement
            # 'cost_per_objective_result',
            'cost_per_one_thousand_ad_impression', # CPM (Cost per 1000 impressions)
            'cost_per_outbound_click', # Cost per outbound click
            # 'cost_per_result',
            'cost_per_thruplay',      # Cost per complete video view
            'cost_per_unique_action_type', # Cost per unique action type
            'cost_per_unique_conversion', # Cost per unique conversion
            'cost_per_unique_inline_link_click', # Cost per unique inline link click
            'created_time',           # When the ad was created
            'date_start',            # Start date of the data
            'date_stop',             # End date of the data
            'updated_time',          # Last update time
            'dda_countby_convs',     # Data-driven attribution conversion count
            'dda_results',           # Data-driven attribution results
            'inline_link_click_ctr', # CTR for inline links
            'inline_link_clicks',    # Number of inline link clicks
            'inline_post_engagement', # Post engagement metrics
            'interactive_component_tap', # Taps on interactive components
            'landing_page_view_per_link_click', # Landing page views per link click
            'marketing_messages_delivered', # Marketing messages successfully delivered
            'marketing_messages_delivery_rate', # Rate of message delivery
            'instagram_upcoming_event_reminders_set', # Instagram event reminder metrics
            'place_page_name',       # Name of the place page
            'social_spend',          # Amount spent on social impressions
            'product_brand',         # Brand of the product
            'product_category',      # Category of the product
            'product_custom_label_0', # Custom product label 0
            'product_custom_label_1', # Custom product label 1
            'product_custom_label_2', # Custom product label 2
            'product_custom_label_3', # Custom product label 3
            'product_custom_label_4', # Custom product label 4
            'product_group_retailer_id', # Retailer ID for product group
            'product_name',          # Name of the product
            'product_retailer_id',   # Retailer's product ID
            # 'objective_result_rate',
            # 'objective_results',
            'outbound_clicks_ctr',   # CTR for outbound clicks
            'purchase_per_landing_page_view', # Purchases per landing page view
            'purchase_roas',         # Return on ad spend for purchases
            'qualifying_question_qualify_answer_rate', # Answer rate for qualifying questions
            # 'result_rate',
            # 'result_values_performance_indicator',
            # 'results',
            'video_30_sec_watched_actions', # 30-second video views
            'video_continuous_2_sec_watched_actions', # 2-second continuous video views
            'video_p100_watched_actions', # Complete video views (100%)
            'video_p25_watched_actions',  # 25% video views
            'video_p50_watched_actions',  # 50% video views
            'video_p75_watched_actions',  # 75% video views
            'video_p95_watched_actions',  # 95% video views
            'video_play_curve_actions',   # Video play curve data
            'video_play_retention_0_to_15s_actions', # Video retention for 0-15 seconds
            'video_play_retention_20_to_60s_actions', # Video retention for 20-60 seconds
            'video_play_retention_graph_actions', # Video retention graph data
            'video_time_watched_actions', # Total video watch time
            'wish_bid',              # Desired bid amount
        ]
        ,
            params={
                "date_preset": "this_quarter",
                "breakdowns": ["age"],
                # "breakdowns": ["age", "gender"],
                "level": "campaign",
                'filtering': [{'field': 'campaign.id', 'operator': 'EQUAL', 'value': "120217765393330368"}],
                "limit": 1000
            }
        )
        campaign_insights_data = [insight._json for insight in campaign_insights]

        natural_language_string = format_insights_as_natural_language(campaign_insights_data)
        return natural_language_string

     except Exception as e:
        print(f"Error: {e}")
        return f"Error retrieving insights: {e}"

def initialize_langchain_agent(llm, id):    

    memory = ChatMessageHistory(session_id=id)
    
    tools = [get_campaign_insights]
    prompt = hub.pull("admin-ads-structured-chat-agent-prompt")
    agent = create_structured_chat_agent(llm, tools, prompt)
 
    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent, 
        tools=tools, 
        handle_parsing_errors=True,
        verbose=True)
    
    agent_with_chat_history = RunnableWithMessageHistory(
        agent_executor,
        lambda session_id: memory,
        input_messages_key="input",
        history_messages_key="chat_history",
    )
    
    return agent_with_chat_history

def get_agent(id: str):
    llm = initialize_llm()
    return initialize_langchain_agent(llm, id)

agent_executor = get_agent("admin1")

while True:
  
    user_input = input("User: ")
    tic = time.perf_counter()
    response = agent_executor.invoke(
        {"input": user_input},
        config={"configurable": {"session_id": "admin1"}},
    )

    toc = time.perf_counter()
    print(response['output'])
    print(f"time_taken {toc - tic:0.4f} seconds")
