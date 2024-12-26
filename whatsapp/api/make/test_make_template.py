import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BUSINESS_ACCOUNT_ID = os.getenv("BUSINESS_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

blank_message = "\u3164"

def create_carousel():
    url = f"https://graph.facebook.com/{BUSINESS_ACCOUNT_ID}/message_templates"

    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type": "application/json"
    }

    data = {
        "name": "intro_test",
        "language": "en_US",
        "category": "marketing",
        "components": [
            {
             "type": "HEADER",
             "format": "TEXT",
             "text": "Hey there welcome to egenie",
            },
            {
            "type": "body",
            "text": "Thanks for subscribing to our newsletter we have signed you up for it successfully welcome to the egenie family",
            },
            
            {
            "type": "footer",
            "text": "Use the buttons below to manage your marketing subscriptions"
            }
        ]
    }


    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = create_carousel()
print(response.status_code)
print(response.json())

