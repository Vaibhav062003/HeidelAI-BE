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
# v3 - pending (only button last word parameter)
# v4 - approved (no params)
# v7 - approved (only body text one word parameter)
# v8 - approved (body text + end of url as parameters)
# v26 - approved (body text + product name + price, no params for link)
# v33 - approved (body text + product name + price, no params for buttons)

    data = {
        "name": "seasonal_promotion",
        "language": "en_US",
        "category": "MARKETING",
        "components": [
            {
            "type": "HEADER",
            "format": "TEXT",
            "text": "Our {{1}} is on!",
            "example": {
                "header_text": [
                "Summer Sale"
                ]
            }
            },
            {
            "type": "BODY",
            "text": "Shop now through {{1}} and use code {{2}} to get {{3}} off of all merchandise.",
            "example": {
                "body_text": [
                [
                    "the end of August","25OFF","25%"
                ]
                ]
            }
            },
            {
            "type": "FOOTER",
            "text": "Use the buttons below to manage your marketing subscriptions"
            },
            {
            "type":"BUTTONS",
            "buttons": [
                {
                "type": "QUICK_REPLY",
                "text": "Unsubscribe from Promos"
                },
                {
                "type":"QUICK_REPLY",
                "text": "Unsubscribe from All"
                }
            ]
            }
        ]
    }


    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = create_carousel()
print(response.status_code)
print(response.json())

