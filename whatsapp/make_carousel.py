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
        "name": "carousel_template_media_cards_v7",
        "language": "en_US",
        "category": "marketing",
        "components": [
            {
            "type": "body",
            "text": "Here are some {{1}} you might be interested in:",
            "example": {
                "body_text": [
                    [
                        "keyboards"
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
                    "format": "image",
                    "example": {
                        "header_handle": [
                        "4:aW1hZ2UuanBn:aW1hZ2UvanBlZw==:ARYxoXEPJ6ckl5-XC5Smfsuhi1dLBj8m4wOuOp8jRye6xBsgYsl9NGAKTp-k2-51f2QAiUzIbmE-CQIiv0LwoMyqAlC-LVZ87IiM7fesXOXkCw:e:1726908382:509681741462833:61562573933016:ARb8Y5_ZMIsJkEU1BqQ"
                        ]
                    }
                    },

                    {
                    "type": "body",
                    "text": "The {{1}}, priced at : {{2}}",
                    "example": {
                        "body_text": [
                            [
                                "Logitech G413 TKL SE Wired Mechanical Gaming Keyboard",
                                "$69.99"
                            ]
                        ]
                    }
                    },

                    {
                    "type": "buttons",
                    "buttons": [
                        {
                        "type": "url",
                        "text": "View on Website",
                        "url": "https://egeniestores.com/product/{{1}}",
                        "example": [
                            "logitech-g413-tkl-se-wired-mechanical-gaming-keyboard/"
                        ]
                        },
                    ]
                    }
                ]
                },
                {
                "components": [
                    {
                    "type": "header",
                    "format": "image",
                    "example": {
                        "header_handle": [
                        "4:aW1hZ2UuanBn:aW1hZ2UvanBn:ARaVslx1-ZWp-1Okc5RdVDHYatSyQoHqrDQavM_G_FEoCJzrr2hHHUPd4zOjAXKMVxmFNCxsKGkS3rXVyfTPNvzs3e4e9u6meIRq3zUJapal1g:e:1726908566:509681741462833:61562573933016:ARZtBWKSJyxvJz_8FYM"
                        ]
                    }
                    },

                    {
                    "type": "body",
                    "text": "The {{1}}, priced at: {{2}}",
                    "example": {
                        "body_text": [
                            [
                                "Cosmic Byte ARES Wireless Controller for PC  Price: $39.99",
                                "$39.99"
                            ]
                        ]
                    }
                    },

                    {
                    "type": "buttons",
                    "buttons": [
                        {
                        "type": "url",
                        "text": "View on Website",
                        "url": "https://egeniestores.com/product/{{1}}",
                        "example": [
                            "cosmic-byte-ares-wireless-controller-gamepad/"
                        ]
                        },
                    ]
                    }
                ]
                },

            ]
            }
        ]
        
    }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = create_carousel()
print(response.status_code)
print(response.json())