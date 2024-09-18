import requests, os, json, hmac, hashlib
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
APP_SECRET = os.getenv("APP_SECRET")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://egeniestores.com", "http://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def validate_signature(payload, signature):
    # Hash the payload with the app secret
    expected_signature = hmac.new(
        bytes(APP_SECRET, 'latin-1'),
        msg=payload.encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Compare signatures to ensure they match
    return hmac.compare_digest(f"sha256={expected_signature}", signature)

# Middleware or decorator to validate signature for POST requests
async def signature_required(request: Request, x_hub_signature_256: str = Header(None)):
    # Get the request payload
    body = await request.body()
    payload = body.decode('utf-8')

    if not x_hub_signature_256:
        raise HTTPException(status_code=403, detail="Signature missing")

    if not validate_signature(payload, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

@app.get("/webhook")
async def verify_webhook(request: Request):
    # This is for the webhook verification (GET request)
    args = request.query_params
    if args.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=str(args.get("hub.challenge")))
    return {"status": "Verification failed"}


@app.post("/webhook")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None)):
    # First, validate the signature
    await signature_required(request, x_hub_signature_256)

    # Process the incoming message (if the signature is valid)
    payload = await request.json()
    print("Incoming webhook payload:", json.dumps(payload, indent=2))

    # Handle incoming messages
    if "entry" in payload and len(payload["entry"]) > 0:
        for entry in payload["entry"]:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                if messages:
                    for message in messages:
                        sender = message["from"]
                        message_text = message["text"]["body"]

                        client_id = sender

                        message_text = f"client_id: {client_id}: " + message_text
                        response = message_text.Upper()
                        # output = agent(message_text)
                        # response = output['output']
                        await send_message(sender, response)


                        # products_space = websocket_product_data.get(client_id)

                        # print("Product Space: ", products_space)    
                        # if products_space:
                        #     response_data = {
                        #         "type": "products",
                        #         "message": response,
                        #         "products": products_space
                        #     }
                        #     websocket_product_data[client_id] = []
                        # else:
                        #     response_data = {
                        #         "type": "message",
                        #         "message": response
                        #     }

                        # Convert the dictionary to JSON
                        # response_json = response_data

                        # if response_json["type"] == "message":                 
                        #     # Send a reply to the sender
                        #     await send_message(sender, response_json["message"])

                        # elif response_json["type"] == "products":
                        #     message = response_json["message"]
                        #     products = response_json["products"]
                        #     await send_carousel(sender, message, products)
    
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

def send_carousel(sender, message, products):
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
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
                    "text": "products"
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
                            "link": products[0]["image_link"] 
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[0]["name"][:50]}*'
                        },
                        {
                            "type": "text",
                            "text": f'*Rs. {products[0]["price"]}*'
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
                                "payload": "macbook-pro"
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
                            "id": products[1]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[1]["name"][:50]}*'
                        },
                        {
                            "type": "text",
                            "text": f'*Rs. {products[1]["price"]}*'
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
                                "payload": "macbook-pro"
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
                            "id": products[2]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[2]["name"][:50]}*'
                        },
                        {
                            "type": "text",
                            "text": f'*Rs. {products[2]["price"]}*'
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
                                "payload": "macbook-pro"
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
                            "id": products[3]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[3]["name"][:50]}*'
                        },
                        {
                            "type": "text",
                            "text": f'*Rs. {products[3]["price"]}*'
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
                                "payload": "macbook-pro"
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
                            "id": products[4]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[4]["name"][:50]}*'
                        },
                        {
                            "type": "text",
                            "text": f'*Rs. {products[4]["price"]}*'
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
                                "payload": "macbook-pro"
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
    print(f"Response: {response.status_code}, {response.json()}")