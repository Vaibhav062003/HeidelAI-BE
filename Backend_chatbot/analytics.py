from google.oauth2 import service_account
from google.protobuf.json_format import MessageToDict
import pandas as pd
import os
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)
from google.analytics.data_v1beta import BetaAnalyticsDataClient

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = 'credentials.json'
property_id = '451093576'

client = BetaAnalyticsDataClient()

request = RunReportRequest(
    property=f"properties/{property_id}",
    dimensions=[
        Dimension(name="newVsReturning")
    ],
    metrics=[
        Metric(name="activeUsers")
    ],
    date_ranges=[
        DateRange(start_date="2024-08-1", end_date="2024-08-31")
    ]
)

response = client.run_report(request)

new_visitors = 0
returning_visitors = 0

# Process the response and gather data
for row in response.rows:
    visitor_type = row.dimension_values[0].value
    active_users = int(row.metric_values[0].value)
    
    if visitor_type == "new":
        new_visitors += active_users
    elif visitor_type == "returning":
        returning_visitors += active_users

# Calculate total visitors
total_visitors = new_visitors + returning_visitors

# Calculate percentages
if total_visitors > 0:
    new_visitor_percentage = (new_visitors / total_visitors) * 100
    returning_visitor_percentage = (returning_visitors / total_visitors) * 100
else:
    new_visitor_percentage = 0
    returning_visitor_percentage = 0

result = f"New Visitors: {new_visitors}\nReturning Visitors: {returning_visitors}\nNew Visitors: {new_visitor_percentage:.2f}%\nReturning Visitors: {returning_visitor_percentage:.2f}%"
print(result)
