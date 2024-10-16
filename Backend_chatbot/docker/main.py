import random, string, secrets, re, json, os, httpx, jwt, time, requests
from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
# from sqlalchemy import create_engine, text
# from langchain_community.utilities import SQLDatabase

# Chatbot Libs
from datetime import datetime, timedelta
from dotenv import load_dotenv
from langchain import hub
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.chat_message_histories import ChatMessageHistory
# from langchain_core.messages import AIMessage, HumanMessage
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

import json
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
def get_date_range(date_range: str):
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
def top_n_most_visited_pages(n : int) -> list:
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
def get_active_users(start_date: str, end_date: str) -> int:
    """
    Fetches the number of active users within a specified date range.

    Args:
        start_date (str): The start date for the query in 'YYYY-MM-DD' format.
        end_date (str): The end date for the query in 'YYYY-MM-DD' format.

    Returns:
        int: The number of active users for the specified date range.
             Returns 0 if no data is found or if an error occurs.
    """
    try:
        client = BetaAnalyticsDataClient()
        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            metrics=[Metric(name="activeUsers")],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
        )
        report = client.run_report(request)

        if report.rows and report.rows[0].metric_values:
            return int(report.rows[0].metric_values[0].value)
        else:
            return 0

    except Exception as e:
        print(f"An error occurred: {e}")
        return 0

