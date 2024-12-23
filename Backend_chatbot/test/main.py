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
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)
from typing import Optional
from pydantic import BaseModel, Field
import json
from google.cloud import firestore
from google.oauth2 import service_account

from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PROPERTY_ID = os.getenv("PROPERTY_ID")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'
CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

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

client = BetaAnalyticsDataClient()

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(os.getenv("SERVICE_ACCOUNT_FILE"))

# Initialize Firestore using the credentials
db = firestore.Client(credentials=credentials)

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
def getDateRange(date_range: str):
    """
    Returns the start date and end date for the specified date range.
    Use this tool to get the date ranges to call other tools.

    Args:
        date_range (str): The date range for the query. Options include:
            - "today": Fetch data for the current day.
            - "yesterday": Fetch data for the previous day.
            - "last week": Fetch data for the previous week.
            - "last month": Fetch data for the previous calendar month.
            - "this month": Fetch data for the current month.
            - "this year": Fetch data from the start of the current year to today.
            - "last year": Fetch data from the start of the previous year to the end of that year.

    Returns:
        dict: A dictionary containing "start_date" and "end_date" for the specified date range.
    """
    today = datetime.today()
    today_str = today.strftime('%Y-%m-%d')

    # Define date ranges based on user input
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

    # Calculate special date ranges
    if date_range.lower() == "last month":
        first_day_of_this_month = datetime(today.year, today.month, 1)
        last_month_end = first_day_of_this_month - timedelta(days=1)
        last_month_start = datetime(last_month_end.year, last_month_end.month, 1)
        selected_range = {
            "start_date": last_month_start.strftime('%Y-%m-%d'),
            "end_date": last_month_end.strftime('%Y-%m-%d')
        }
    elif date_range.lower() == "this month":
        first_day_of_this_month = datetime(today.year, today.month, 1)
        selected_range = {
            "start_date": first_day_of_this_month.strftime('%Y-%m-%d'),
            "end_date": today_str
        }
    else:
        selected_range = date_ranges.get(date_range.lower(), date_ranges["this year"])

    return selected_range

@tool
def topNMostVisitedPages(n : int = 10) -> list:
    """
    Fetches the top n most visited pages of the website.

    Args:
        n (int): The number of pages to fetch.
    Returns:
        list: A list of page URLs from most to least visited.   
    """

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="unifiedScreenClass")],
        metrics=[Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date="2024-01-01", end_date="today")],
    )
    response = client.run_report(request)

    excluded_pages = ['Home - Egenie4u']
    pages = []
    for i, row in enumerate(response.rows):
        if row.dimension_values[0].value not in excluded_pages:    
            pages.append(row.dimension_values[0].value)
            if i > n - 1:
                break

    return pages

@tool
def fetchDeviceModelsData(n : int = 10):
    """
    Fetches the data for users' device models
    args:
        int: The number of devices to return.
    """
    client = BetaAnalyticsDataClient()

    # Set the current date
    today = datetime.today().strftime('%Y-%m-%d')

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="deviceModel")],
        metrics=[Metric(name="activeUsers")], 
        date_ranges=[DateRange(start_date="2024-01-01", end_date=today)],
        limit = n
    )

    response = client.run_report(request)

    # Format the data as a string
    device_data_str = "\n".join(
    
    f"{row.dimension_values[0].value} - {row.metric_values[0].value}"

    for row in response.rows
    )
    device_data_str = "Device Model - Active Users\n" + device_data_str

    return device_data_str

@tool
def fetechGoogleAnalyticsData(start_date: str, end_date: str):
    """
    Fetches Google Analytics data for the specified date range.
    Args:
        start_date (str): The start date of the report in the format "YYYY-MM-DD".
        end_date (str): The end date of the report in the format "YYYY-MM-DD".
    Returns:
        str: A formatted string containing the fetched data.
    """
    date_range = DateRange(start_date=start_date, end_date=end_date)

    request1 = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[date_range],
    )

    report1 = client.run_report(request1)

    output_string = ""
    for row in report1.rows:
        event_name = row.dimension_values[0].value
        event_count = row.metric_values[0].value
        if event_name == "scroll":
            output_string += f"{event_name}: {event_count} times\n"
        else:
            output_string += f"{event_name.capitalize()}: {event_count}\n"

    request2 = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="newVsReturning")
        ],
        metrics=[
            Metric(name="activeUsers")
        ],
        date_ranges=[
            DateRange(date_range)
        ]
    )

    report2 = client.run_report(request2)

    new_visitors = 0
    returning_visitors = 0

    for row in report2.rows:
        visitor_type = row.dimension_values[0].value
        active_users = int(row.metric_values[0].value)
        
        if visitor_type == "new":
            new_visitors += active_users
        elif visitor_type == "returning":
            returning_visitors += active_users

    total_visitors = new_visitors + returning_visitors

    if total_visitors > 0:
        new_visitor_percentage = (new_visitors / total_visitors) * 100
        returning_visitor_percentage = (returning_visitors / total_visitors) * 100
    else:
        new_visitor_percentage = 0
        returning_visitor_percentage = 0

    output_string += f"New Visitors: {new_visitors}\nReturning Visitors: {returning_visitors}\nNew Visitors: {new_visitor_percentage:.2f}%\nReturning Visitors: {returning_visitor_percentage:.2f}%"

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

    request3 = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        metrics=metrics,
        date_ranges=[date_range]
    )
    report3 = client.run_report(request3)

    # Iterate through the rows and append the metric names and values to the string
    for row in report3.rows:
        for i, metric_value in enumerate(row.metric_values):
            metric_name = report3.metric_headers[i].name
            output_string += f"{metric_name}: {metric_value.value}\n"

    prompt = """\nRespond with an analysis of the data, focusing on:\nUser Engagement: Compare Page_view and User_engagement. Analyze scroll events and engagementRate.\nVisitor Behavior: Break down New Visitors and Returning Visitors percentages. Compare First_visit and Session_start.\nSession Metrics: Analyze averageSessionDuration and sessionsPerUser. Discuss sessions and activeUsers.\nConversion Metrics: Analyze Form_start vs. Form_submit. Discuss View_search_results and Click events. Comment on addToCarts, checkouts, and purchaserRate.\nOverall Insights: Summarize key findings. Provide actionable recommendations for enhancing user engagement and conversion rates."""
    return output_string + prompt

