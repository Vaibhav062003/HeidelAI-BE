import time, requests, os, json, hmac, hashlib
from asyncio import Lock
from pinecone import Pinecone
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import AgentExecutor, create_structured_chat_agent
from langchain_core.runnables.history import RunnableWithMessageHistory
from sentence_transformers import SentenceTransformer
from google.oauth2 import service_account
from google.cloud import firestore
from langchain_google_firestore import FirestoreChatMessageHistory
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from collections import OrderedDict
from langchain import hub

# Regular dictionary to store processed message IDs with their timestamps
processed_message_ids = OrderedDict()
EXPIRY_TIME_SECONDS = 86400 

address_details = []

load_dotenv()

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_ID = os.getenv("IG_ID")
IG_VERSION = os.getenv("IG_VERSION")

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERSION = os.getenv("VERSION")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
APP_SECRET = os.getenv("APP_SECRET")

# Define the path to your service account JSON file
SERVICE_ACCOUNT_FILE = 'whatsapp-chatbot-history-cf43052a413d.json'

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)

# Initialize Firestore using the credentials
client = firestore.Client(credentials=credentials)

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PINECONE_INDEX = os.getenv("PINECONE_INDEX")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")

websocket_product_data = {}
websocket_product_data_ig = {}
flag = False
search_term = ""
websocket_data_lock = Lock()

model = SentenceTransformer('all-MiniLM-L6-v2')

def initialize_pinecone():
    pc = Pinecone(api_key=PINECONE_API_KEY)

    index_name = PINECONE_INDEX
    
    index = pc.Index(index_name)
    return index

def initialize_llm():
    llm = ChatGoogleGenerativeAI(model=LLM_MODEL, temperature=0)
    return llm

@tool
def fetch_company_info(dummy: str) -> str:
    """
    Fetches both company information and policies for Egenie4u.
    Use this tool if the user asks for contact information or company policies.
    Returns:
        str: A confirmation of the products displayed to the user.
    """
    return """
    Egenie4u Policies:
    - Easy returns and fast shipping.
    - All products come with GST bills and are brand new, sealed.
    - Warranty claims supported through respective companies using Egenie4u's GST bill.

    About Egenie4u:
    - 22 years of wholesale expertise translated into competitive retail pricing.
    - Wide range of products, including high-end gaming PCs, consoles, and computer peripherals.
    - Regular promotions and discounts on popular items.
    - Commitment to customer satisfaction and after-sales support.

    For more information, contact us at:
    Contact Number: +918408062973
    Address: 9, 257/b, Tiranga Bhavan, Opposite Dagduseth Ganapati Mandir, Pune - 411002
    GST Number: 27BCLPM6314F1ZK
    """

@tool
def fetch_product_data(client_id: str, query: str, category: str, price: int = 0) -> str:
    """
    Searches the vector database for products based on the user's query, category, and price.
    There are these fixed categories - "device", "custom", "accessories".

    Args:
        client_id (str): The client's unique identifier.
        query (str): The user's search query.
        category (str): The category of the product.
        price (int): The price of the product.

    Returns:
        str: A confirmation of the products displayed to the user.
    """

    global websocket_product_data 
    global websocket_product_data_ig 
    global search_term 

    search_term = query

    filters = {
        "category": category,
        "stock_quantity": {"$gt": 0}
    }
    if price != 0:
        filters["price"] = {"$lte": int(price)}

    print(f"Query: {query}, Category: {category}, Price: {price}")

    query_vector = model.encode(query).tolist()
    search_results = index.query(
        vector=query_vector,
        filter=filters,
        top_k=5,
        include_values=False,
        include_metadata=True
    )

    product_data = [
        result['metadata']
        for result in search_results['matches']  
    ]

    product_list = []
    product_string = "Here are the products sent to the user:\n"

    for product in product_data:
        product_string += f"Name: {product['name']}, Price: {product['price']}\n"
        product_dict = {
        "id": product['product_id'],
        "name": product['name'],
        "price": product['price'],
        "permalink": product['permalink'],
        "image_link": product['images'],
        "on_sale": product['on_sale'],
        "regular_price": product['regular_price'],
        }
        product_list.append(product_dict)

    websocket_product_data[client_id] = product_list
    print("Lenght: ", len(websocket_product_data[client_id]))
    websocket_product_data_ig[client_id] = product_list
    print("Lenght: ", len(websocket_product_data_ig[client_id]))
    return product_string

def get_session_history(session_id: str) -> FirestoreChatMessageHistory:

    global flag

    chat_history = FirestoreChatMessageHistory(
        session_id=session_id, collection="HistoryMessages", client=client
    )
    
    if len(chat_history.messages) > 20:
        flag = True

    return chat_history

def initialize_langchain_agent(llm: int):    

    tools = [fetch_product_data, fetch_company_info]

    prompt = hub.pull("customer-structured-chat-agent-prompt")

    agent = create_structured_chat_agent(llm, tools, prompt)
    agent_executor = AgentExecutor.from_agent_and_tools(agent=agent, 
                                   tools=tools, 
                                   verbose=True,
                                   handle_parsing_errors=True,
                                   max_iterations=100,
                                   )

    agent_with_chat_history = RunnableWithMessageHistory(
    agent_executor,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history",
    )

    return agent_with_chat_history

