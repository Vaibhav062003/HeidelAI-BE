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
        "to": vipul,
        "type": "template",
        "template": {
            "name": "carousel_template_media_cards_v2",
            "language": {
            "code": "en_US"
            },
            "components": [

            {
                "type": "body",
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
                            "type": "image",
                            "image": {
                            "id": "559325606779082"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                    },

                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
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
                            "type": "image",
                            "image": {
                            "id": "1320987485953333"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                    },

                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
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

# Example usage
response_text = "Check out these amazing products curated just for you!"
products = [
    {
        "image_url": "https://egeniestores.com/wp-content/uploads/2024/05/61aHJIjnH6L._SL1500_.jpg",
        "price": "Logitech G413 TKL SE Wired Mechanical Gaming Keyboard",
        "url": "https://egeniestores.com/product/logitech-g413-tkl-se-wired-mechanical-gaming-keyboard/"
    },
    {
        "image_url": "https://egeniestores.com/wp-content/uploads/2024/04/61xf2zTjBeL._SL1500_.jpg",
        "price": "Cosmic Byte ARES Wireless Controller for PC",
        "url": "https://egeniestores.com/product/cosmic-byte-ares-wireless-controller-gamepad/"
    }
]

response = send_carousel_message()
print(response.status_code)
print(response.json())
