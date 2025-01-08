import time, requests, os, json, hmac, hashlib
import uuid
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
from pymongo import MongoClient
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel
from pytz import timezone
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict
from bson.objectid import ObjectId
from enum import Enum


from send_mail import notify
# Store WebSocket connections
connected_clients: Dict[str, WebSocket] = {}


from fastapi import WebSocket



# from datetime import datetime
# from pytz import timezone

# from custom_mail_service import send_email
# india_time = datetime.now(timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S")
# send_email("rounaklongani@gmail.com", "Egenie - Koyeb" ,"Service was redepolyed", f"Koyeb of egeniesales20@gmail.com was redeployed at {india_time}")

# Add MongoDB connection for whatsapp support requests
MONGODB_URI = os.getenv("MONGODB_URI")
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client['Cluster0']  # database name
support_requests = db['support_requests']  # collection name
chat_history = db['chat_history']  # collection name


MONGODB_URI2 = os.getenv("MONGODB_URI2")
mongo_client2 = MongoClient(MONGODB_URI2)
db2 = mongo_client2['Cluster0']  # database name
conversations_collection = db["conversations"]
messages_collection = db["messages"]


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
BUSINESS_ACCOUNT_ID = os.getenv("BUSINESS_ACCOUNT_ID")

# Define the path to your service account JSON file
SERVICE_ACCOUNT_FILE = 'whatsapp-chatbot-history-cf43052a413d.json'

# Load the credentials from the JSON file
credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)

# Initialize Firestore using the credentials
client = firestore.Client(credentials=credentials)


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





"""DB FUNCTIONS BELOW"""

# Database initialisation to insert connversations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

DB_URI = os.getenv("DB_URI")