index = initialize_pinecone()

def get_agent():
    llm = initialize_llm()
    return initialize_langchain_agent(llm)

agent_executor = get_agent()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://egeniestores.com", "http://localhost"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def remove_expired_message_ids():
    current_time = time.time()
    expired_ids = []

    # Iterate through the dictionary to find expired message IDs
    for message_id, timestamp in processed_message_ids.items():
        if current_time - timestamp > EXPIRY_TIME_SECONDS:
            expired_ids.append(message_id)

    # Remove the expired message IDs
    for message_id in expired_ids:
        del processed_message_ids[message_id]

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
"""
Incoming webhook payload: {
  "object": "instagram",
  "entry": [
    {
      "time": 1732191852352,
      "id": "17841438674306985",
      "messaging": [
        {
          "sender": {
            "id": "2989171314563378"
          },
          "recipient": {
            "id": "17841438674306985"
          },
          "timestamp": 1732191851589,
          "message": {
            "mid": "aWdfZAG1faXRlbToxOklHTWVzc2FnZAUlEOjE3ODQxNDM4Njc0MzA2OTg1OjM0MDI4MjM2Njg0MTcxMDMwMTI0NDI1ODY5MzQ2MTMzOTk3NjI1NTozMTk1MzI5OTc3MjgyNzk4ODA5ODgxMjUxMzI0MjU3ODk0NAZDZD",
            "text": "Hi"
          }
        }
      ]
    }
  ]
}
"""

