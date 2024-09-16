from fastapi import FastAPI, Request
import json, requests, os
from dotenv import load_dotenv

app = FastAPI()

load_dotenv()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")

@app.get("/webhook")
async def verify_webhook(request: Request):
    args = request.query_params
    if args.get("hub.verify_token") == VERIFY_TOKEN:
        return args.get("hub.challenge")  # Respond with hub.challenge to verify webhook
    return {"status": "Verification failed"}

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()

    # Log incoming message payload for debugging
    print("Incoming webhook payload:", json.dumps(payload, indent=2))

    # Check if the webhook contains a message event
    if "entry" in payload and len(payload["entry"]) > 0:
        for entry in payload["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if messages:
                    for message in messages:
                        # Extract the sender's phone number and the message text
                        sender = message["from"]
                        message_text = message["text"]["body"]
                        
                        # Send a reply
                        await send_message(sender, "Thanks for your message!")

    return {"status": "ok"}

async def send_message(recipient, message_text):
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {
            "body": message_text
        }
    }

    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")