def initialize_database():
    db_uri = DB_URI

    # Create the SQLAlchemy engine
    engine = create_engine(db_uri, pool_pre_ping=True)
    
    # Create whatsapp table if it doesn't exist
    create_table_query = """
    CREATE TABLE IF NOT EXISTS whatsapp (
        id SERIAL PRIMARY KEY,
        name TEXT,
        query TEXT,
        response TEXT,
        time_taken TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    # Create instagram table if it doesn't exist
    create_table_users = """
    CREATE TABLE IF NOT EXISTS instagram (
        id SERIAL PRIMARY KEY,
        username TEXT,
        query TEXT,
        response TEXT,
        time_taken TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    with engine.connect() as connection:
        connection.execute(text(create_table_query))
        connection.execute(text(create_table_users))
    
    return engine

engine = initialize_database()

# function to store the message into whatsapp db
def insert_into_whatsapp( name, query, response, time_taken, max_retries=3, retry_delay=2):
    insert_query = text("""
    INSERT INTO whatsapp (name, query, response, time_taken)
    VALUES (:name, :query, :response, :time_taken)
    """)
    
    retries = 0
    while retries < max_retries:
        try:
            with engine.connect() as connection:
                connection.execute(insert_query, {
                    'name': name,
                    'query': query,
                    'response': response,
                    'time_taken': time_taken
                })
            print("Data inserted successfully.")
            break  # Exit the loop if the query succeeds
        except OperationalError as e:
            if 'SSL SYSCALL error: EOF detected' in str(e):
                retries += 1
                print(f"SSL SYSCALL error detected. Retrying {retries}/{max_retries}...")
                time.sleep(retry_delay)  # Wait before retrying
            else:
                raise e  # Re-raise if it's not the specific error we're handling

    if retries == max_retries:
        print("Failed to insert data after multiple retries.")

# function to store the message into instagram db
def insert_into_instagram( username, query, response, time_taken, max_retries=3, retry_delay=2):
    insert_query = text("""
    INSERT INTO instagram (username, query, response, time_taken)
    VALUES (:username, :query, :response, :time_taken)
    """)
    
    retries = 0
    while retries < max_retries:
        try:
            with engine.connect() as connection:
                connection.execute(insert_query, {
                    'username': username,
                    'query': query,
                    'response': response,
                    'time_taken': time_taken
                })
            print("Data inserted successfully.")
            break  # Exit the loop if the query succeeds
        except OperationalError as e:
            if 'SSL SYSCALL error: EOF detected' in str(e):
                retries += 1
                print(f"SSL SYSCALL error detected. Retrying {retries}/{max_retries}...")
                time.sleep(retry_delay)  # Wait before retrying
            else:
                raise e  # Re-raise if it's not the specific error we're handling

    if retries == max_retries:
        print("Failed to insert data after multiple retries.")

"""DB FUNCTIONS ABOVE"""


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
    allow_origins=["https://egeniestores.com", "https://heidelai.com","http://localhost", "http://wordpress.local", "http://wordpress.local:10004", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    try:
        await websocket.accept()
        connected_clients[user_id] = websocket
        print(f"Client connected: {user_id}")
        
        try:
            while True:
                # Keep the connection alive and wait for messages
                data = await websocket.receive_text()
                # Handle any incoming messages if needed
                print(f"Received message from {user_id}: {data}")
                
        except WebSocketDisconnect:
            print(f"Client disconnected: {user_id}")
        finally:
            # Clean up when the connection is closed
            if user_id in connected_clients:
                del connected_clients[user_id]
                
    except Exception as e:
        print(f"WebSocket error: {e}")
        if user_id in connected_clients:
            del connected_clients[user_id]

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



active_connections: List[WebSocket] = []

@app.websocket("/ws/conversations")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            # Wait for any messages (optional)
            data = await websocket.receive_text()
            # Process message if needed
    except Exception as e:
        print(f"Error: {e}")
    finally:
        active_connections.remove(websocket)

# Call this when a new message arrives
async def broadcast_new_message(conversation):
    message = {
        "type": "new_message",
        "conversation": conversation
    }
    for connection in active_connections:
        try:
            await connection.send_text(json.dumps(message))
        except Exception as e:
            print(f"Error sending message: {e}")



class MessageRole(str, Enum):
    CUSTOMER = "customer"
    AI = "ai"
    AGENT = "agent"
    SYSTEM = "system"

class Message2(BaseModel):
    id: str
    platform: str
    conversation_id: str
    content: str
    sender_id: str
    role: MessageRole
    timestamp: datetime
    status: str = "sent"

class Conversation(BaseModel):
    id: str
    platform: str
    customer_id: str
    customer_name: str
    last_message: str
    timestamp: datetime
    unread_count: int = 0
    avatar_url: Optional[str] = None
    is_ai_enabled: bool = True
    assigned_agent_id: Optional[str] = None


# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        print(f"Client connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            print(f"Client disconnected: {client_id}")

    async def broadcast(self, message: dict):
        disconnected_clients = []
        for client_id, websocket in self.active_connections.items():
            try:
                await websocket.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to {client_id}: {e}")
                disconnected_clients.append(client_id)
        
        for client_id in disconnected_clients:
            self.disconnect(client_id)

# Initialize manager
manager = ConnectionManager()

@app.get("/conversations")
async def get_conversations():
    conversations = list(conversations_collection.find({}))
    return [Conversation(**conv).dict() for conv in conversations]

@app.get("/conversations/{conversation_id}")
async def get_conversations(conversation_id: str):
    conversations = list(conversations_collection.find({"conversation_id": conversation_id}))
    return [Conversation(**conv).dict() for conv in conversations]

@app.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str):
    messages = list(messages_collection.find({"conversation_id": conversation_id}))
    return [Message2(**msg).dict() for msg in messages]


async def create_or_update_conversation(platform, customer_id, customer_name, last_message) -> str:
    conversation_id = f"{platform}_{customer_id}"
    conversation = {
        "id": conversation_id,
        "platform": platform,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "last_message": last_message,
        "timestamp": datetime.now(),
        "unread_count": 1,
        "is_ai_enabled": True,  # Default to AI enabled
        "assigned_agent_id": None
    }
    
    conversations_collection.update_one(
        {"id": conversation_id},
        {"$set": conversation},
        upsert=True
    )
    return conversation_id

async def store_message(
    content: str,
    conversation_id: str,
    sender_id: str,
    role: MessageRole,
    platform: str
) -> str:
    message = {
        "id": str(uuid.uuid4()),
        "platform": platform,
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_id,
        "role": role,
        "timestamp": datetime.now(),
        "status": "sent"
    }

    await manager.broadcast({
    "type": "new_message",
    "message": {
        "id": str(uuid.uuid4()),
        "platform": platform,
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_id,
        "role": role,
        "timestamp": datetime.now(),
        "status": "sent"
    }
    })
    
    messages_collection.insert_one(message)
    return message["id"]


# Add WebSocket endpoint
@app.websocket("/ws2/{client_id}")
async def websocket_endpoint(websocket: WebSocket):
    # Retrieve the path parameter from websocket.path_params
    client_id = websocket.path_params["client_id"]
    await manager.connect(client_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            print(f"Received message from {client_id}: {data}")
    except WebSocketDisconnect:
        manager.disconnect(client_id)




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
async def check_live_agent_status(sender_id: str) -> bool:
    """
    Check if a user is currently in live agent mode
    
    Args:
        sender_id (str): The WhatsApp ID of the sender
        
    Returns:
        bool: True if user is in live agent mode, False otherwise
    """
    try:
        # Check support_requests collection for active status
        request = support_requests.find_one({
            "user_id": sender_id,
            "status": {"$in": ["pending", "active"]},  # Check for both pending and active states
            "platform": "whatsapp"
        })
        
        return bool(request)  # Returns True if request exists, False otherwise
        
    except Exception as e:
        print(f"Error checking live agent status: {e}")
        return False

# Update handle_live_agent_request to set the status
async def handle_live_agent_request(sender_id: str, sender_name: str):
    """Handle live agent request by storing user info and chat history in MongoDB"""
    
    # Get chat history from Firestore
    chat_history = get_session_history(sender_id)
    
    # Format messages for MongoDB
    formatted_messages = []
    for msg in chat_history.messages:
        formatted_messages.append({
            "role": msg.type,
            "content": msg.content,
            "timestamp": msg.additional_kwargs.get("timestamp", datetime.now())
        })
    
    # Create support request document
    support_request = {
        "user_id": sender_id,
        "user_name": sender_name,
        "status": "pending",  # pending, active, resolved
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
        "chat_history": formatted_messages,
        "platform": "whatsapp"
    }
    
    try:
        # Check if there's an existing request
        existing_request = support_requests.find_one({
            "user_id": sender_id,
            "status": {"$in": ["pending", "active"]}
        })
        
        if existing_request:
            # Update existing request
            support_requests.update_one(
                {"_id": existing_request["_id"]},
                {
                    "$set": {
                        "status": "pending",
                        "updated_at": datetime.now(),
                        "chat_history": formatted_messages
                    }
                }
            )
        else:
            # Insert new request
            result = support_requests.insert_one(support_request)
            print(f"Support request created with ID: {result.inserted_id}")
        
        return True
    except Exception as e:
        print(f"Error creating support request: {e}")
        return False


# Function to notify connected clients
async def notify_client(client_id: str, message: dict):
    if client_id in connected_clients:
        try:
            await connected_clients[client_id].send_json(message)
        except Exception as e:
            print(f"Error notifying client {client_id}: {e}")
            # Remove dead connections
            if client_id in connected_clients:
                del connected_clients[client_id]


# Function to whatsapp history into Mongo DB
async def store_whatsapp_history(sender_id: str, sender_name: str, sender_message: str, bot_message: str, 
                                cart_data: List, address_data: List, carousel_data: List, message_id: Optional[str] = None):
    """Store WhatsApp chat history in MongoDB"""
    try:
        # Get current time in Indian timezone
        india_tz = timezone('Asia/Kolkata')
        current_time = datetime.now(india_tz)

        # Format messages for chat history, only include non-empty messages
        new_message = []
        
        if sender_message:
            new_message.append({
                "role": "user",
                "content": sender_message,
                "timestamp": current_time,
                "id": None  # User messages don't have IDs
            })
            
        if bot_message:
            new_message.append({
                "role": "assistant",
                "content": bot_message,
                "timestamp": current_time,
                "id": message_id  # Include message ID for admin/bot messages
            })

        # Skip if no messages to store
        if not new_message:
            return True

        # Check if document exists
        existing_doc = chat_history.find_one({"user_id": sender_id})

        if existing_doc:
            # Update existing document
            existing_messages = existing_doc.get('chat_history', [])
            updated_messages = existing_messages + new_message

            # Keep only the last 20 messages
            if len(updated_messages) > 40:
                updated_messages = updated_messages[-40:]

            update_data = {
                "chat_history": updated_messages,
                "last_updated": current_time
            }

            # Update additional data if provided
            if cart_data:
                update_data["cart_data"] = cart_data
            if address_data:
                update_data["address_data"] = address_data
            if carousel_data:
                update_data["carousel_data"] = carousel_data

            chat_history.update_one(
                {"user_id": sender_id},
                {"$set": update_data}
            )
        else:
            # Create new document
            new_doc = {
                "user_id": sender_id,
                "user_name": sender_name,
                "chat_history": new_message,
                "cart_data": cart_data if cart_data else [],
                "address_data": address_data if address_data else [],
                "carousel_data": carousel_data if carousel_data else [],
                "created_at": current_time,
                "last_updated": current_time
            }
            chat_history.insert_one(new_doc)

        print(f"Successfully stored/updated chat history for user: {sender_id} in MongoDB")

        # Notify via WebSocket if client is connected
        if sender_id in connected_clients:
            try:
                # Send WebSocket notifications for non-empty messages
                if sender_message:
                    await connected_clients[sender_id].send_json({
                        "type": "new_message",
                        "message": {
                            "role": "human",
                            "content": sender_message,
                            "timestamp": current_time.isoformat(),
                            "id": None
                        }
                    })
                if bot_message:
                    await connected_clients[sender_id].send_json({
                        "type": "new_message",
                        "message": {
                            "role": "assistant",
                            "content": bot_message,
                            "timestamp": current_time.isoformat(),
                            "id": message_id
                        }
                    })
            except Exception as ws_error:
                print(f"WebSocket send error: {ws_error}")
                # Remove dead connections
                if sender_id in connected_clients:
                    del connected_clients[sender_id]

        return True

    except Exception as e:
        print(f"Error storing chat history: {e}")
        return False

sender_name = ""

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
                    if "read" in message:
                        print("Message read event")
                        continue

                    ig_message_id = message["message"]["mid"]
                    
                    if ig_message_id in processed_message_ids:
                        print("Duplicate message, skipping processing.")
                        continue
                    
                    # Add message ID with the current timestamp
                    processed_message_ids[ig_message_id] = time.time()

                    sender_id = message["sender"]["id"]
                    recipient_id = message["recipient"]["id"]
                    message_text = message["message"]["text"]

                    user_details = await get_ig_username(sender_id)

                    username = user_details.get('username')
                    name = user_details.get('name')

                    user = name + " - @" + username

                    print(f"Message Received from {user}, Message: {message_text}")

                     # Create or update conversation
                    conversation_id = await create_or_update_conversation("instagram", username, name, message_text)
                    
                    
                    # Check if conversation exists
                    existing_conversation = conversations_collection.find_one({"id": conversation_id})
                    
                    conversation = conversations_collection.find_one({"id": conversation_id})

                    
                    message_id = await store_message(
                        content=message_text,
                        conversation_id=conversation_id,
                        sender_id=username,
                        role=MessageRole.CUSTOMER,
                        platform="instagram"
                    )

                    
                    

                    # Broadcast appropriate event based on whether it's new or updated
                    if not existing_conversation:
                        await manager.broadcast({
                            "type": "new_conversation",
                            "conversation": conversation
                        })
                    else:
                        await manager.broadcast({
                            "type": "conversation_updated",
                            "conversation": conversation
                        })

                    await manager.broadcast({
                        "type": "new_message",
                        "platform": "whatsapp",
                        "message": {
                            "sender_id": sender,
                            "content": user_input,
                            "timestamp": datetime.now().isoformat()
                        }
                    })

                    if message_text == "Hi":
                        res = await send_ig_introduction(sender_id)
                        processed_message_ids[res['message_id']] = time.time()
                        print(f"Introduction sent to {sender_id}")
                        continue

                    user_input = f"(client_id: {sender_id}): " + message_text

                    tic = time.perf_counter()
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

                            toc = time.perf_counter()
                            print(f"Response took {toc - tic:0.4f} seconds")    
                            time_taken = round(toc - tic, 4)



                            insert_into_instagram(user, message_text, response['output'], time_taken)

                            processed_message_ids[res2['message_id']] = time.time()
                            search_term = ""
                            print("websocket_product_data_ig 2: ", websocket_product_data_ig) 
                        else:
                            print("Came in else")
                            res = await send_ig_message(sender_id, response['output'])

                            toc = time.perf_counter()
                            print(f"Response took {toc - tic:0.4f} seconds")
                            time_taken = round(toc - tic, 4)

                            insert_into_instagram(user, message_text, response['output'], time_taken)

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
        # Payload is WhatsApp

        await signature_required(request, x_hub_signature_256)

        global sender_name
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

                                # Create or update conversation
                                conversation_id = await create_or_update_conversation("whatsapp", sender, sender_name, user_input)
                                
                                # Check if conversation exists
                                existing_conversation = conversations_collection.find_one({"id": conversation_id})
                                
                                conversation = conversations_collection.find_one({"id": conversation_id})

                                message_id = await store_message(
                                    content=user_input,
                                    conversation_id=conversation_id,
                                    sender_id=sender,
                                    role=MessageRole.CUSTOMER,
                                    platform="whatsapp"
                                )

                                broadcast_new_message(conversation)

                               
                                # Check if user is already in live agent mode
                                is_live = await check_live_agent_status(sender)
                                if is_live:
                                    # Only store the message and notify frontend
                                    await store_whatsapp_history(
                                        sender_id=sender,
                                        sender_name=sender_name,
                                        sender_message=user_input,
                                        bot_message="",
                                        cart_data=[],
                                        address_data=[],
                                        carousel_data=[]
                                    )
                                    return {"status": "ok"}

                                if user_input == "Hi":
                                    await send_introduction(sender, sender_name)
                                    await store_whatsapp_history(sender, sender_name, user_input, f"Hi {sender_name}!👋 I'm *Talkative*, an AI-powered agent for eGenie Stores. I'm here to help you find the perfect tech products for your needs. \n \n✅ *Whether you're looking for:* \n  - A new phone\n  - A powerful gaming laptop\n  - A custom-built PC \n\nI can help you find the *best* options for your budget and preferences.🎉 \n\nFeel free to ask me any questions you may have about our *services* or *policies* , and I'll be happy to assist!📕", [], [], [])
                                    print("Introduction sent to Name: ", sender_name, ", Number: ",sender )
                                    continue
        
                                if ("live agent" in user_input.lower() or
                                "sales representative" in user_input.lower() or
                                "human" in user_input.lower() or
                                "customer support" in user_input.lower()):
                                    success = await handle_live_agent_request(sender, sender_name)
                                    if success:
                                        response = "Thank you for requesting live support. Our team has been notified and will get back to you shortly."
                                    else:
                                        response = "We're sorry, there was an error processing your live support request. Please try again later."
                                    await send_message(sender, response)
                                    customer_details = sender_name + " - " + sender
                                    notify(customer_details, "egeniestore@gmail.com")
                                    await store_whatsapp_history(sender, sender_name, user_input, response, [], [], [])
                                    return {"status": "ok"}


                                is_live = await check_live_agent_status(sender)
                                if is_live:
                                    # Store message and notify frontend only
                                    await store_whatsapp_history(sender, sender_name, user_input, "", [], [], [])
                                    # Broadcast appropriate event based on whether it's new or updated
                                    if not existing_conversation:
                                        await manager.broadcast({
                                            "type": "new_conversation",
                                            "conversation": conversation
                                        })
                                    else:
                                        await manager.broadcast({
                                            "type": "conversation_updated",
                                            "conversation": conversation
                                        })

                                    await manager.broadcast({
                                        "type": "new_message",
                                        "platform": "whatsapp",
                                        "message": {
                                            "id" : message_id,
                                            "conversation_id": conversation_id,
                                            "content": user_input,
                                            "sender_id": sender,
                                            "role" : MessageRole.CUSTOMER,
                                            "timestamp": datetime.now().isoformat(),
                                            "status": "sent"
                                        }
                                    })

                                    return {"status": "ok"}

                                client_id = sender
                                format_user_input = f"(client_id: {client_id}): " + user_input

                                # John Doe - +91 9900990099
                                whatsapp_user = sender_name + " - +" + client_id[:2] + " " + client_id[2:]

                                tic = time.perf_counter()

                                response = agent_executor.invoke(
                                    {"input": format_user_input},
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

                                        toc = time.perf_counter()
                                        print(f"Response took {toc - tic:0.4f} seconds")
                                        time_taken = round(toc - tic, 4)

                                        await store_whatsapp_history(sender, sender_name, user_input, f"Here are some {search_term} that you might be interested in.", [], [], products_space)
                                        insert_into_whatsapp(whatsapp_user, message['text']['body'], response['output'], time_taken)

                                        ai_message_id = await store_message(
                                            content="Here are some {search_term} that you might be interested in.",
                                            conversation_id=conversation_id,
                                            sender_id="AI_BOT",
                                            role=MessageRole.AI,
                                            platform="whatsapp"
                                        )

                                        

                                         # Broadcast appropriate event based on whether it's new or updated
                                        if not existing_conversation:
                                            await manager.broadcast({
                                                "type": "new_conversation",
                                                "conversation": conversation
                                            })
                                        else:
                                            await manager.broadcast({
                                                "type": "conversation_updated",
                                                "conversation": conversation
                                            })

                                        await manager.broadcast({
                                            "type": "new_message",
                                            "platform": "whatsapp",
                                            "message": {
                                                "sender_id": sender,
                                                "content": user_input,
                                                "timestamp": datetime.now().isoformat()
                                            }
                                        })


                                        search_term = ""
                                        print("websocket_product_data 2: ", websocket_product_data) 
                                    else:
                                        print("Came in else")
                                        await send_message(sender, response['output'])

                                        ai_message_id = await store_message(
                                            content=response['output'],
                                            conversation_id=conversation_id,
                                            sender_id="AI_BOT",
                                            role=MessageRole.AI,
                                            platform="whatsapp"
                                        )

                                        

                                         # Broadcast appropriate event based on whether it's new or updated
                                        if not existing_conversation:
                                            await manager.broadcast({
                                                "type": "new_conversation",
                                                "conversation": conversation
                                            })
                                        else:
                                            await manager.broadcast({
                                                "type": "conversation_updated",
                                                "conversation": conversation
                                            })

                                        await manager.broadcast({
                                            "type": "new_message",
                                            "platform": "whatsapp",
                                            "message": {
                                                "sender_id": sender,
                                                "content": user_input,
                                                "timestamp": datetime.now().isoformat()
                                            }
                                        })

                                        toc = time.perf_counter()
                                        print(f"Response took {toc - tic:0.4f} seconds")
                                        time_taken = round(toc - tic, 4)

                                        await store_whatsapp_history(sender, sender_name, user_input, response['output'], [], [], [])
                                        insert_into_whatsapp(whatsapp_user, message['text']['body'], response['output'], time_taken)

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
                                product_name = payload[2]
                                await store_whatsapp_history(sender, sender_name, "Know More", f"*{product_name}* \n\n🎉 *Key Highlights:* \n  - EMI Available! ✅ \n  - Get GST invoice and save up to 28% on business purchases \n  - Free Shipping all over India 🚚\n\nTo contact us, simply type 'Live Agent'", [], [], [payload[0]])
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
                                await store_whatsapp_history(sender, sender_name,"Sender sent cart" , "Thanks for sending your us cart! 📦🤩\n\nBefore we can proceed, we just need a few more details about *your address* so we know exactly where to deliver your order.\n\nLet's get this sorted and your items will be on their way!🚀", product_items,[], [])
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
                                        details = get_sender_details(sender)
                                        await store_whatsapp_history(sender, sender_name, "Sender sent address", "", [], details, [])
                                    elif payload == "info_incorrect":
                                        print("Info is incorrect")
                                        delete_address_details(sender)
                                        await get_details(sender, again=True)

                            # await send_message(sender, "Got your responses. Thank you!\n\nThis functionality is still in development.")

    return {"status": "ok"}


async def get_ig_username(sender_id):
    url = f"https://graph.facebook.com/{VERSION}/{sender_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "username, name",
        "access_token" : IG_ACCESS_TOKEN
    }

    response = requests.get(url, headers=headers, params=params)
    print(f"Response: {response.status_code}, {response.json()}")
    response = response.json()
    return response

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
        print(response)
        if len(response['data']) == 0:
            print("Product not found")
            await send_ig_message(id, "Sorry, we could not find the product details. Please try again.")
            return

        product_cat_id = response['data'][0]['id']

        elements.append({ "id" : product_cat_id })

    print(elements)
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



class Message(BaseModel):
    user_id: str
    message: str


# Define the message model
class ChatMessage(BaseModel):
    user_id: str
    message: str
    message_id: Optional[str] = None

@app.post("/api/chat_history/message")
async def send_chat_message(message: ChatMessage):
    try:
        # Send message to WhatsApp
        await send_message(message.user_id, message.message)
        
        # Store in chat history
        await store_whatsapp_history(
            sender_id=message.user_id,
            sender_name="Admin",
            sender_message="",
            bot_message=message.message,
            message_id=message.message_id,  # Pass the message ID
            cart_data=[],
            address_data=[],
            carousel_data=[]
        )
        
        return {"status": "success", "message": "Message sent successfully"}
    except Exception as e:
        print(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chat_history")
async def get_chat_history():
    try:
        conversations = list(chat_history.find(
            {},
            {'_id': 0}
        ).sort('last_updated', -1).limit(50))
        
        return conversations
    except Exception as e:
        print(f"Error fetching chat history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chat_history/{user_id}")
async def get_user_chat(user_id: str):
    try:
        user_chat = chat_history.find_one(
            {'user_id': user_id},
            {'_id': 0}
        )
        
        if not user_chat:
            raise HTTPException(status_code=404, detail="Chat history not found")
            
        return user_chat
    except Exception as e:
        print(f"Error fetching user chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/chat_history/{user_id}/new")
async def get_new_messages(user_id: str, since: str):
    try:
        # Convert the since parameter to timezone-aware datetime
        since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
        
        # Query MongoDB for new messages
        user_chat = chat_history.find_one(
            {
                'user_id': user_id,
                'chat_history': {
                    '$elemMatch': {
                        'timestamp': {'$gt': since_dt}
                    }
                }
            },
            {'_id': 0, 'chat_history': 1}
        )
        
        if not user_chat:
            return []
            
        # Filter messages after the since timestamp
        new_messages = [
            msg for msg in user_chat['chat_history']
            if msg.get('timestamp') and msg['timestamp'] > since_dt
        ]
        
        return new_messages
    except Exception as e:
        print(f"Error fetching new messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations")
async def get_conversations():
    try:
        conversations = list(support_requests.find(
            {},
            {'_id': 0}
        ).sort('created_at', -1).limit(50))
        
        # Format the response
        return [
            {
                'user_id': conv['user_id'],
                'user_name': conv['user_name'],
                'status': conv['status'],
                'created_at': conv['created_at'],
                'chat_history': conv['chat_history'][-1:],  # Get only last message
                'platform': conv['platform']
            }
            for conv in conversations
        ]
    except Exception as e:
        print(f"Error fetching conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/{user_id}")
async def get_conversation(user_id: str):
    try:
        conversation = support_requests.find_one(
            {'user_id': user_id},
            {'_id': 0}
        )
        
        if not conversation:
            print(f"Conversation not found for user_id: {user_id}")
            raise HTTPException(status_code=404, detail="Conversation not found")
            
        return conversation
    except Exception as e:
        print(f"Error fetching conversation: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/conversations/message")
async def send_message_from_api(message: Message):
    try:
        result = support_requests.update_one(
            {'user_id': message.user_id},
            {'$push': {
                'chat_history': {
                    'role': 'assistant',
                    'content': message.message,
                    'timestamp': datetime.utcnow()
                }
            }}
        )
        # Send message to WhatsApp user
        
        url = f"https://graph.facebook.com/{VERSION}/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": message.user_id,
            "type": "text",
            "text": {"body": message.message}
        }
        
        response = requests.post(url, headers=headers, json=data)
        if not response.ok:
            print(f"Error sending WhatsApp message: {response.text}")
            raise HTTPException(status_code=500, detail="Failed to send WhatsApp message")
        
        if result.modified_count == 0:
            print(f"Conversation not found for user_id: {message.user_id}")
            raise HTTPException(status_code=404, detail="Conversation not found")
            
        return {"success": True}
    except Exception as e:
        print(f"Error sending message: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/products/details")
async def get_product_details(payload: dict):
    try:
        # Validate required fields
        product_id = payload.get("product_retailer_id")
        quantity = payload.get("quantity")
        item_price = payload.get("item_price")
        
        if not all([product_id, quantity, item_price]):
            return {"error": "Missing required fields"}

        # First API call to get product catalog ID
        url1 = f"https://graph.facebook.com/v20.0/759103595212051/products"
        params1 = {
            "filter": json.dumps({
                "retailer_id": {"eq": product_id}
            }),
            "fields": "id",
            "access_token": ACCESS_TOKEN
        }

        response = requests.get(url1, params=params1)
        response.raise_for_status()  # Raise exception for non-200 status codes
        data = response.json()
        
        if not data.get('data') or len(data['data']) == 0:
            return {"error": "Product not found"}

        product_cat_id = data['data'][0]['id']

        # Second API call to get product details
        url = f"https://graph.facebook.com/{product_cat_id}"
        params = {
            "access_token": ACCESS_TOKEN,
            "fields": "name,price,image_url"
        }

        response = requests.get(url, params=params)
        response.raise_for_status()
        product_data = response.json()

        # Clean and format price
        price = product_data.get('price', '0')
        clean_price = int(float(price.replace("₹", "").replace(",", "")))

        product_details = {
            "name": product_data.get('name', ''),
            "amount": {
                "value": int(f"{clean_price}00"),
                "offset": 100
            },
            "sale_amount": {
                "value": int(f"{item_price}00"),
                "offset": 100
            },
            "quantity": int(quantity),
            "country_of_origin": "IN",
            "image": {
                "link": product_data.get('image_url', '')
            }
        }

        return product_details

    except requests.RequestException as e:
        print(f"API request failed: {e}")
        return {"error": "Failed to fetch product details"}
    except (ValueError, KeyError) as e:
        print(f"Data processing error: {e}")
        return {"error": "Invalid data format"}
    except Exception as e:
        print(f"Unexpected error: {e}")
        return {"error": "An unexpected error occurred"}
    
@app.post("/api/create_template")
async def create_template(payload: dict):
    name = payload.get("name")
    language = payload.get("language")
    category = payload.get("category")
    header = payload.get("header")
    body = payload.get("body")
    footer = payload.get("footer")

    url = f"https://graph.facebook.com/{BUSINESS_ACCOUNT_ID}/message_templates"

    headers = {
        "Authorization": "Bearer " + ACCESS_TOKEN,
        "Content-Type": "application/json"
    }

    data = {
        "name": name,
        "language": "en_US",
        "category": category,
        "components": [
            {
             "type": "HEADER",
             "format": "TEXT",
             "text": header,
            },
            {
            "type": "body",
            "text": body,
            },
            
            {
            "type": "footer",
            "text": footer
            }
        ]
    }


    try:
        response = requests.post(url, headers=headers, data=json.dumps(data))
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx and 5xx)
    except requests.exceptions.RequestException as e:
        # Log or handle the error here
        raise HTTPException(status_code=500, detail=str(e))

    return response.json()  # Return the JSON response from the Facebook API

@app.get("/api/fetch_templates")
async def create_template():
    url = f"https://graph.facebook.com/{BUSINESS_ACCOUNT_ID}/message_templates?fields=name,status,category,language"
    headers = {"Authorization": "Bearer " + ACCESS_TOKEN}

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=f"Error fetching templates: {str(e)}")
    except ValueError:
        raise HTTPException(status_code=500, detail="Invalid JSON response from Facebook API")

    return {"status": "success", "data": data}




