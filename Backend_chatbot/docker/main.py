import random, string, secrets, re, json, os, httpx, jwt, time, requests
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
# from sqlalchemy import create_engine, text
# from langchain_community.utilities import SQLDatabase

# Chatbot Libs
from langchain import hub
from dotenv import load_dotenv
from langchain_core.tools import tool
from datetime import datetime, timedelta
from langchain_google_genai import ChatGoogleGenerativeAI
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain_community.chat_message_histories import ChatMessageHistory
from defines import getCreds, getMediaInsights, getUserMedia, getMostRecentMediaByType, demographics_insights
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)

from google.cloud import firestore
from google.oauth2 import service_account

# Load the environment variables
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PROPERTY_ID = os.getenv("PROPERTY_ID")
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY")
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'
CLARITY_TOKEN = os.getenv("CLARITY_TOKEN")

os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGCHAIN_API_KEY")
os.environ["LANGCHAIN_PROJECT"] =os.getenv("LANGCHAIN_PROJECT")

instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
access_token = os.getenv("ACCESS_TOKEN")

# Initialize the GA4 client
client = BetaAnalyticsDataClient()

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(os.getenv("SERVICE_ACCOUNT_FILE"))

# Initialize Firestore using the credentials
db = firestore.Client(credentials=credentials)


def initialize_llm() -> ChatGoogleGenerativeAI:
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    return llm

# Tools
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

    return output_string

@tool    
def fetchClarityData(dummy: str):
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
    result = ""

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

    return result

@tool
def fetchChatbotConversations(dummy: str) -> str:
    """
    Returns the customer chatbot conversations.
    """

    # Reference to the "HistoryMessages" collection
    collection_ref = db.collection("HistoryMessages")
    
    limit=100

    # Query all documents in the collection
    docs = collection_ref.stream()

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
    
    return combined_string

@tool
def fetchInstagramAccountData(dummy: str) -> str:
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
        'access_token': access_token,
        'metric_type': 'total_value',
    }

    response = requests.get(url, params=base_params)

    if response.status_code == 200:

        data = response.json()
        
        # Create a formatted string to print the metrics
        metrics_str = "Instagram Metrics:\n"
        for item in data['data']:
            metrics_str += f"{item['title']} : {item['total_value']['value']}\n"

        return metrics_str
    else:
        print(f"Error: {response.status_code}")
        print(response.json())

@tool
def fetchDemographicsData(dummy: str) -> str:
    """
    Fetches Egenie's Instagram account demographics insights. 
    Returns a string containing formatted metrics. 
    """
    data = demographics_insights()
    return data

@tool
def fetchInstagramMediaData(dummy: str) -> str:
    """
    Fetches insights for the most recent Instagram image and reel posts.
    """
    url = f'https://graph.facebook.com/{instagram_account_id}/insights'

    # Fetch user media
    params = getCreds()  # Fetch credentials
    response = getUserMedia(params)  # Get all media
    media_data = response['json_data']['data']  # Extract media list

    # Separate media by type
    most_recent_image = getMostRecentMediaByType(media_data, 'IMAGE')
    most_recent_reel = getMostRecentMediaByType(media_data, 'VIDEO')

    post_insigths = ""

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

    return post_insigths

def initialize_langchain_agent(llm: ChatGoogleGenerativeAI , id: str) -> RunnableWithMessageHistory:    

    memory = ChatMessageHistory(session_id=id)
    
    tools = [getDateRange, topNMostVisitedPages, fetchDeviceModelsData, fetechGoogleAnalyticsData, fetchClarityData, fetchChatbotConversations, fetchInstagramAccountData, fetchDemographicsData, fetchInstagramMediaData]
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

def get_agent(id: str) -> RunnableWithMessageHistory:
    llm = initialize_llm()
    return initialize_langchain_agent(llm, id)


###################################################################################################

# def initialize_database():
#     db_uri = "cockroachdb://egenie:mTt-zNDpB35pjYZ7lEotLw@chatbot-5648.7s5.aws-ap-south-1.cockroachlabs.cloud:26257/defaultdb"

#     # Create the SQLAlchemy engine
#     engine = create_engine(db_uri)

#     drop_table = """
#     DROP TABLE IF EXISTS testing;
#     """
#     drop_table2 = """
#     DROP TABLE IF EXISTS users;
#     """
    
#     # Create queries table if it doesn't exist
#     create_table_query = """
#     CREATE TABLE IF NOT EXISTS chat_history (
#         id SERIAL PRIMARY KEY,
#         user_id VARCHAR(50),
#         username TEXT,
#         userEmail TEXT,
#         query TEXT,
#         response TEXT,
#         time_taken TEXT,
#         timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#     );
#     """