@tool
def get_device_model_data(n : int = 10):
    """
    Fetches the data for device models used by users for the current day from Google Analytics 4.

    args:
        int: The number of devices to return, default = 10
    Returns:
        dict: A dictionary where keys are device models and values are the number of active users for each model.
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

    device_data = {row.dimension_values[0].value: int(row.metric_values[0].value) for row in response.rows}

    return device_data

@tool
def get_total_page_views(start_date: str, end_date: str):
    """
    Fetches the total number of page views from Google Analytics 4 for a specified date range.

    Args:
        start_date (str): The start date for the query in 'YYYY-MM-DD' format.
        end_date (str): The end date for the query in 'YYYY-MM-DD' format.

    Returns:
        int: The total number of page views recorded between the start date and end date.

    Example:
        get_total_page_views("2024-08-01", "2024-08-31")
        1500
    """
    try:
        pageviews_request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",
            metrics=[Metric(name="screenPageViews")],
            date_ranges=[DateRange(start_date=start_date, end_date=end_date)]
        )
        pageviews_response = client.run_report(pageviews_request)

        # Check if the response contains rows and metric values
        if pageviews_response.rows and pageviews_response.rows[0].metric_values:
            return int(pageviews_response.rows[0].metric_values[0].value)
        else:
            return 0

    except Exception as e:
        # Log the exception or handle it as needed
        print(f"An error occurred: {e}")
        return 0
    
@tool
def get_comperhensive_report(start_date: str, end_date: str):
    """
    Retrieves a comprehensive report based on the specified start and end dates.
    Args:
        start_date (str): The start date of the report in the format "YYYY-MM-DD".
        end_date (str): The end date of the report in the format "YYYY-MM-DD".
    Returns:
        str: A formatted string containing the report data.
    """
    
    request3 = RunReportRequest(
    property=f"properties/{PROPERTY_ID}",
    dimensions=[
        Dimension(name="eventName"),
    ],
    metrics=[
        Metric(name="eventCount")
    ],
    date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
    )

    response3 = client.run_report(request3)

    result = ""

    for row in response3.rows:
        event_name = row.dimension_values[0].value
        event_count = row.metric_values[0].value
        if event_name == "scroll":
            result += f"{event_name}: {event_count} times\n"  # Add "times" for scroll event
        else:
            result += f"{event_name}: {event_count}\n"

    params = {"numOfDays": "3", "dimension1": "PopularPages"}
    headers = {
    "Authorization": f"Bearer {CLARITY_TOKEN}",
    "Content-type": "application/json"
    }
    response = requests.get("https://www.clarity.ms/export-data/api/v1/project-live-insights",
    params=params,
    headers=headers,
    )

    data = response.json() 

    for metric in data:
        metric_name = metric.get('metricName', 'Unknown Metric')
        information = metric.get('information', [{}])

        if len(information) == 1:
            information = information[0]
            if metric_name == "DeadClickCount":
                result += f"**Dead Click Count:**\n"
                result += f"Sessions with Dead Clicks Percentage: {information.get('sessionsWithMetricPercentage', 'N/A')}%\n"
                result += f"Sessions without Dead Clicks Percentage: {information.get('sessionsWithoutMetricPercentage', 'N/A')}%\n"
                result += f"Total Dead Clicks: {information.get('subTotal', 'N/A')}\n\n"

            elif metric_name == "ScrollDepth":
                result += f"**Average Scroll Depth:**\n"
                result += f"Average Scroll Depth: {information.get('averageScrollDepth', 'N/A')}%\n\n"

            elif metric_name == "Traffic":
                result += f"**Traffic Metrics:**\n"
                result += f"Total Session Count: {information.get('totalSessionCount', 'N/A')}\n"
                result += f"Total Bot Session Count: {information.get('totalBotSessionCount', 'N/A')}\n"
                result += f"Distinct User Count: {information.get('distinctUserCount', 'N/A')}\n"
                result += f"Pages Per Session Percentage: {information.get('pagesPerSessionPercentage', 'N/A'):.2f}%\n\n"

            elif metric_name == "EngagementTime":
                result += f"**Engagement Time:**\n"
                result += f"Total Time: {information.get('totalTime', 'N/A')} seconds\n"
                result += f"Active Time: {information.get('activeTime', 'N/A')} seconds\n\n"

        elif len(information) > 1:

            if metric_name == "PageTitle":
                result += f"**Page Titles:**\n"
                for entry in information:
                    if isinstance(entry, dict):
                        result += f"Page Title: {entry.get('name', 'N/A')} - Sessions Count: {entry.get('sessionsCount', 'N/A')}\n"
                    else:
                        result += f"Unexpected data format in PageTitle.\n"
                result += "\n"

            elif metric_name == "PopularPages":
                result += f"**Popular Pages:**\n"
                for entry in information:
                    if isinstance(entry, dict):
                        result += f"URL: {entry.get('url', 'N/A')} - Visits Count: {entry.get('visitsCount', 'N/A')}\n"
                    else:
                        result += f"Unexpected data format in PopularPages.\n"
                result += "\n"

    return result

@tool
def get_new_vs_returning_visitors(start_date: str, end_date: str):
    """
    This tool fetches data about new and returning visitors from Google Analytics for the 
    specified date range. It calculates and returns the total number of new and returning visitors, 
    along with the percentage of each category relative to the total number of visitors.
    Args:
        start_date (str): The start date of the report in the format "YYYY-MM-DD".
        end_date (str): The end date of the report in the format "YYYY-MM-DD".
    Returns:
        str: A formatted string showing the count and percentage of new and returning visitors.
    """
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[
            Dimension(name="newVsReturning")
        ],
        metrics=[
            Metric(name="activeUsers")
        ],
        date_ranges=[
            DateRange(start_date=start_date, end_date=end_date)
        ]
    )

    response = client.run_report(request)

    new_visitors = 0
    returning_visitors = 0

    for row in response.rows:
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

    result = f"New Visitors: {new_visitors}\nReturning Visitors: {returning_visitors}\nNew Visitors: {new_visitor_percentage:.2f}%\nReturning Visitors: {returning_visitor_percentage:.2f}%"
    return result

@tool
def get_customer_chatbot_conversations(dummy: str) -> str:
    """
    Gets the customer chatbot conversations from the Firestore database.
    Analyse the conversations and return a detailed analysis on what can be improved.
    args:
        dummy (str): A dummy argument to trigger the tool.
    returns:
        str: User's conversations with customer/sales chatbot.
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

def initialize_langchain_agent(llm: ChatGoogleGenerativeAI , id: str) -> RunnableWithMessageHistory:    

    memory = ChatMessageHistory(session_id=id)
    
    tools = [top_n_most_visited_pages, get_active_users, get_device_model_data, get_date_range, get_total_page_views, get_comperhensive_report, get_new_vs_returning_visitors, get_customer_chatbot_conversations]
    prompt = hub.pull("admin-structured-chat-agent-prompt")
    agent = create_structured_chat_agent(llm, tools, prompt)

    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent, 
        tools=tools, 
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