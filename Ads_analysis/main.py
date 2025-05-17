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

@tool(args_schema=PopulateArgSchema)
def fetchInstagramAccountData(input_arg: Optional[str] = "") -> str:
    """
    Fetch Instagram account metrics like
    - Impressions
    - Accounts reached
    - Content interactions
    - Profile link taps
    - Accounts engaged
    - Saves
    - Shares.
    """
        
    url = f"https://graph.facebook.com/v21.0/{instagram_account_id}/insights"

    base_params = {
        'metric': 'impressions, reach, total_interactions, profile_links_taps, accounts_engaged, saves, shares',
        'period': 'day',
        'access_token': ACCESS_TOKEN,
        'metric_type': 'total_value',
    }

    response = requests.get(url, params=base_params)

    if response.status_code == 200:

        data = response.json()
        
        # Create a formatted string to print the metrics
        metrics_str = input_arg
        metrics_str = "Instagram Metrics:\n"
        for item in data['data']:
            metrics_str += f"{item['title']} : {item['total_value']['value']}\n"

        prompt = """\nCompare Impressions and Accounts reached. What does this indicate about content visibility?\nAnalyze Content interactions and Accounts engaged. What does this suggest about user engagement?\nDiscuss Profile links taps, Saves, and Shares. What do these metrics tell you about user behavior and content value?\nSummarize key findings. Provide actionable recommendations to improve reach and engagement."""
        return metrics_str + prompt
    else:
        print(f"Error: {response.status_code}")
        print(response.json())

@tool(args_schema=PopulateArgSchema)
def fetchInstagramDemographicsData(input_arg: Optional[str] = "") -> str:
    """
    Fetches Egenie's Instagram account demographics insights. 
    Returns a string containing formatted metrics. 
    """
    data = input_arg
    data = demographics_insights()
    prompt = """\nAnalyze Country, City, Gender, and Age distributions. What do these metrics tell you about the follower base and engagement patterns?\nAnalyze Country, City, Gender, and Age distributions. How do these metrics compare with follower and engaged audience demographics?\nSummarize key findings. Provide actionable recommendations to improve audience targeting and engagement."""
    return data + prompt

@tool(args_schema=PopulateArgSchema)
def fetchInstagramMediaData(input_arg: Optional[str] = "") -> str:
    """
    Fetches insights for the most recent Instagram image and reel posts.
    """

    # Fetch user media
    params = getCreds()  # Fetch credentials
    response = getUserMedia(params)  # Get all media
    media_data = response['json_data']['data']  # Extract media list

    # Separate media by type
    most_recent_image = getMostRecentMediaByType(media_data, 'IMAGE')
    most_recent_reel = getMostRecentMediaByType(media_data, 'VIDEO')

    print(most_recent_image)

    post_insigths = input_arg

    print("test")

    # Print details for the most recent image
    if most_recent_image:
        post_insigths += "MOST RECENT IMAGE POST\n"
        # post_insigths += f"Link to post: {most_recent_image['permalink']}"
        post_insigths += f"Caption: {most_recent_image.get('caption', 'No caption')}\n"
        post_insigths += f"Posted at: {most_recent_image['timestamp']}\n"
        params['latest_media_id'] = most_recent_image['id']
        params['metric'] = 'follows,reach,saved,comments,shares,profile_visits,profile_activity'
        params['breakdown'] = 'action_type'
        image_insights = getMediaInsights(params)
        
        post_insigths += "\nIMAGE POST INSIGHTS\n"
        print(image_insights['json_data'])
        for insight in image_insights['json_data']['data']:
            post_insigths += f"{insight['title']}: {insight['values'][0]['value']}\n"

    # Print details for the most recent reel
    if most_recent_reel:
        post_insigths += "\nMOST RECENT REEL POST\n"
        # post_insigths += f"Link to post: {most_recent_reel['permalink']}\n"
        post_insigths += f"Caption: {most_recent_reel.get('caption', 'No caption')}\n"
        post_insigths += f"Posted at: {most_recent_reel['timestamp']}\n"
        params['latest_media_id'] = most_recent_reel['id']
        params['metric'] = 'saved,comments,shares,reach,ig_reels_avg_watch_time'
        reel_insights = getMediaInsights(params)
        
        post_insigths += "\nREEL POST INSIGHTS\n"
        for insight in reel_insights['json_data']['data']:
            post_insigths += f"{insight['title']}: {insight['values'][0]['value']}\n"
        print(reel_insights['json_data'])
    prompt = """ \nFor Post: \nAnalyze Impressions, Accounts reached, and Profile visits. What do these metrics tell you about post visibility and engagement? \n Discuss Comments, Shares, and Saved. What does this indicate about user interaction and content value?\nFor Reels: \nCompare Accounts reached, Initial plays, and Reels average watch time. What does this suggest about reel engagement and watch duration?\nAnalyze Comments, Shares, and Saved. How do these metrics compare with the image post?\nOverall Insights:\nSummarize key findings. Provide actionable recommendations to improve post and reel performance\nEnsure you pass the correct date range to get meaningful insights."""
    return post_insigths + prompt

@tool(args_schema=PopulateArgSchema)
def fetchMetaAdsData(input_ard: Optional[str] = "") -> str:
    """
    Retrieves maximum campaign insights.
    """
    campaign_id = "120217765393330368"  

    try:
        my_account = AdAccount(AD_ACCOUNT_ID)

        campaign_insights = my_account.get_insights(
            fields=[
                'campaign_name', 'clicks', 'cpc', 'ctr', 'spend', 'reach', 'impressions',
                'frequency', 'conversions', 'cost_per_conversion', 'conversion_values',
                'website_ctr', 'video_play_actions', 'video_avg_time_watched_actions',
                'objective', 'website_purchase_roas', 'cost_per_unique_click', 'actions',
                'action_values', 'outbound_clicks', 'cost_per_unique_outbound_click',
            ],
            params={
                "date_preset": "this_quarter",
                "breakdowns": ["age", "gender"],
                "level": "campaign",
                'filtering': [{'field': 'campaign.id', 'operator': 'EQUAL', 'value': campaign_id}],
                "limit": 1000
            }
        )
        campaign_insights_data = [insight._json for insight in campaign_insights]

        # Call the helper function to format data
        natural_language_string = format_insights_as_natural_language(campaign_insights_data)
        prompt = """Analyze Performance: Determine which age/gender groups excel in various metrics (CTR, CPC, conversions, etc.). Don't just calculate; understand why they perform well.\nIdentify Key Audiences: Pinpoint the current top performers and explain what makes them valuable.\nExplore New Opportunities: Uncover hidden potential in underperforming or untapped segments. Who could be persuaded with the right approach?\nOptimize Strategy: Targeting: Refine targeting based on performance, but consider expanding beyond obvious winners.\nBudget: Distribute budget strategically, not just to the top performers. Where will each dollar have the most impact?\nMessaging: How can you tailor your message to resonate with different groups? Think beyond simple demographics; consider their motivations and interests.\nSet your behaviour to what to do next approach. To make it better.Perform the actions and give direct conclusions."""
        return natural_language_string + prompt

    except Exception as e:
        print(f"Error: {e}")
        return f"Error retrieving insights: {e}"

def initialize_langchain_agent(llm, id):    

    memory = ChatMessageHistory(session_id=id)
    
    tools = [fetchInstagramAccountData, fetchInstagramDemographicsData, fetchInstagramMediaData, fetchMetaAdsData]
    prompt = hub.pull("admin-structured-chat-agent-prompt")
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
