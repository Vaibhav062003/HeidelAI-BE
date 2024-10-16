import requests, os, json
from dotenv import load_dotenv

load_dotenv()

instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
access_token = os.getenv("ACCESS_TOKEN")

# The URL for the API request
url = f'https://graph.facebook.com/{instagram_account_id}/insights'

# Define the metrics you want to retrieve
# 'impressions', 'reach', 'engagement', and 'follower_count' are common metrics for eCommerce

#follower_count
#phone_call_clicks

#reached_audience_demographics

params = {
    'metric': 'reached_audience_demographics',
    'period': 'lifetime',  
    'access_token': access_token,
    'metric_type': 'total_value'
}

# Make the request to fetch insights
response = requests.get(url, params=params)

# Check if the request was successful
if response.status_code == 200:
    # Parse the response JSON
    data = response.json()
    print("Instagram Metrics: ", data)
    
    with open('instagram_metrics.json', 'w') as json_file:
        json.dump(data, json_file, indent=4)
    print("Data saved to instagram_metrics.json")

else:
    # If the request was not successful, print the error
    print(f"Error: {response.status_code}")
    print(response.json())

"""
impressions, 
reach, 
follower_count, 
email_contacts, 
phone_call_clicks, 
text_message_clicks, 
get_directions_clicks, 
website_clicks, 
profile_views, 
online_followers, 
accounts_engaged, 
total_interactions, 
likes, 
comments, 
shares, 
saves, 
replies, 
engaged_audience_demographics, 
reached_audience_demographics, 
follower_demographics, 
follows_and_unfollows, 
profile_links_taps, 
views, 
threads_likes, 
threads_replies, 
reposts, 
quotes, 
threads_followers, 
threads_follower_demographics

1.
params = {
    'metric': 'impressions,reach,profile_views,website_clicks, website_clicks, total_interactions, profile_views, profile_links_taps',
    'period': 'day',  # or 'week', depending on your needs
    'access_token': ACCESS_TOKEN,
    'metric_type': 'total_value'
}

2.

"""
