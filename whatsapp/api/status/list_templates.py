import json
import os
import requests
from dotenv import load_dotenv

load_dotenv()

BUSINESS_ACCOUNT_ID = os.getenv("BUSINESS_ACCOUNT_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

blank_message = "\u3164"

def create_carousel():
    url = f"https://graph.facebook.com/{BUSINESS_ACCOUNT_ID}/message_templates?fields=name,status,category,language"

    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN
    }


    response = requests.get(url, headers=headers)
    return response

response = create_carousel()
print(response.status_code)
print(response.json())