@tool(args_schema=PopulateArgSchema)    
def fetchClarityData(input_arg: Optional[str] = "") -> str:
    """
    Retrieves a data from Clarity of the last 3 days.
    """

    # Fetch Clarity Data
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
    result = input_arg

    if response.status_code != 200:
        return "API limit reached. Please try again later."

    # Dead Clicks and Scroll Depth
    for metric in response.json():
        metric_name = metric.get('metricName', 'Unknown Metric')
        information = metric.get('information', [{}])

        if len(information) == 1:
            information = information[0]
            if metric_name == "DeadClickCount":
                result += f"**Dead Clicks:**\n"
                result += f"Total Dead Clicks: {information.get('subTotal', 'N/A')}\n"
                result += f"Sessions with Dead Clicks: {information.get('sessionsWithMetricPercentage', 'N/A')}%\n"
                result += f"Sessions without Dead Clicks: {information.get('sessionsWithoutMetricPercentage', 'N/A')}%\n"

            elif metric_name == "ScrollDepth":
                result += f"**Scroll Metrics:**\n"
                result += f"Average Scroll Depth: {information.get('averageScrollDepth', 'N/A')}%\n"

            elif metric_name == "Traffic":
                result += f"**Traffic and Session Metrics:**\n"
                result += f"Total Session Count: {information.get('totalSessionCount', 'N/A')}\n"
                result += f"Total Bot Session Count: {information.get('totalBotSessionCount', 'N/A')}\n"
                result += f"Distinct User Count: {information.get('distinctUserCount', 'N/A')}\n"
                result += f"Pages Per Session Percentage: {information.get('pagesPerSessionPercentage', 'N/A'):.2f}%\n"

            elif metric_name == "EngagementTime":
                result += f"**Engagement Time:**\n"
                result += f"Total Time: {information.get('totalTime', 'N/A')} seconds\n"
                result += f"Active Time: {information.get('activeTime', 'N/A')} seconds\n"

        elif len(information) > 1 and metric_name == "PageTitle":
            result += f"**Page Titles:**\n"
            for entry in information:
                if isinstance(entry, dict):
                    result += f"\"{entry.get('name', 'N/A')}\" - {entry.get('sessionsCount', 'N/A')} sessions\n"

    prompt = """\nRespond with:\nAnalyze Total Dead Clicks and Sessions with Dead Clicks. What does this indicate about user navigation?\nDiscuss Scroll Count and Average Scroll Depth. What do these metrics suggest about user engagement?\nCompare Total Session Count and Total Bot Session Count. What does this tell you about real user activity?\nAnalyze Distinct User Count and Pages Per Session Percentage. What does this indicate about user behavior?\nCompare Total Time and Active Time. What does this suggest about user interaction?\nBreak down session counts by page titles. Identify the most and least visited pages. What does this tell you about user interests - and navigation patterns?"""
    return result + prompt

