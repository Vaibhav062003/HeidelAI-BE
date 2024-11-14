import os, time, requests, json, pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from googleapiclient.discovery import build
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)

load_dotenv()

PROPERTY_ID = os.getenv("PROPERTY_ID")
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'credentials.json'

client = BetaAnalyticsDataClient()

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

print(get_active_users('2024-08-21', '2024-08-21'))

# request3 = RunReportRequest(
#     property=f"properties/{PROPERTY_ID}",
#     dimensions=[
#         Dimension(name="eventName"),
#         Dimension(name="platform")
#     ],
#     metrics=[
#         Metric(name="eventCount")
#     ],
#     date_ranges=[DateRange(start_date="2024-01-01", end_date="today")],
#     )

# response3 = client.run_report(request3)


def ga4_result_to_df(response):
    result_dict = {}
    for dimensionHeader in response.dimension_headers:
        result_dict[dimensionHeader.name] = []
    for metricHeader in response.metric_headers:
        result_dict[metricHeader.name] = []
    for rowIdx, row in enumerate(response.rows):
        for i, dimension_value in enumerate(row.dimension_values):
            dimension_name = response.dimension_headers[i].name
            result_dict[dimension_name].append(dimension_value.value)
        for i, metric_value in enumerate(row.metric_values):
            metric_name = response.metric_headers[i].name
            result_dict[metric_name].append(metric_value.value)
    return pd.DataFrame(result_dict)

# df3 = ga4_result_to_df(response3)
# print(df3)

def load_analytics_data():
    """
    Fetches various analytics data from Google Analytics 4 and combines it into a single string
    that can be used by an LLM for responding to queries.

    Returns:
        str: A string containing the combined analytics data.
    """
    client = BetaAnalyticsDataClient()
    today = datetime.today().strftime('%Y-%m-%d')

    # Conversions
    conversions_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="eventName")],
        metrics=[Metric(name="conversions")],
        date_ranges=[DateRange(start_date="2024-01-01", end_date=today)]
    )
    conversions_response = client.run_report(conversions_request)

    #Active users for the current day
    today = datetime.today().strftime('%Y-%m-%d')

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        metrics=[Metric(name="activeUsers")],
        date_ranges=[DateRange(start_date=today, end_date=today)]
    )

    report = client.run_report(request)

    for row in report.rows:
        todays_active_users = row.metric_values[0].value

    #User's devices
    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name="deviceModel")],
        metrics=[Metric(name="activeUsers")], 
        date_ranges=[DateRange(start_date="2024-01-01", end_date="today")]
    )

    response = client.run_report(request)

    device_data = {row.dimension_values[0].value: int(row.metric_values[0].value) for row in response.rows}
    device_data = dict(list(device_data.items())[:8])

    # Average Session Duration
    session_duration_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        metrics=[Metric(name="averageSessionDuration")],
        date_ranges=[DateRange(start_date="2024-01-01", end_date=today)]
    )
    session_duration_response = client.run_report(session_duration_request)

    # Pageviews
    pageviews_request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        metrics=[Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date="2024-01-01", end_date=today)]
    )
    pageviews_response = client.run_report(pageviews_request)

    #Top 5 most visited pages
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
            if i > 5 - 1:
                break

    # Process and combine data
    data_string = "Analytics Data:\n"

    data_string += "\nTotal active users today: "
    data_string += f"{todays_active_users}\n"
    
    data_string += "\nUser's Device information: "
    data_string += f"{device_data}\n"
    
    # Average Session Duration
    data_string += "\nAverage Session Duration: "
    data_string += f"{session_duration_response.rows[0].metric_values[0].value} seconds\n"

    # Pageviews
    data_string += "\nPageviews since the start of the year: "
    data_string += f"{pageviews_response.rows[0].metric_values[0].value}\n"
    
    # Top 5 visited pages
    data_string += "\nTop 5 most visited pages: "
    data_string += f"{pages}\n"

    return data_string