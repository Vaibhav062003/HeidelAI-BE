# Built-in modules
import uuid
import requests
import json
from typing import Optional
from datetime import datetime
from bson.objectid import ObjectId

# Internal modules
from core.managers import manager
from schemas.models import MessageRole
from database import get_mongo_db
from core.globals import instagram_connections_map
from config.settings import IG_ID, IG_ACCESS_TOKEN, IG_VERSION

db = get_mongo_db()

async def send_ig_message(id, message_text, instagram_id=None) -> dict:
    """
    Send a message to an Instagram user using the appropriate access token
    
    Args:
        id: The recipient's Instagram user ID
        message_text: The message content to send
        instagram_id: Optional Instagram business account ID to use for sending
                     (if not provided, will use default IG_ID and IG_ACCESS_TOKEN)
    """
    try:
        # If an Instagram business account ID is provided, look up its access token
        if instagram_id:
            # Query the specific Instagram connection to get its access token
            connection = db.instagram_connections.find_one(
                {"instagram_id": instagram_id, "is_active": True},
                {"page_access_token": 1, "page_id": 1},
                sort=[("last_updated", -1)]
            )

            print(f"Retrieved IG Connection: {connection}")
            
            if connection and connection.get("page_access_token"):
                # Use the connection-specific token and page ID
                access_token = connection["page_access_token"]
                page_id = connection.get("page_id", instagram_id)
                print(f"Using Instagram-specific token for {instagram_id}")
            else:
                # Fallback to default token if connection not found
                access_token = IG_ACCESS_TOKEN
                page_id = IG_ID
                print(f"Instagram connection not found for {instagram_id}, using default token")
        else:
            # Use default token if no Instagram ID specified
            access_token = IG_ACCESS_TOKEN
            page_id = IG_ID
            print("Using default Instagram token")
        
        # Build the API URL using the appropriate page_id
        url = f"https://graph.instagram.com/{IG_VERSION}/{instagram_id}/messages"

        headers = {
            "Authorization" : "Bearer " + access_token,
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

        # Send the message
        response = requests.post(url, headers=headers, json=data)
        print(f"Instagram message response: {response.status_code}, {response.json()}")
        
        # Return the response for further processing
        return response.json()
        
    except Exception as e:
        print(f"Error sending Instagram message: {e}")
        # Return an error response that can be handled by the caller
        return {"error": str(e), "success": False}


async def store_instagram_message(
    org_id: str,
    content: str,
    conversation_id: str,
    sender_id: str,
    sender_name: str,
    sender_username: str,
    recipient_id: str,
    role: MessageRole,
    message_id: Optional[str] = None,
) -> str:
    """
    Store a message in the Instagram-specific collection and notify appropriate WebSocket clients
    
    Args:
        org_id: Organization ID for the current context
        content: The message content
        conversation_id: The ID of the conversation
        sender_id: Instagram user ID of the sender
        sender_name: Name of the sender
        sender_username: Username of the sender
        recipient_id: Instagram business account ID (page ID)
        role: Role of the sender (customer, ai, agent)
        message_id: Optional message ID for tracking
        
    Returns:
        str: The stored message ID
    """
    # Create a unique message ID if not provided
    if not message_id:
        message_id = str(uuid.uuid4())
    
    # Get Instagram-specific collections
    conversations_collection = db[f"conversations_{org_id}"]
    messages_collection = db[f"messages_{org_id}"]
    
    # Create message object
    message = {
        "id": message_id,
        "message_id": message_id,  # For compatibility with IG webhook format
        "platform": "instagram",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "role": role,
        "timestamp": datetime.now(),
        "status": "sent"
    }
    
    # Store message in Instagram-specific collection
    messages_collection.insert_one(message)
    
    # Keep only latest 40 messages per conversation
    messages_count = messages_collection.count_documents({"conversation_id": conversation_id})
    if messages_count > 40:
        # Find and delete oldest message
        oldest_message = messages_collection.find_one(
            {"conversation_id": conversation_id},
            sort=[("timestamp", 1)]  # 1 for ascending order
        )
        if oldest_message:
            messages_collection.delete_one({"_id": oldest_message["_id"]})
    
    # Update message counter for the conversation
    conversation = conversations_collection.find_one_and_update(
        {"conversation_id": conversation_id},
        {"$inc": {"message_counter": 1, "unread_count": 1}},
        return_document=True
    )
    
    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "instagram",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_username,  # Use username for the frontend
        "role": role,
        "timestamp": datetime.now().isoformat(),
        "status": "sent",
        "sender_name": sender_name
    }
    
    # Broadcast to Instagram-specific WebSocket connections
    if recipient_id in instagram_connections_map:
        await broadcast_instagram_message(recipient_id, "new_message", {
            "message": broadcast_message
        })
    
    # Check if counter >= 10 for conversational analytics
    if conversation and conversation.get("message_counter", 0) >= 10:
        # Perform analytics tasks
        # await analyze_conversation_messages(conversation_id)
        # await update_product_recommendations(conversation_id)
        
        # Reset the counter to 0
        conversations_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"message_counter": 0}}
        )
    
    return message_id


