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

def mark_read(message_id):
    print("Message ID: ", message_id)
    print("Entered mark_read")
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(data))
    print(f"Response: {response.status_code}, {response.json()}")

mark_read("wamid.HBgMOTE3OTcyNTEwNjI5FQIAEhgUM0FGNUJCOEE2MjA5MzM0MTdCQzkA")