@app.post("/webhook")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None)):
    global flag
    global search_term
    global websocket_product_data
    global websocket_product_data_ig

    # Remove expired message IDs
    remove_expired_message_ids()


    # First, validate the signature
    payload = await request.json()
    print("Incoming webhook payload:", json.dumps(payload, indent=2))


    if payload["object"] == "instagram":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                
                for message in entry.get("messaging", []):
                    ig_message_id = message["message"]["mid"]
                    
                    if ig_message_id in processed_message_ids:
                        print("Duplicate message, skipping processing.")
                        continue
                    
                    # Add message ID with the current timestamp
                    processed_message_ids[ig_message_id] = time.time()

                    sender_id = message["sender"]["id"]
                    recipient_id = message["recipient"]["id"]
                    message_text = message["message"]["text"]

                    print(f"Message Received from {sender_id}, Message: {message_text}")
                    

                    if message_text == "Hi":
                        res = await send_ig_introduction(sender_id)
                        processed_message_ids[res['message_id']] = time.time()
                        print(f"Introduction sent to {sender_id}")
                        continue

                    user_input = f"(client_id: {sender_id}): " + message_text

                    response = agent_executor.invoke(
                        {"input": user_input},
                        config={"configurable": {"session_id": sender_id}},
                    )


                    async with websocket_data_lock:
                        print("websocket_product_data_ig 1: ", websocket_product_data_ig)
                        products_space = websocket_product_data_ig.get(sender_id)
                        print("Products space 2: ", products_space)

                        if products_space:
                            print("Came in if")
                            del websocket_product_data_ig[sender_id] 
                            res = await send_ig_carousel(sender_id, products_space)
                            processed_message_ids[res['message_id']] = time.time()
                            res2 = await send_ig_message(sender_id, response['output'])
                            processed_message_ids[res2['message_id']] = time.time()
                            search_term = ""
                            print("websocket_product_data_ig 2: ", websocket_product_data_ig) 
                        else:
                            print("Came in else")
                            res = await send_ig_message(sender_id, response['output'])
                            processed_message_ids[res['message_id']] = time.time()

                    print(f"Message sent to {sender_id}, Response: {response['output']}")

                    if flag:
                        flag = False
                        doc_ref = client.collection("HistoryMessages").document(sender_id)
                        doc = doc_ref.get()
                        
                        if doc.exists:
                            messages = doc.to_dict().get('messages', [])
                            print(f"Original messages count: {len(messages)}")
                            excess_count = len(messages) - 15
                            messages = messages[excess_count:] 
                            
                            doc_ref.update({'messages': messages})
                            print("Document updated with trimmed messages.")
    else:
        await signature_required(request, x_hub_signature_256)


        # Process the incoming message (if the signature is valid)

        # Handle incoming messages
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                for change in entry.get("changes", []):
                    value = change.get("value", {})

                    statuses = value.get("statuses", [])
                    if statuses:
                        for status in statuses:
                            message_status = status["status"]
                            recipient_id = status["recipient_id"]
                            print("Message Status: ", message_status, ", Recipient ID: ", recipient_id)

                    contacts = value.get("contacts", [])
                    if contacts:
                        for contact in contacts:
                            sender_name = contact["profile"]["name"]
                            # print("Sender name: ", sender_name)

                    messages = value.get("messages", [])
                    if messages:
                        for message in messages:
                            message_id = message["id"]

                            await mark_read(message_id)

                            # Check if message ID is already processed
                            if message_id in processed_message_ids:
                                print("Duplicate message, skipping processing.")
                                continue
                            
                            # Add message ID with the current timestamp
                            processed_message_ids[message_id] = time.time()

                            sender = message["from"]
                            
                            

                            if message["type"] == "text":
                                user_input = message["text"]["body"]
                                print("Message Received from Name: ", sender_name, ", Number: ",sender ,", Message: ", user_input)

                                if user_input == "Hi":
                                    await send_introduction(sender, sender_name)
                                    print("Introduction sent to Name: ", sender_name, ", Number: ",sender )
                                    continue
        

                                client_id = sender
                                user_input = f"(client_id: {client_id}): " + user_input

                                response = agent_executor.invoke(
                                    {"input": user_input},
                                    config={"configurable": {"session_id": client_id}},
                                )

                                async with websocket_data_lock:
                                    print("websocket_product_data 1: ", websocket_product_data)
                                    products_space = websocket_product_data.get(client_id)
                                    

                                    print("Products space 2: ", products_space)

                                    if products_space:
                                        print("Came in if")
                                        await send_message(sender, "Please give me a minute.")
                                        del websocket_product_data[client_id] 
                                        await send_carousel(sender, search_term, products_space)
                                        search_term = ""
                                        print("websocket_product_data 2: ", websocket_product_data) 
                                    else:
                                        print("Came in else")
                                        await send_message(sender, response['output'])

                                print("Products space 3: ", products_space)

                                if flag:
                                    flag = False
                                    doc_ref = client.collection("HistoryMessages").document(client_id)
                                    doc = doc_ref.get()
                                    
                                    if doc.exists:
                                        messages = doc.to_dict().get('messages', [])
                                        print(f"Original messages count: {len(messages)}")
                                        excess_count = len(messages) - 15
                                        messages = messages[excess_count:] 
                                        
                                        doc_ref.update({'messages': messages})
                                        print("Document updated with trimmed messages.")

                                print("Message sent to Name: ", sender_name, ", Number: ", sender, ", Response: ", response['output'])

                            elif message["type"] == "button":
                                print("Name: ", sender_name, ", Number: ",sender ,", clicked on know more")
                                payload = message["button"]["payload"]
                                print("Button Payload: ", payload)

                                

                                payload = payload.split("_")
                                print("Payload: ", payload)


                                # output = agent(payload)
                                # response = output['output']
                                # await send_message(sender, "This functionality is in development.")
                                print("Sender clicked Know More")
                                await send_product(sender, payload)
                                # print("Sent message to Name: ", sender_name, ", Number: ", sender, "")

                                # await send_product(sender, payload)
                            elif message["type"] == "order":
                                # response = f"Hey {sender_name},Thank you for trying to place and order with us. \n\n We're sorry this feature is not available yet."
                                # await send_message(sender, response)

                                product_items = message["order"]["product_items"]

                                for product in product_items:
                                    product_id = product["product_retailer_id"]
                                    quantity = product["quantity"]
                                    item_price = product["item_price"]
                                    currency = product["currency"]

                                    print(f"Product ID: {product_id}, Quantity: {quantity}, Price: {item_price}, Currency: {currency}")

                                print("Sender sent cart")
                                add_order_details(sender, product_items)
                                await get_details(sender, again=False)

                                # print("Sent message to Name: ", sender_name, ", Number: ", sender, ", Response: ", response)

                            elif message["type"] == "interactive":
                                print("Name: ", sender_name, ", Number: ",sender ,", sent details")
                                print("Sender clicked on interactive message")
                                interactive_type = message["interactive"]["type"]
                                
                                if interactive_type == "nfm_reply":
                                    # Extract the JSON string from the message 
                                    response_json_str = message["interactive"]["nfm_reply"]["response_json"]

                                    # Parse the JSON string into a Python dictionary
                                    response_json = json.loads(response_json_str)

                                    # Add the sender details to the address details list
                                    add_address_details(sender, response_json)
                                    print(address_details)
                                    # Print the parsed JSON
                                    # print(response_json)
                                    # {'pin_code': '411058', 'full_name': 'Rounak longani', 'flow_token': 'AQAAAAACS5FpgQ_cAAAAAD0QI3s.', 'state': 'Maharashtra ', 'email': 'Rounaklongani@gmail.com', 'city': 'Pune', 'country': '0_India', 'address': 'Hahsjs'}

                                    await confirm_address(sender, response_json)

                                elif interactive_type == "button_reply":
                                    
                                    payload = message["interactive"]["button_reply"]["id"]
                                    print("Reply Payload: ", payload)
                                    if payload == "info_correct":
                                        print("Info is correct")
                                        await get_payment(sender, sender_name)
                                    elif payload == "info_incorrect":
                                        print("Info is incorrect")
                                        delete_address_details(sender)
                                        await get_details(sender, again=True)

                            # await send_message(sender, "Got your responses. Thank you!\n\nThis functionality is still in development.")

    return {"status": "ok"}

