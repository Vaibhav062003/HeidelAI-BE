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

def send_flow():
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "recipient_type": "individual",
        "messaging_product": "whatsapp",
        "to": rounak,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {
                "type": "text",
                "text": "Complete your order"
            },
            "body": {
                "text": "Thanks for sending your us cart! 📦🤩\n\nBefore we can proceed, we just need a few more details about *your address* so we know exactly where to deliver your order.\n\nLet's get this sorted and your items will be on their way!🚀"
            },
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": "AQAAAAACS5FpgQ_cAAAAAD0QI3s.",
                    "flow_id": "1278152453552456",
                    "flow_cta": "Enter details",
                    "flow_action": "navigate",
                    "flow_action_payload": {
                    "screen": "contact_details"
                    }
                }
            }
        }
        }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    return response


response = send_flow()
print(response.status_code)
print(response.json())
