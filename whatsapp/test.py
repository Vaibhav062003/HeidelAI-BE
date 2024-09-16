import json, os, requests, aiohttp, asyncio
from dotenv import load_dotenv

load_dotenv()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
RECIPIENT_WAID = os.getenv("RECIPIENT_WAID")


def send_message():
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"

    print(url)
    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
        "messaging_product" : "whatsapp",
        "to" : "+918815785354",
        "type" : "template",
        "template" : {
            "name" : "hello_world",
            "language" : {"code" : "EN_US"}
        }
    }
    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response

response = send_message()
print(response.status_code)
print(response.json())