async def mark_read(message_id):
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

async def send_ig_message(id, message_text):
    url = f"https://graph.facebook.com/{VERSION}/{IG_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + IG_ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
        "recipient":{
            "id":f"{id}"
        },
        "message":{
            "text":f"{message_text}"
        }
    }


    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")
    return response.json()

async def send_introduction(recipient, name):
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {
            "link": "https://egeniestores.com/wp-content/uploads/egenie.jpeg",
            "caption" : f"Hi {name}!👋 I'm *Talkative*, an AI-powered agent for eGenie Stores. I'm here to help you find the perfect tech products for your needs. \n \n✅ *Whether you're looking for:* \n  - A new phone\n  - A powerful gaming laptop\n  - A custom-built PC \n\nI can help you find the *best* options for your budget and preferences.🎉 \n\nFeel free to ask me any questions you may have about our *services* or *policies* , and I'll be happy to assist!📕"
        }
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(data))
    print(f"Response: {response.status_code}, {response.json()}")

async def send_ig_introduction(id):
    url = f"https://graph.facebook.com/{IG_VERSION}/{IG_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
        "recipient":{
            "id":f"{id}"
        },
        "message":{
            "text" :f"Hi!👋 I'm Talkative, an AI-powered agent for eGenie Stores. I'm here to help you find the perfect tech products for your needs. \n \n✅ Whether you're looking for: \n  - A new phone\n  - A powerful gaming laptop\n  - A custom-built PC \n\nI can help you find the best options for your budget and preferences.🎉 \n\nFeel free to ask me any questions you may have about our services or policies , and I'll be happy to assist!📕"

        }
    }
    
    response = requests.post(url, headers=headers, data=json.dumps(data))
    print(f"Response: {response.status_code}, {response.json()}")
    return response.json()

