import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
sahil = "+919511907785"
vipul = "+918408062973"
rounak = "+917972510629"

def send_carousel_message():
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": rounak,
        "type": "template",
        "template": {
            "name": "single_product_v2",
            "language": {
            "code": "en_US"
            },
            "components": [
            {
                "type": "header",
                "parameters": [
                {
                    "type": "product",
                    "product": {
                    "product_retailer_id": "MRX33HN/A_201",
                    "catalog_id": "759103595212051"
                    }
                }
                ]
            },
            {
                "type": "body",
                "parameters": [
                    {
                    "type":"text",
                    "text":"Apple MacBook Pro (14-Inch, Apple M3 Pro Chip, 11-Core CPU, 14-Core GPU, 18GB Unified Memory, 512GB SSD, macOS, Space Black)"
                    }
                ]
            }
            ]
        }
    }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response


response = send_carousel_message()
print(response.status_code)
print(response.json())
