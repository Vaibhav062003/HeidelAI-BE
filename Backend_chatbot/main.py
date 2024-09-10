import os, time
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from langchain import hub
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain.agents import AgentExecutor, create_structured_chat_agent
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,     
    RunReportRequest,
)


load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PROPERTY_ID = os.getenv("PROPERTY_ID")
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'

client = BetaAnalyticsDataClient()

def initialize_llm():
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    return llm


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
        pandas.DataFrame: A DataFrame containing the report data with dimensions and metrics.
    Raises:
        None
    Example:
        start_date = "2022-01-01"
        end_date = "2022-01-31"
        report = get_comperhensive_report(start_date, end_date)
    """
    
    request3 = RunReportRequest(
    property=f"properties/{PROPERTY_ID}",
    dimensions=[
        Dimension(name="audienceId"),
        Dimension(name="eventName"),
    ],
    metrics=[
        Metric(name="eventCount")
    ],
    date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
    )

    response3 = client.run_report(request3)

    result_dict = {}
    for dimensionHeader in response3.dimension_headers:
        result_dict[dimensionHeader.name] = []
    for metricHeader in response3.metric_headers:
        result_dict[metricHeader.name] = []
    for rowIdx, row in enumerate(response3.rows):
        for i, dimension_value in enumerate(row.dimension_values):
            dimension_name = response3.dimension_headers[i].name
            result_dict[dimension_name].append(dimension_value.value)
        for i, metric_value in enumerate(row.metric_values):
            metric_name = response3.metric_headers[i].name
            result_dict[metric_name].append(metric_value.value)
    return pd.DataFrame(result_dict)

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

def initialize_langchain_agent(llm):    

    tools = [top_n_most_visited_pages, get_active_users, get_device_model_data, get_date_range, get_total_page_views, get_comperhensive_report, get_new_vs_returning_visitors]
    prompt = hub.pull("hwchase17/structured-chat-agent")
    # print(prompt)
    custom_instructions = "Always use the 'get_date_range' tool to get the start and end dates for other tools. For example, you can use 'get_date_range(\"last week\")' to get the date range for the previous week."
    modified_prompt = prompt + "\n\n" + custom_instructions
    agent = create_structured_chat_agent(llm, tools, prompt)
    agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)
    return agent_executor

def get_agent():
    llm = initialize_llm()
    return initialize_langchain_agent(llm)


agent_executor = get_agent()

chat_history = []

def add_to_chat_history(user_message, ai_response):
    chat_history.append(HumanMessage(content=user_message))
    chat_history.append(AIMessage(content=ai_response))

while True:
    user_input = input("User: ")
    tic = time.perf_counter()
    response = agent_executor.invoke(
        {
            "input": user_input,
            "chat_history": chat_history,
        }
    )
    toc = time.perf_counter()
    print(response['output'])
    add_to_chat_history(user_input, response["output"])
    print(f"time_taken {toc - tic:0.4f} seconds")


