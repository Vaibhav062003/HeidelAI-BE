# wa_service.py

# Built-in modules
import json, requests
from typing import Optional
from datetime import datetime
from bson.objectid import ObjectId

# Internal modules
from core.managers import manager
from schemas.models import MessageRole
from database import get_mongo_db
from core.services import services
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations

db = get_mongo_db()

async def create_or_update_whatsapp_conversation(org_id: str, recipient_id, sender_id, customer_name, last_message) -> str:
    """
    Create or update a conversation in the WhatsApp-specific conversations collection
    
    Args:
        org_id: Organization ID for the current context
        recipient_id: WhatsApp business account ID
        sender_id: WhatsApp user phone number who sent the message
        customer_name: Name of the customer
        last_message: The content of the last message
        
    Returns:
        str: The conversation ID
    """
    conversation_id = f"whatsapp_{sender_id}"
    
    # Get the WhatsApp-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]
    
    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    if existing_conversation:
        # Update existing conversation without modifying is_ai_enabled
        wa_conversation = {
            "id": conversation_id,
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "whatsapp_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "last_message": last_message,
            "timestamp": datetime.now(),
            "last_message_timestamp": datetime.now(),
            # NOTE: Preserve existing values for fields below
            # "unread_count" and "is_ai_enabled" will keep their existing values
        }
    else:
        # Create new conversation with is_ai_enabled set to True
        wa_conversation = {
            "id": conversation_id,
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "whatsapp_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "last_message": last_message,
            "timestamp": datetime.now(),
            "last_message_timestamp": datetime.now(),
            "unread_count": 1,
            "is_ai_enabled": True  # Default for new conversations
        }
    
    # Use $set to update only specified fields
    conversations_collection.update_one(
        {"conversation_id": conversation_id},
        {"$set": wa_conversation},
        upsert=True
    )
    
    # Get the updated conversation for broadcasting
    updated_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    # Broadcast to WhatsApp-specific connections
    if recipient_id in services.whatsapp_connections_map:
        # Format the timestamp for JSON serialization
        broadcast_conversation = dict(updated_conversation)
        if "last_message_timestamp" in broadcast_conversation:
            broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
        if "_id" in broadcast_conversation:
            broadcast_conversation["_id"] = str(broadcast_conversation["_id"])
            
        await broadcast_whatsapp_message(recipient_id, "conversation_updated", {
            "conversation": broadcast_conversation
        })
        
    return conversation_id

async def store_whatsapp_message(
    org_id: str, 
    content: str,
    conversation_id: str,
    sender_id: str,
    sender_name: str,
    recipient_id: str,
    role: MessageRole,
    message_id: Optional[str] = None,
    message_type: Optional[str] = "text",
    raw_data: Optional[dict] = None
) -> str:
    """
    Store a message in the WhatsApp-specific collection and notify appropriate WebSocket clients
    
    Args:
        org_id: Organization ID for the current context
        content: The message content
        conversation_id: The ID of the conversation
        sender_id: WhatsApp user phone number of the sender
        sender_name: Name of the sender
        recipient_id: WhatsApp business account ID
        role: Role of the sender (customer, ai, agent)
        message_id: Optional message ID for tracking
        message_type: Type of the message (text, image, etc.)
        raw_data: Raw message data for reference
        
    Returns:
        str: The stored message ID
    """
    # Get WhatsApp-specific collections
    conversations_collection = db[f"conversations_{org_id}"]
    messages_collection = db[f"messages_{org_id}"]
    
    # Create message object
    message = {
        "id": message_id,
        "message_id": message_id,
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "role": role,
        "timestamp": datetime.now(),
        "status": "received",
        "message_type": message_type,
        "raw_data": raw_data  # Store the raw message for reference
    }
    
    # Store message in WhatsApp-specific collection
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
    
    # Update message counter and unread count for the conversation
    conversation = conversations_collection.find_one_and_update(
        {"conversation_id": conversation_id},
        {"$inc": {"message_counter": 1, "unread_count": 1}},
        return_document=True
    )
    
    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": sender_id,
        "role": role,
        "timestamp": datetime.now().isoformat(),
        "status": "received",
        "sender_name": sender_name,
        "message_type": message_type
    }
    
    # Broadcast to WhatsApp-specific WebSocket connections
    if recipient_id in services.whatsapp_connections_map:
        await broadcast_whatsapp_message(recipient_id, "new_message", {
            "message": broadcast_message
        })
    
    # Check if counter >= 10 for conversational analytics
    if conversation and conversation.get("message_counter", 0) >= 10:
        # Perform analytics tasks
        try:
            # await analyze_conversation_messages(conversation_id)
            # await update_product_recommendations(conversation_id)
            
            # Reset the counter to 0
            conversations_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {"message_counter": 0}}
            )
        except Exception as e:
            print(f"Error during WhatsApp conversation analysis: {e}")
    
    return message_id

# Function to broadcast to WhatsApp-specific connections
async def broadcast_whatsapp_message(whatsapp_id: str, message_type: str, data: dict):
    """
    Broadcast messages specifically to WhatsApp connections
    
    Args:
        whatsapp_id: The WhatsApp business account ID
        message_type: Type of message (new_message, conversation_updated, etc.)
        data: The message data to broadcast
    """
    # First, check if we have any connections for this WhatsApp ID
    if whatsapp_id in services.whatsapp_connections_map and services.whatsapp_connections_map[whatsapp_id]:
        disconnected_clients = []
        
        # Convert any datetime objects in the data to ISO format strings
        serializable_data = json.loads(json.dumps(data, default=lambda obj: obj.isoformat() if isinstance(obj, datetime) else str(obj) if isinstance(obj, ObjectId) else None))
        
        # Send to all WhatsApp-specific connections
        for connection_key in services.whatsapp_connections_map[whatsapp_id]:
            try:
                if connection_key in manager.active_connections:
                    await manager.active_connections[connection_key].send_json({
                        "type": message_type,
                        "platform": "whatsapp",
                        **serializable_data
                    })
                    print(f"WhatsApp notification sent to {connection_key}: {message_type}")
            except Exception as e:
                print(f"Error notifying WhatsApp connection {connection_key}: {e}")
                disconnected_clients.append(connection_key)
        
        # Clean up disconnected clients
        for client_id in disconnected_clients:
            if client_id in manager.active_connections:
                del manager.active_connections[client_id]
            if whatsapp_id in services.whatsapp_connections_map:
                services.whatsapp_connections_map[whatsapp_id].discard(client_id)
                if not services.whatsapp_connections_map[whatsapp_id]:
                    del services.whatsapp_connections_map[whatsapp_id]

