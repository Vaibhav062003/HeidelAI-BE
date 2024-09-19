import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
CATALOGUE_ID = os.getenv("CATALOGUE_ID")
sahil = "+919511907785"
vipul = "+918408062973"
rounak = "+917972510629"


def send_carousel_message():
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": vipul,
        "type": "template",
        "template": {
            "name": "product_card_carousel_v1",
            "language": {
            "code": "en_US"
            },
            "components": [
            {
                "type": "body",
                "parameters": [
                {
                    "type": "text",
                    "text": "laptops"
                },
                {
                    "type": "text",
                    "text": "Do you have any specific preferences in mind, like a particular brand, screen size, or budget?"
                }
                ]
            },
            {
                "type": "carousel",
                "cards": [
                {
                    "card_index": 0,
                    "components": [
                    {
                        "type": "header",
                        "parameters": [
                        {
                            "type": "product",
                            "product": {
                            "product_retailer_id": "82JQ00JCIN_470",
                            "catalog_id": CATALOGUE_ID
                            }
                        }
                        ]
                    }
                    ]
                },
                {
                    "card_index": 1,
                    "components": [
                    {
                        "type": "header",
                        "parameters": [
                        {
                            "type": "product",
                            "product": {
                            "product_retailer_id": "FA617XS-N3026WS_746",
                            "catalog_id": CATALOGUE_ID
                            }
                        }
                        ]
                    }
                    ]
                },
                {
                    "card_index": 2,
                    "components": [
                    {
                        "type": "header",
                        "parameters": [
                        {
                            "type": "product",
                            "product": {
                            "product_retailer_id": "e0250ax_4465",
                            "catalog_id": CATALOGUE_ID
                            }
                        }
                        ]
                    }
                    ]
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
