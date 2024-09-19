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
        "name": "product_card_carousel_v1",
        "language": "en_US",
        "category": "marketing",
        "components": [
            {
            "type": "body",
            "text": "Here are some {{1}} you might be interested in. {{2}}.",
            "example": {
                "body_text": [
                [
                    "laptops",
                    "Do you have any specific preferences in mind, like a particular brand, screen size, or budget?"
                ]
                ]
            }
            },
            {
            "type": "carousel",
            "cards": [
                {
                "components": [
                    {
                    "type": "header",
                    "format": "product"
                    },
                    {
                    "type": "buttons",
                    "buttons": [
                        {
                        "type": "spm",
                        "text": "View"
                        },
                        {
                        "type": "quick_reply",
                        "text": "Know more"
                        }
                    ]
                    }
                ]
                },
                {
                "components": [
                    {
                    "type": "header",
                    "format": "product"
                    },
                    {
                    "type": "buttons",
                    "buttons": [
                        {
                        "type": "spm",
                        "text": "View"
                        }
                    ]
                    }
                ]
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

