from google.oauth2 import service_account
from googleapiclient.discovery import build

# Path to your credentials.json file
SERVICE_ACCOUNT_FILE = 'credentials.json'

# Scopes required by Google Analytics
SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']

# Create credentials using the service account file
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES)

# Initialize the Analytics API client
analytics = build('analyticsdata', 'v1beta', credentials=credentials)

def fetch_data():
    response = analytics.properties().runReport(
        property='properties/451093576',
        body={
            "dimensions": [{"name": "city"}],
            "metrics": [{"name": "activeUsers"}],
            "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}]
        }
    ).execute()
    return response

# Fetch and print the data
data = fetch_data()
print(data)