@tool(args_schema=PopulateArgSchema)  
def fetchChatbotConversations(input_arg: Optional[str] = "") -> str:
    """
    Returns the customer chatbot conversations.
    """

    # Reference to the "HistoryMessages" collection
    collection_ref = db.collection("HistoryMessages")
    
    limit=100

    # Query all documents in the collection
    docs = collection_ref.stream()

    combined_string = input_arg

    combined_messages = []
    
    # Iterate through each document in the collection
    for doc in docs:
        doc_data = doc.to_dict()
        messages = doc_data.get('messages', [])

        combined_messages.append("\nUser history:")

        # Process each message (decode and extract content and type)
        for message in messages:
            if isinstance(message, bytes):
                try:
                    # Decode bytes to string and load JSON
                    message_str = message.decode('utf-8')
                    message_data = json.loads(message_str)
                    
                    # Extract content and sender type
                    content = message_data.get('content', '')
                    sender_type = message_data.get('type', '')  # 'human' or 'ai'
                    
                    # Append the formatted message
                    combined_messages.append(f"{sender_type}: {content}")
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    print(f"Error decoding message: {e}")
                    continue

        # Check if we've reached the limit of 100 messages
        if len(combined_messages) >= limit:
            combined_messages = combined_messages[:limit]  # Trim to the first 100 messages
            break

    # Join the messages into a single string with line breaks
    combined_string = "\n".join(combined_messages)
    
    print(f"Combined messages count: {len(combined_messages)}")
    
    prompt = """\nAnalyze the data to provide insights on:\nCustomer Intent: What are customers frequently searching for?  Identify the primary goals or needs expressed by customers in their queries (e.g., product information, support, order status).\nPopular Products: Which product categories, specific products, or product features are mentioned most often? Quantify the frequency of product mentions.\nSearch Effectiveness: Are there repeated queries or signs of failed searches? Look for patterns indicating that customers are struggling to find what they need (e.g., many similar queries, queries followed by "not found" or frustration).\nCustomer Issues: Identify frustration patterns or common problems customers are experiencing. Look for keywords indicating negative sentiment, repeated queries, or requests for escalation. Analyze the types of issues faced (e.g., technical problems, product questions, navigation difficulties).\nBot Performance: How effectively is the bot answering queries? Assess the bot's ability to correctly interpret customer intent and provide helpful responses. Identify gaps in the bot's knowledge or areas where responses could be improved. Provide specific examples of successful and unsuccessful interactions."""

    return combined_string + prompt

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

    post_insigths = input_arg

    # Print details for the most recent image
    if most_recent_image:
        post_insigths += "MOST RECENT IMAGE POST\n"
        # post_insigths += f"Link to post: {most_recent_image['permalink']}"
        post_insigths += f"Caption: {most_recent_image.get('caption', 'No caption')}\n"
        post_insigths += f"Posted at: {most_recent_image['timestamp']}\n"
        params['latest_media_id'] = most_recent_image['id']
        params['metric'] = 'follows,impressions,reach,saved,comments,shares,profile_visits,profile_activity'
        params['breakdown'] = 'action_type'
        image_insights = getMediaInsights(params)
        
        post_insigths += "\nIMAGE POST INSIGHTS\n"
        for insight in image_insights['json_data']['data']:
            post_insigths += f"{insight['title']}: {insight['values'][0]['value']}\n"

    # Print details for the most recent reel
    if most_recent_reel:
        post_insigths += "\nMOST RECENT REEL POST\n"
        # post_insigths += f"Link to post: {most_recent_reel['permalink']}\n"
        post_insigths += f"Caption: {most_recent_reel.get('caption', 'No caption')}\n"
        post_insigths += f"Posted at: {most_recent_reel['timestamp']}\n"
        params['latest_media_id'] = most_recent_reel['id']
        params['metric'] = 'saved,comments,shares,reach,plays,ig_reels_avg_watch_time'
        reel_insights = getMediaInsights(params)
        
        post_insigths += "\nREEL POST INSIGHTS\n"
        for insight in reel_insights['json_data']['data']:
            post_insigths += f"{insight['title']}: {insight['values'][0]['value']}\n"

    prompt = """ \nFor Post: \nAnalyze Impressions, Accounts reached, and Profile visits. What do these metrics tell you about post visibility and engagement? \n Discuss Comments, Shares, and Saved. What does this indicate about user interaction and content value?\nFor Reels: \nCompare Accounts reached, Initial plays, and Reels average watch time. What does this suggest about reel engagement and watch duration?\nAnalyze Comments, Shares, and Saved. How do these metrics compare with the image post?\nOverall Insights:\nSummarize key findings. Provide actionable recommendations to improve post and reel performance\nEnsure you pass the correct date range to get meaningful insights."""
    return post_insigths + prompt

@tool(args_schema=PopulateArgSchema)
def fetchMetaAdsData(input_ard: Optional[str] = "") -> str:
    """
    Retrieves maximum campaign insights.
    """
    campaign_id = "120213265876940368"  

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
    
    tools = [getDateRange, topNMostVisitedPages, fetchDeviceModelsData, fetechGoogleAnalyticsData, fetchClarityData, fetchChatbotConversations, fetchInstagramAccountData, fetchInstagramDemographicsData, fetchInstagramMediaData, fetchMetaAdsData]
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
        {"input": user_input,
        "date": datetime.now().strftime('%Y-%m-%d'),},
        config={"configurable": {"session_id": "admin1"}},
    )

    toc = time.perf_counter()
    print(response['output'])
    print(f"time_taken {toc - tic:0.4f} seconds")