async def send_carousel(sender: str, search_term: str, products):
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
                    "text": search_term
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
                            "text": f'*{products[0]["name"][:50]}...*'
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
                                "payload": f"{str(int(products[0]['id']))}_0_{products[0]['name'][:50]}"
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
                            "link": products[1]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[1]["name"][:50]}...*'
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
                                "payload": f"{str(int(products[1]['id']))}_1_{products[1]['name'][:50]}"
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
                            "link": products[2]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[2]["name"][:50]} *'
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
                                "payload": f"{str(int(products[2]['id']))}_2_{products[2]['name'][:50]}"
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
                            "link": products[3]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[3]["name"][:50]}...*'
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
                                "payload": f"{str(int(products[3]['id']))}_3_{products[3]['name'][:50]}"
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
                            "link": products[4]["image_link"]
                            }
                        }
                        ]
                    },

                    {
                        "type": "body",
                        "parameters" : [
                        {
                            "type": "text",
                            "text": f'*{products[4]["name"][:50]}...*'
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
                                "payload": f"{str(int(products[4]['id']))}_4_{products[4]['name'][:50]}"
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
    print(f"Sent products to {sender}")

async def send_ig_carousel(id, products):
    elements = []

    for product in products:
        product_id = int(product["id"])

        url1 = f"https://graph.facebook.com/v20.0/759103595212051/products"
        params1 = {
            "filter" : json.dumps({
                "retailer_id" : { "eq" :  str(product_id) }
            }),
            "fields" : "id",
            "access_token" : {ACCESS_TOKEN}
        }

        response = requests.get(url1, params=params1)
        response = response.json()
        # print(response)
        if len(response['data']) == 0:
            print("Product not found")
            await send_ig_message(id, "Sorry, we could not find the product details. Please try again.")
            return

        product_cat_id = response['data'][0]['id']

        elements.append({ "id" : product_cat_id })


    url = f"https://graph.facebook.com/{IG_VERSION}/{IG_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {IG_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    
    # Building the carousel template with response variable and product details
    data = {
            "recipient":{
                "id":f"{id}"
        },
        "message":{
            "attachment":{
            "type":"template",
            "payload": {
                "template_type": "product",
                "elements": elements
            }
            }
        }
        }

    response = requests.post(url, headers=headers, data=json.dumps(data))
    print(f"Response: {response.status_code}, {response.json()}")
    return response.json()

async def send_product(sender, payload):
    index = int(payload[1])
    name = payload[2]

    print("Index: ", index)

    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
        "type": "interactive",
        "interactive": {
            "type": "product",
            "body": {
                "text": f"*{name}* \n\n🎉 *Key Highlights:* \n  - EMI Available! ✅ \n  - Get GST invoice and save up to 28% on business purchases \n  - Free Shipping all over India 🚚\n\n"
            },
            "footer": {
                "text": "To contact us, simply type 'Live Agent'"
            },
            "action": {
                "catalog_id": "759103595212051",
                "product_retailer_id": payload[0]
            }
        }
    }

    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")
    print(f"Message sent to {sender} with product {name}")

async def get_details(sender, again: bool):
    print(again)
    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    if again:
        print("Again")
        data = {
            "recipient_type": "individual",
            "messaging_product": "whatsapp",
            "to": sender,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "header": {
                    "type": "text",
                    "text": "Details Correction"
                },
                "body": {
                    "text": "Please enter correct details."
                },
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_token": str(sender),
                        "flow_id": "1253252929429522",
                        "flow_cta": "Enter details",
                        "flow_action": "navigate",
                        "flow_action_payload": {
                        "screen": "contact_details"
                        }
                    }
                }
            }
        }
    else:
        print("First time")
        # Building the carousel template with response variable and product details
        data = {
            "recipient_type": "individual",
            "messaging_product": "whatsapp",
            "to": sender,
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
                        "flow_token": str(sender),
                        "flow_id": "1253252929429522",
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
    print(f"Response: {response.status_code}, {response.json()}")

async def confirm_address(sender, response_json):
    full_name = response_json['full_name']
    number = response_json['flow_token']
    email = response_json['email']
    parse_country = response_json['country']
    parse_country = parse_country.split("_")
    country = parse_country[1]
    pin_code = response_json['pin_code']
    address = response_json['address']
    state = response_json['state']
    city = response_json['city']
    gst_number = response_json.get('gst_number', 'N/A')
    billing_address = response_json.get('billing_address', 'none')
    billing_state = response_json.get('billing_state', 'none')
    billing_city = response_json.get('billing_city', 'none')
    billing_pincode = response_json.get('billing_pincode', 'none')

    url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    if gst_number == 'N/A' and billing_address != 'none':
        # shipping + billing
        data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header" : {
                "type":"text",
                "text":"Address Confirmation"
            },
            "body": {
                "text": "Did we get it right?🤔 Please confirm whether this info is correct \n\n"
                        "*Contact Details* \n"
                            f"Name : {full_name}\n"
                            f"Email : {email} "
                        "\n\n*Shipping Address* \n"
                            f"Address: {address}\n"
                            f"State: {state}\n"
                            f"City: {city}\n"
                            f"PIN Code: {pin_code}"
                        "\n\n*Billing Address* \n"
                            f"Address: {billing_address}\n"
                            f"State: {billing_state}\n"
                            f"City: {billing_city}\n"
                            f"PIN Code: {billing_pincode}\n"
            },
            "footer" : {
                "text":"Only use the buttons to reply👇"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_correct",
                            "title": "Yes"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_incorrect",
                            "title": "No"
                        }
                    }
                ]
            }
        }
    }
    elif billing_address == 'none' and gst_number != 'N/A':
        # shipping + gst
        data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header" : {
                "type":"text",
                "text":"Address Confirmation"
            },
            "body": {
                "text": "Did we get it right?🤔 Please confirm whether this info is correct \n\n"
                        "*Contact Details* \n"
                            f"Name : {full_name}\n"
                            f"Email : {email} "
                        "\n\n*Shipping + Billing Address* \n"
                            f"Address: {address}\n"
                            f"State: {state}\n"
                            f"City: {city}\n"
                            f"PIN Code: {pin_code}"
                        "\n\n*Business Details* \n"
                            f"GST Number: {gst_number}\n"
            },
            "footer" : {
                "text":"Only use the buttons to reply👇"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_correct",
                            "title": "Yes"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_incorrect",
                            "title": "No"
                        }
                    }
                ]
            }
        }
    }
    elif billing_address == 'none' and gst_number == 'N/A':
        # shipping
        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": sender,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header" : {
                    "type":"text",
                    "text":"Address Confirmation"
                },
                "body": {
                    "text": "Did we get it right?🤔 Please confirm whether this info is correct \n\n"
                            "*Contact Details* \n"
                            f"Name : {full_name}\n"
                            f"Email : {email} "
                        "\n\n*Shipping + Billing Address* \n"
                            f"Address: {address}\n"
                            f"State: {state}\n"
                            f"City: {city}\n"
                            f"PIN Code: {pin_code}\n"
                },
                "footer" : {
                    "text":"Only use the buttons to reply👇"
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": "info_correct",
                                "title": "Yes"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": "info_incorrect",
                                "title": "No"
                            }
                        }
                    ]
                }
            }
        }
    else:
        # billing + shipping + gst
        data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "header" : {
                "type":"text",
                "text":"Address Confirmation"
            },
            "body": {
                "text": "Did we get it right?🤔 Please confirm whether this info is correct \n\n"
                        "*Contact Details* \n"
                            f"Name : {full_name}\n"
                            f"Email : {email} "
                        "\n\n*Shipping Address* \n"
                            f"Address: {address}\n"
                            f"State: {state}\n"
                            f"City: {city}\n"
                            f"PIN Code: {pin_code}"
                        "\n\n*Billing Address* \n"
                            f"Address: {billing_address}\n"
                            f"State: {billing_state}\n"
                            f"City: {billing_city}\n"
                            f"PIN Code: {billing_pincode}"
                        "\n\n*Business Details* \n"
                            f"GST Number: {gst_number}\n"
            },
            "footer" : {
                "text":"Only use the buttons to reply👇"
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_correct",
                            "title": "Yes"
                        }
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "info_incorrect",
                            "title": "No"
                        }
                    }
                ]
            }
        }
    }


    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")