#     # Create users table if it doesn't exist
#     create_table_users = """
#     CREATE TABLE IF NOT EXISTS users (
#         id SERIAL PRIMARY KEY,
#         userName TEXT,
#         userEmail TEXT,
#         timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#     );
#     """

#     with engine.connect() as connection:
#         # connection.execute(text(drop_table))
#         # connection.execute(text(drop_table2))
#         connection.execute(text(create_table_query))
#         connection.execute(text(create_table_users))
    
#     return engine

# engine = initialize_database()

# def generate_user_id():
#     return ''.join(random.choices(string.ascii_letters + string.digits, k=10))

           
"""Code modified from below"""
gkey = "86c9abe0-f0a8-4230-9fce-ba54a70c538f"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZGVudGlmaWVyIjoyNjk1fQ.kSE16TbPskzEm1mX_RrIlXqWkF97pI9JzkOjVMsXl-Y"

# def store_query_in_db(user_id, username, userEmail, query, response, time_taken):
#     insert_query = text("""
#     INSERT INTO chat_history (user_id, username, userEmail, query, response, time_taken)
#     VALUES (:user_id, :username, :userEmail, :query, :response, :time_taken)
#     """)
    
#     with engine.connect() as connection:
#         connection.execute(insert_query, {'user_id': user_id, 'username': username, 'userEmail': userEmail, 'query': query, 'response': response, 'time_taken': time_taken})

app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://egeniestores.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class EmailInput(BaseModel):
    email: str

class OtpInput(BaseModel):
    otp_id: str
    otp: str
    name : str
    email: str

SECRET_KEY = secrets.token_hex(32) 

def create_jwt_token(userName, userEmail, otp_id, otp):
    token_payload = {
        'userName': userName,
        'userEmail': userEmail,  # Include the username in the token payload
        'otp_id': otp_id,
        'otp' : otp
    }
    token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")
    return token

def verify_jwt_token(authorization: str = Header(...)) -> str:
    try:
        token = authorization.split(" ")[1]  # Get the token from the "Bearer" prefix
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/protected-route")
async def protected_route(token: str = Depends(verify_jwt_token)):
    return {"success": True, "message": f"Access granted for {token}"}

# def store_user_in_db(userName, userEmail):
#     insert_query = text("""
#     INSERT INTO users (userName, userEmail)
#     VALUES (:userName, :userEmail)
#     """)
    
#     with engine.connect() as connection:
#         connection.execute(insert_query, {'userName': userName, 'userEmail': userEmail})

@app.post("/send-mail-otp")
async def send_otp(email_input: EmailInput):
    url = 'https://api.fazpass.com/v1/otp/request'
    payload = {'email': email_input.email, 'gateway_key': gkey}
    headers = {
        'Authorization': f'Bearer {BEARER_TOKEN}'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)

@app.post("/verify-mail-otp")
async def send_otp(otp_input: OtpInput):
    userName = otp_input.name
    userEmail = otp_input.email

    # store_user_in_db(userName, userEmail)/

    url = 'https://api.fazpass.com/v1/otp/verify'
    payload = {'otp_id': otp_input.otp_id, 'otp' : otp_input.otp}
    headers = {
        'Authorization': f'Bearer {BEARER_TOKEN}'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            # Create a JWT token with the payload and secret key
            # token_payload = {'otp_id': otp_input.otp_id, 'otp': otp_input.otp}
            # token = jwt.encode(token_payload, SECRET_KEY, algorithm="HS256")

            token = create_jwt_token(userName, userEmail, otp_input.otp_id, otp_input.otp)

            # Get the response data from the OTP verification API
            response_data = response.json()

            # Include the JWT token in the response
            response_data['token'] = token

            return response_data
        else:
            raise HTTPException(status_code=response.status_code, detail=response.text)

def extract_token_from_headers(headers):
    # Extract the token from the 'Authorization' header
    authorization = headers.get('Authorization', '')
    if authorization.startswith('Bearer '):
        return authorization.split(' ')[1]
    return None

def decode_jwt_token(token):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    websocket_id = str(id(websocket))
    agent = get_agent(websocket_id)

    # user_id = generate_user_id()
    try:
        while True:
            data = await websocket.receive_text()
            print("Input: ", data)
            tic = time.perf_counter()

            response = agent.invoke(
                {"input": data},
                config={"configurable": {"session_id": websocket_id}},
            )
            
            response = response['output']

            print("Response: ", response)
            toc = time.perf_counter()
            print(f"Response took {toc - tic:0.4f} seconds")

            # time_taken = round(toc - tic, 4)

            # Store query and response in the database
            # store_query_in_db(user_id, username, userEmail, data, response, time_taken)
            
            await websocket.send_text(response)

    except WebSocketDisconnect:
        pass


@app.get("/")
async def get():
    return JSONResponse(content={"message": "WebSocket endpoint is ready. Connect to /ws for WebSocket communication."})