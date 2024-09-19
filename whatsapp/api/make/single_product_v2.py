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
        "name": "single_product_v2",
        "language": "en_US",
        "category": "marketing",
        "components": [
            {
            "type": "header",
            "format": "product"
            },
            {
            "type": "body",
            "text": "The {{1}} \n \n 🎉 *Key Highlights:* \n - EMI Available! ✅ \n - Get GST invoice and save up to 28% on business purchases \n - Free Shipping all over India 🚚",
            "example": {
                "body_text": [
                [
                    "Apple MacBook Pro (14-Inch, Apple M3 Pro Chip, 11-Core CPU, 14-Core GPU, 18GB Unified Memory, 512GB SSD, macOS, Space Black)"
                ]
                ]
            }
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


    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = create_carousel()
print(response.status_code)
print(response.json())