async def get_payment(sender, sender_name):
    order_id = generate_order_id()
    details = get_sender_details(sender)

    if details is None:
        print("Details not found for sender")
        await send_message(sender, "Sorry, we could not find your address details. Please try again.")
        return
    
    items = []

    for product in details["order_details"]:
        product_id = product["product_retailer_id"]
        quantity = product["quantity"]
        item_price = product["item_price"]
        # currency = product["currency"]
        # print(f"Product ID: {product_id}, Quantity: {quantity}, Price: {item_price}, Currency: {currency}")

        url1 = f"https://graph.facebook.com/v20.0/759103595212051/products"
        params1 = {
            "filter" : json.dumps({
                "retailer_id" : { "eq" :  product_id }
            }),
            "fields" : "id",
            "access_token" : {ACCESS_TOKEN}
        }

        response = requests.get(url1, params=params1)
        response = response.json()
        # print(response)
        if len(response['data']) == 0:
            print("Product not found")
            await send_message(sender, "Sorry, we could not find the product details. Please try again.")
            return

        product_cat_id = response['data'][0]['id']

        url = f"https://graph.facebook.com/{product_cat_id}"

        params = {
            "access_token": ACCESS_TOKEN,
            "fields": "name, price, image_url"
        }

        response = requests.get(url, params=params)
        response = response.json()
        price = response['price']
        clean_price = price.replace("₹", "").replace(",", "")
        clean_price = int(float(clean_price))
        product_details = {
            "name":response['name'],
            "amount": {
                "value": int(f"{clean_price}00"),
                "offset": 100
            },
            "sale_amount": {
                "value": int(f"{item_price}00"),
                "offset": 100
            },
            "quantity": int(quantity),
            "country_of_origin":"IN",
            "image" : {
                "link" : response['image_url']
            }
        }

        items.append(product_details)

    print(f"Items: {items}")

    sum = 0
    for item in items:
        sum += (int(item['quantity']) * int(item['sale_amount']['value']))


    url = f"https://graph.facebook.com/{IG_VERSION}/{PHONE_NUMBER_ID}/messages"

    headers = {
        "Authorization" : "Bearer " + ACCESS_TOKEN,
        "Content-Type" : "application/json",
    }
    data = {
    "messaging_product": "whatsapp",
    "recipient_type": "individual",
    "to": sender,
    "type": "interactive",
    "interactive": {
        "type": "order_details",
        "header": {
            "type": "text",
            "text": "Complete your order"
        },
        "body": {
            "text": f"{sender_name}, you are one step away!😎\n\nComplete your payment and confirm your order with us.📦 \n\nTo contact us regarding any queries please reply with *Live agent*"
        },
        "action": {
            "name": "review_and_pay",
            "parameters": {
                "reference_id": str(order_id),
                "type": "physical-goods",
                "beneficiaries" : [
                    {
                        "name":f"{details['address']['full_name']}",
                        "address_line1":f"{details['address']['address']}",
                        "city":f"{details['address']['city']}",
                        "state":f"{details['address']['state']}",
                        "country":"India",
                        "postal_code":f"{details['address']['pin_code']}",
                    }
                ],
                "payment_type": "upi",
                "payment_configuration": "egenieupi",
                "currency": "INR",
                "total_amount": {
                    "value": int(sum),
                    "offset": 100
                },
                "order": {
                    "status": "pending",
                    "items": items,
                    "subtotal": {
                        "value": int(sum),
                        "offset": 100
                    },
                    "tax": {
                        "value": 0,
                        "offset": 100
                    },
                    "discount": {
                        "value": 0,
                        "offset": 100
                    }
                    

                }
            }
        }
    }
}

    response = requests.post(url, headers=headers, json=data)
    print(f"Response: {response.status_code}, {response.json()}")

def generate_order_id():
    # File to store the last used order ID
    file_path = "last_order_id.txt"
    
    # Check if the file exists
    if os.path.exists(file_path):
        # Read the last order ID
        with open(file_path, "r") as file:
            last_order_id = int(file.read())
    else:
        # Start from 1000 if the file does not exist
        last_order_id = 1000
    
    # Increment the order ID
    new_order_id = last_order_id + 1

    # Save the new order ID to the file
    with open(file_path, "w") as file:
        file.write(str(new_order_id))
    
    # Return the new order ID
    return new_order_id

def add_order_details(sender, product_items):
    # Check if sender already exists in address_details
    sender_entry = next((entry for entry in address_details if entry["sender_id"] == sender), None)
    
    if sender_entry:
        # If sender already exists, just update the order details
        sender_entry["order_details"] = product_items
        print(f"Updated order details for sender: {sender}")
    else:
        # If sender does not exist, create a new entry with order details and placeholder for address
        address_details.append({
            "sender_id": sender,
            "order_details": product_items,
            "address": None  # Placeholder for address
        })
        print(f"Added new entry with order details for sender: {sender}")

