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
        "to": "+918815785354",
        "type": "template",
        "template": {
            "name": "carousel_template_test_v38",
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
                            "type": "image",
                            "image": {
                            "link": "https://egeniestores.com/wp-content/uploads/2024/04/7ebc8d65-d257-42f0-85f4-cdd1993033cd23021130-300x300-1.jpg"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": "*Apple MacBook Pro*"
                        },
                        {
                            "type": "text",
                            "text": "*Rs. 1,19,900*"
                        },
                        ]
                    },

                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": "MRX33HN/A_201"
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
                            "type": "image",
                            "image": {
                            "id": "491457840369215"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": "*Lenovo Legion 5 Pro*"
                        },
                        {
                            "type": "text",
                            "text": "*Rs. 86,700*"
                        },
                        ]
                    },

                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": "82JQ00JCIN_470"
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
                            "type": "image",
                            "image": {
                            "id": "1078100377368226"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": "*ASUS TUF Gaming A15 2023 Gaming Laptop*"
                        },
                        {
                            "type": "text",
                            "text": "*Rs. 1,21,990*"
                        },
                        ]
                    },

                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": "FA506ICB-HN075W_3780"
                            }
                        ]
                    }
                    ]
                },
                {
                    "card_index": 3,
                    "components": [
                    {
                        "type": "header",
                        "parameters": [
                        {
                            "type": "image",
                            "image": {
                            "id": "478952854961267"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": "*Lenovo LOQ 2024 Gaming Laptop*"
                        },
                        {
                            "type": "text",
                            "text": "*Rs. 77,700*"
                        },
                        ]
                    },

                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": "wc_post_id_801"
                            }
                        ]
                    }
                    ]
                },
                {
                    "card_index": 4,
                    "components": [
                    {
                        "type": "header",
                        "parameters": [
                        {
                            "type": "image",
                            "image": {
                            "id": "841083418167186"
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": "*SONY PLAYSTATION 5*"
                        },
                        {
                            "type": "text",
                            "text": "*Rs. 51,500*"
                        },
                        ]
                    },

                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [
                            {
                                "type": "payload",
                                "payload": "wc_post_id_407"
                            }
                        ]
                    }
                    ]
                },
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
