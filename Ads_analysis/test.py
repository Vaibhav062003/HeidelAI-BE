from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from dotenv import load_dotenv
import os
load_dotenv()
instagram_account_id = os.getenv("INSTAGRAM_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
APP_ID = os.getenv('APP_ID')
APP_SECRET = os.getenv('SECRET_TOKEN')
AD_ACCOUNT_ID = os.getenv('AD_ACCOUNT_ID')
FacebookAdsApi.init(APP_ID, APP_SECRET, ACCESS_TOKEN)

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

def get_max_campaign_insights_as_natural_language(campaign_id):
     try:
        my_account = AdAccount(AD_ACCOUNT_ID)

        campaign_insights = my_account.get_insights(
            fields=[
                'campaign_name',  #Campaign Name for identification
                'clicks', # Number of clicks on the ad
                'cpc',    # Cost per click
                'ctr',    # Click-through rate (important for ad effectiveness)
                'spend',   # Total amount spent on the campaign
                'reach', # Number of people reached
                'impressions',  # Number of times the ad was displayed
                'frequency',      # Average number of times each person saw the ad
                'conversions', # Number of conversions (if applicable to your objective)
                'cost_per_conversion', # Cost per conversion (if applicable)
                'conversion_values', #value of conversions
                'website_ctr', #website click through rate, if you have website conversions enabled
                'video_play_actions', #number of video plays, if using video ads
                'video_avg_time_watched_actions',  # Average video watch time (if using video ads)
                'objective', #the objective of the campaign
                'website_purchase_roas', # Return on ad spend for website purchases
                'cost_per_unique_click', # Cost per unique click (de-duplicates clicks)
                'actions', # Include actions (e.g., link clicks, video views) for more detailed analysis
                'action_values', #monetary value of actions
                'outbound_clicks', #all clicks leading out of facebook
                'cost_per_unique_outbound_click', #cost per click leading out of facebook
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

        natural_language_string = format_insights_as_natural_language(campaign_insights_data)
        return natural_language_string

     except Exception as e:
        print(f"Error: {e}")
        return f"Error retrieving insights: {e}"

# Example usage:
natural_language_output = get_max_campaign_insights_as_natural_language("120217765393330368")
print(natural_language_output)