def add_address_details(sender, address_info):
    # Find the existing entry for the sender
    sender_entry = next((entry for entry in address_details if entry["sender_id"] == sender), None)
    
    if sender_entry:
        # Update the address details for the existing sender
        sender_entry["address"] = address_info
        print(f"Added address details for sender: {sender}")
    else:
        address_details.append({
                "sender_id": sender,
                "order_details": None,  # Placeholder for order details
                "address": address_info
            })
        print(f"Added new entry with address details for sender: {sender}")

def delete_address_details(sender):
    # Find the existing entry for the sender
    sender_entry = next((entry for entry in address_details if entry["sender_id"] == sender), None)
    
    if sender_entry:
        # Set the address to None, effectively removing address details
        sender_entry["address"] = None
        print(f"Address details removed for sender: {sender}")
    else:
        print(f"No details found for sender: {sender}")

def get_sender_details(sender):
    # Find the entry for the sender
    sender_entry = next((entry for entry in address_details if entry["sender_id"] == sender), None)
    
    if sender_entry:
        # Return the full details of the sender
        print(f"Details for sender {sender}: {sender_entry}")
        return sender_entry
    else:
        print(f"No details found for sender: {sender}")
        return None
# recieved  payment
"""
Incoming webhook payload: {
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "389116497625642",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "918788424068",
              "phone_number_id": "423792927480992"
            },
            "statuses": [
              {
                "id": "wamid.HBgMOTE3OTcyNTEwNjI5FQIAEhgKMTMzMjkxOTg3MgA=",
                "status": "captured",
                "timestamp": "1727439107",
                "recipient_id": "917972510629",
                "type": "payment",
                "payment": {
                  "reference_id": "123456789",
                  "amount": {
                    "value": 100,
                    "offset": 100
                  },
                  "currency": "INR",
                  "transaction": {
                    "id": "order_P2BafH9GcXfe2G",
                    "type": "razorpay",
                    "status": "success",
                    "created_timestamp": 1727439107,
                    "updated_timestamp": 1727439107,
                    "amount": {
                      "value": 100,
                      "offset": 100
                    },
                    "currency": "INR",
                    "method": {
                      "type": "upi"
                    }
                  },
                  "receipt": "receipt-value",
                  "notes": {
                    "key1": "value1"
                  }
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}
"""


"""
User sent details:
Incoming webhook payload: {
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "389116497625642",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "918788424068",
              "phone_number_id": "423792927480992"
            },
            "contacts": [
              {
                "profile": {
                  "name": "Rounak"
                },
                "wa_id": "917972510629"
              }
            ],
            "messages": [
              {
                "context": {
                  "from": "918788424068",
                  "id": "wamid.HBgMOTE3OTcyNTEwNjI5FQIAERgSNTkxNjYzMkYwNjlDNjEyQUEwAA=="
                },
                "from": "917972510629",
                "id": "wamid.HBgMOTE3OTcyNTEwNjI5FQIAEhgUM0FCMjI0NDM4OTAxQTA3MjY1NEYA",
                "timestamp": "1727263232",
                "type": "interactive",
                "interactive": {
                  "type": "nfm_reply",
                  "nfm_reply": {
                    "response_json": "{\"email\":\"rounak@gmail.com\",\"country\":\"0_India\",\"flow_token\":\"AQAAAAACS5FpgQ_cAAAAAD0QI3s.\",\"pin_code\":\"411058\",\"address\":\"Warje pune\",\"state\":\"Maharashtra \",\"full_name\":\"Rounak longani\",\"city\":\"Pune\"}",
                    "body": "Sent",
                    "name": "flow"
                  }
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}
"""



"""
User sent cart then payload is:

Incoming webhook payload: {
  "object": "whatsapp_business_account",
  "entry": [
    {
      "id": "389116497625642",
      "changes": [
        {
          "value": {
            "messaging_product": "whatsapp",
            "metadata": {
              "display_phone_number": "918788424068",
              "phone_number_id": "423792927480992"
            },
            "contacts": [
              {
                "profile": {
                  "name": "Rounak"
                },
                "wa_id": "917972510629"
              }
            ],
            "messages": [
              {
                "from": "917972510629",
                "id": "wamid.HBgMOTE3OTcyNTEwNjI5FQIAEhgUM0E3QTFCQkY3NEQ1OUYwNTBCRTMA",
                "timestamp": "1726773211",
                "type": "order",
                "order": {
                  "catalog_id": "759103595212051",
                  "text": "",
                  "product_items": [
                    {
                      "product_retailer_id": "MRX33HN/A_201",
                      "quantity": 1,
                      "item_price": 185000,
                      "currency": "INR"
                    }
                  ]
                }
              }
            ]
          },
          "field": "messages"
        }
      ]
    }
  ]
}
"""