async def create_or_update_instagram_conversation(org_id: str, recipient_id, sender_id, customer_name, username, last_message) -> str:
    """
    Create or update a conversation in the Instagram-specific conversations collection
    
    Args:
        org_id: Organization ID for the current context
        recipient_id: Instagram business account ID (page ID)
        sender_id: Instagram user ID who sent the message
        customer_name: Name of the customer
        username: Instagram username of the customer
        last_message: The content of the last message
        
    Returns:
        str: The conversation ID
    """
    conversation_id = f"instagram_{username}"
    
    # Get the Instagram-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]
    
    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    if existing_conversation:
        # Update existing conversation without modifying is_ai_enabled
        ig_conversation = {
            "id": conversation_id,
            "platform": "instagram",
            "conversation_id": conversation_id,
            "instagram_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "customer_username": username,
            "last_message": last_message,
            "timestamp": datetime.now(),
            "last_message_timestamp": datetime.now(),
            # NOTE: Preserve existing values for fields below
            # "unread_count" and "is_ai_enabled" will keep their existing values
        }
    else:
        # Create new conversation with is_ai_enabled set to True
        ig_conversation = {
            "id": conversation_id,
            "platform": "instagram",
            "conversation_id": conversation_id,
            "instagram_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "customer_username": username,
            "last_message": last_message,
            "timestamp": datetime.now(),
            "last_message_timestamp": datetime.now(),
            "unread_count": 1,
            "is_ai_enabled": True  # Default for new conversations
        }
    
    # Use $set to update only specified fields
    conversations_collection.update_one(
        {"conversation_id": conversation_id},
        {"$set": ig_conversation},
        upsert=True
    )
    
    # Get the updated conversation for broadcasting
    updated_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    # Broadcast to Instagram-specific connections
    if recipient_id in instagram_connections_map:
        # Format the timestamp for JSON serialization
        broadcast_conversation = dict(updated_conversation)
        if "last_message_timestamp" in broadcast_conversation:
            broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
        if "_id" in broadcast_conversation:
            broadcast_conversation["_id"] = str(broadcast_conversation["_id"])
            
        await broadcast_instagram_message(recipient_id, "conversation_updated", {
            "conversation": broadcast_conversation
        })
        
    return conversation_id

# Add a new broadcast method for Instagram notifications
async def broadcast_instagram_message(instagram_id: str, message_type: str, data: dict):
    """
    Broadcast messages specifically to Instagram connections
    
    Args:
        instagram_id: The Instagram business account ID
        message_type: Type of message (new_message, conversation_updated, new_conversation)
        data: The message data to broadcast
    """
    # First, check if we have any connections for this Instagram ID
    if instagram_id in instagram_connections_map and instagram_connections_map[instagram_id]:
        disconnected_clients = []
        
        # Convert any datetime objects in the data to ISO format strings
        serializable_data = json.loads(json.dumps(data, default=lambda obj: obj.isoformat() if isinstance(obj, datetime) else str(obj) if isinstance(obj, ObjectId) else None))
        
        # Send to all Instagram-specific connections
        for connection_key in instagram_connections_map[instagram_id]:
            try:
                if connection_key in manager.active_connections:
                    await manager.active_connections[connection_key].send_json({
                        "type": message_type,
                        "platform": "instagram",
                        **serializable_data
                    })
                    print(f"Instagram notification sent to {connection_key}: {message_type}")
            except Exception as e:
                print(f"Error notifying Instagram connection {connection_key}: {e}")
                disconnected_clients.append(connection_key)
        
        # Clean up disconnected clients
        for client_id in disconnected_clients:
            if client_id in manager.active_connections:
                del manager.active_connections[client_id]
            if instagram_id in instagram_connections_map:
                instagram_connections_map[instagram_id].discard(client_id)
                if not instagram_connections_map[instagram_id]:
                    del instagram_connections_map[instagram_id]


async def get_ig_username(sender_id, page_access_token):
    if page_access_token == None:
        page_access_token = IG_ACCESS_TOKEN
    url = f"https://graph.instagram.com/{sender_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "username, name",
        "access_token" : page_access_token
    }

    response = requests.get(url, headers=headers, params=params)
    print(f"Response: {response.status_code}, {response.json()}")
    response = response.json()
    return response
