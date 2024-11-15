import requests, os, json
from dotenv import load_dotenv

load_dotenv()

instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
access_token = os.getenv("ACCESS_TOKEN")
secret_token = os.getenv("SECRET_TOKEN")

# The URL for the API request
url = f'https://graph.facebook.com/{instagram_account_id}/insights'

# Define the metrics you want to retrieve

#follower_count

#reached_audience_demographics
params = {
    'metric': 'impressions, reach, profile_views, total_interactions, profile_links_taps, website_clicks, accounts_engaged, saves, shares',
    'period': 'day',  
    'access_token': access_token,
    # 'appsecret_proof': secret_token,
    'metric_type': 'total_value'
}

# Make the request to fetch insights
response = requests.get(url, params=params)

if response.status_code == 200:
    # Parse the response JSON
    data = response.json()
    
    # Create a formatted string to print the metrics
    metrics_str = "Instagram Metrics:\n"
    for item in data['data']:
        metrics_str += f"{item['title']} : {item['total_value']['value']}\n"
        # metrics_str += f"{item['title']} ({item['name']}): {item['total_value']['value']}\n"
        # metrics_str += f"  Description: {item['description']}\n"
    
    # Print the formatted string
    print(metrics_str)

else:
    # If the request was not successful, print the error
    print(f"Error: {response.status_code}")
    print(response.json())

# def get_instagram_insights() -> str:
#     """
#     Fetches Egenie's instagram account insights.
#     """
#     try:
#         url = f'https://graph.facebook.com/{instagram_account_id}/insights'
#         params = {
#             'metric': 'impressions,reach,profile_views,total_interactions,profile_links_taps,website_clicks,accounts_engaged,saves,shares',
#             'period': 'day',  
#             'metric_type': 'total_value',
#             'access_token': access_token
#         }

#         print("Calling Instagram API...")  # Debug print
#         response = requests.get(url, params=params)
#         print(f"Response status: {response.status_code}")  # Debug print

#         if response.status_code == 200:
#             data = response.json()
#             metrics_str = "Instagram Metrics:\n"
#             for item in data['data']:
#                 metrics_str += f"{item['title']}: {item['total_value']['value']}\n"
#             return metrics_str
#         else:
#             error_msg = f"Error: {response.status_code}"
#             print(error_msg)  # Debug print
#             return error_msg

#     except Exception as e:
#         error_msg = f"Error fetching Instagram insights: {str(e)}"
#         print(error_msg)  # Debug print
#         return error_msg

    
# get_instagram_insights()

"""
impressions, 
reach, 
follower_count, 
email_contacts,  
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
    'metric': 'follower_count, impressions, reach, profile_views, total_interactions, profile_links_taps, phone_call_clicks, text_message_clicks, website_clicks, accounts_engaged, saved, shares',
    'period': 'day',  
    'access_token': access_token,
    'metric_type': 'total_value'
}

without metric_type = 'total_value'
params = {
    'metric': 'email_contacts, follower_count, phone_call_clicks, text_message_clicks, website_clicks, accounts_engaged, saved, shares',
    'period': 'day',  
    'access_token': access_token,
}

2.
params = {
    'metric': 'impressions, reach',
    'period': 'week',  
    'access_token': access_token,
    'metric_type': 'total_value'
}

3.
params = {
    'metric': 'impressions, reach',
    'period': 'week',  
    'access_token': access_token,
    'metric_type': 'total_value'
}

4.
params = {
    'metric': 'online_followers',
    'period': 'lifetime',  
    'access_token': access_token,
    'metric_type': 'total_value'
}

"""
# Instagram Metrics:
# Impressions (impressions): 24443
# Accounts reached (reach): 18581
# Profile visits (profile_views): 454
# Content interactions (total_interactions): 156   
# Profile links taps (profile_links_taps): 0
# Website link taps (website_clicks): 132
# Accounts engaged (accounts_engaged): 168
# Saves (saves): 9
#   Description: The number of saves of your content minus the number of unsaves.
# Shares (shares): 10
#   Description: The number of shares of your posts, stories, reels, videos and live videos.