# FLOW JSON
"""
{"version":"3.1","screens":[{"id":"RECOMMEND","title":"Contact Details","data":{},"layout":{"type":"SingleColumnLayout","children":[{"type":"Form","name":"flow_path","children":[{"type":"TextSubheading","text":"Full name"},{"type":"TextInput","label":"Eg. Steve Jobs","name":"TextInput_3be76e","required":true,"input-type":"text","helper-text":""},{"type":"TextSubheading","text":"Contact Information"},{"type":"TextInput","label":"Email address","name":"TextInput_863f4d","required":true,"input-type":"email","helper-text":""},{"type":"TextCaption","text":"We'll use this email to send you details and updates about your order."},{"type":"Footer","label":"Continue","on-click-action":{"name":"navigate","next":{"type":"screen","name":"screen_xdycdl"},"payload":{"screen_0_TextInput_0":"${form.TextInput_3be76e}","screen_0_TextInput_1":"${form.TextInput_863f4d}"}}}]}]}},{"id":"screen_xdycdl","title":"Delivery Details","data":{"screen_0_TextInput_0":{"type":"string","__example__":"Example"},"screen_0_TextInput_1":{"type":"string","__example__":"Example"}},"terminal":true,"layout":{"type":"SingleColumnLayout","children":[{"type":"Form","name":"flow_path","children":[{"type":"TextSubheading","text":"Shipping Address"},{"type":"TextCaption","text":"Enter the address where you want your order delivered.\nNote: This address will be used for billing."},{"type":"Dropdown","label":"Country","required":true,"name":"Dropdown_057767","data-source":[{"id":"0_India","title":"India"}]},{"type":"TextArea","label":"Address","required":true,"name":"TextArea_72d055"},{"type":"TextInput","label":"State","name":"TextInput_1a72ca","required":true,"input-type":"text","helper-text":""},{"type":"TextInput","label":"City","name":"TextInput_064ada","required":true,"input-type":"text"},{"type":"TextInput","label":"PIN Code","name":"TextInput_e94033","required":true,"input-type":"number"},{"type":"Footer","label":"Submit","on-click-action":{"name":"complete","payload":{"screen_1_Dropdown_0":"${form.Dropdown_057767}","screen_1_TextArea_1":"${form.TextArea_72d055}","screen_1_TextInput_2":"${form.TextInput_1a72ca}","screen_1_TextInput_3":"${form.TextInput_064ada}","screen_1_TextInput_4":"${form.TextInput_e94033}","screen_0_TextInput_0":"${data.screen_0_TextInput_0}","screen_0_TextInput_1":"${data.screen_0_TextInput_1}"}}}]}]}}]}
{"version":"3.1","screens":[{"id":"RECOMMEND","title":"Contact Details","data":{},"layout":{"type":"SingleColumnLayout","children":[{"type":"Form","name":"flow_path","children":[{"type":"TextSubheading","text":"Full name"},{"type":"TextInput","label":"Eg. Steve Jobs","name":"TextInput_3be76e","required":true,"input-type":"text","helper-text":""},{"type":"TextSubheading","text":"Contact Information"},{"type":"TextInput","label":"Email address","name":"TextInput_863f4d","required":true,"input-type":"email","helper-text":""},{"type":"TextCaption","text":"We'll use this email to send you details and updates about your order."},{"type":"Footer","label":"Continue","on-click-action":{"name":"navigate","next":{"type":"screen","name":"screen_xdycdl"},"payload":{"screen_0_TextInput_0":"${form.TextInput_3be76e}","screen_0_TextInput_1":"${form.TextInput_863f4d}"}}}]}]}},{"id":"screen_xdycdl","title":"Delivery Details","data":{"screen_0_TextInput_0":{"type":"string","__example__":"Example"},"screen_0_TextInput_1":{"type":"string","__example__":"Example"}},"terminal":true,"layout":{"type":"SingleColumnLayout","children":[{"type":"Form","name":"flow_path","children":[{"type":"TextSubheading","text":"Shipping Address"},{"type":"TextCaption","text":"Enter the address where you want your order delivered.\nNote: This address will be used for billing."},{"type":"Dropdown","label":"Country","required":true,"name":"Dropdown_057767","data-source":[{"id":"0_India","title":"India"}]},{"type":"TextArea","label":"Address","required":true,"name":"TextArea_72d055"},{"type":"TextInput","label":"State","name":"TextInput_1a72ca","required":true,"input-type":"text","helper-text":""},{"type":"TextInput","label":"City","name":"TextInput_064ada","required":true,"input-type":"text"},{"type":"TextInput","label":"PIN Code","name":"TextInput_e94033","required":true,"input-type":"number"},{"type":"Footer","label":"Submit","on-click-action":{"name":"complete","payload":{"screen_1_Dropdown_0":"${form.Dropdown_057767}","screen_1_TextArea_1":"${form.TextArea_72d055}","screen_1_TextInput_2":"${form.TextInput_1a72ca}","screen_1_TextInput_3":"${form.TextInput_064ada}","screen_1_TextInput_4":"${form.TextInput_e94033}","screen_0_TextInput_0":"${data.screen_0_TextInput_0}","screen_0_TextInput_1":"${data.screen_0_TextInput_1}"}}}]}]}}]}

"""