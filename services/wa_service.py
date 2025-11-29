# Built-in modules
import json, requests
import uuid
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson.objectid import ObjectId
from fastapi import HTTPException

# Internal modules
from core.managers import manager
from database import get_mongo_db
from core.services import services
from config.settings import IG_VERSION
from schemas.models import MessageRole
from services.conversation_utils import reopen_conversation
from services.scheduler_service import abort_scheduled_message
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations

db = get_mongo_db()

async def create_or_update_whatsapp_conversation(org_id: str, 
                                                 recipient_id, 
                                                 customer_phone_no, 
                                                 customer_name, 
                                                 last_message, 
                                                 last_sender,
                                                 is_auto_followup: bool = False) -> str:
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
    conversation_id = f"whatsapp_{customer_phone_no}"
    now = datetime.now(timezone.utc)
    
    # Get the WhatsApp-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]
    
    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    if existing_conversation:

        abort_result = await abort_scheduled_message(org_id, conversation_id)
        print("Aborted Scheduled message: ", abort_result)

        reopen_result = await reopen_conversation(org_id, conversation_id)
        print("Reopened conversation: ", reopen_result)

        # Update existing conversation without modifying is_ai_enabled
        wa_conversation = {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "whatsapp_id": recipient_id,
            "customer_id": customer_phone_no,
            "customer_name": customer_name,
            "last_message": last_message,
            "last_message_timestamp": now,
            "last_sender": last_sender,
            # NOTE: Preserve existing values for fields below
            # "unread_count" and "is_ai_enabled" will keep their existing values
        }

        
        if last_sender == "customer":
            # Reset inactivity reminders on customer reply
            wa_conversation["reminder_stage"] = 0
            wa_conversation["next_action_at"] = None

        elif last_sender == "agent" and not is_auto_followup:
            # Manual agent message resets the inactivity countdown
            wa_conversation["reminder_stage"] = 0
            wa_conversation["next_action_at"] = now + timedelta(hours=6)

    else:
        # Create new conversation with is_ai_enabled set to True
        wa_conversation = {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "whatsapp_id": recipient_id,
            "customer_id": customer_phone_no,
            "customer_name": customer_name,
            "last_message": last_message,
            "last_message_timestamp": now,
            "unread_count": 1,
            "is_ai_enabled": False,  # Default for new conversations
            "status": "open",
            "last_sender": last_sender,
            "started_at": now,
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "closed_at": None
        }
    
    # Use $set to update only specified fields
    conversations_collection.update_one(
        {"conversation_id": conversation_id},
        {"$set": wa_conversation},
        upsert=True
    )

    print("Whatsapp conversation updated:", wa_conversation)
    
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
            
        # await broadcast_whatsapp_message(recipient_id, "conversation_updated", {
        #     "conversation": broadcast_conversation
        # })
        
    return conversation_id

async def store_whatsapp_message(
    org_id: str, 
    content: str,
    conversation_id: str,
    customer_phone_no: str,
    type: Optional[str],
    payload: Optional[dict],
    sender_name: str,
    recipient_id: str,
    role: MessageRole,
    message_id: Optional[str] = None,
    raw_data: Optional[dict] = None
) -> str:
    """
    Store a message in the WhatsApp-specific collection and notify appropriate WebSocket clients
    
    Args:
        org_id: Organization ID for the current context
        content: The message content
        conversation_id: The ID of the conversation
        customer_phone_no: WhatsApp phone number of the sender
        type: Type of the message (text, image, etc.)
        payload: Additional payload data for the message
        sender_name: Name of the sender
        recipient_id: WhatsApp business account ID
        role: Role of the sender (customer, ai, agent)
        message_id: Optional message ID for tracking
        raw_data: Raw message data for reference
        
    Returns:
        str: The stored message ID
    """
    if not message_id:
        message_id = str(uuid.uuid4())

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
        "type": type,
        "payload": payload,
        "sender_id": customer_phone_no,
        "sender_name": sender_name,
        "role": role,
        "timestamp": datetime.now(timezone.utc),
        "status": "received",
        "raw_data": raw_data  # Store the raw message for reference
    }   
    
    # Store message in WhatsApp-specific collection
    messages_collection.insert_one(message)

    print("Whatsapp message stored:", message_id)
    
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

    print("Whatsapp conversation updated:", conversation_id)
    
    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": customer_phone_no,
        "role": role,
        "timestamp": datetime.now().isoformat(),
        "status": "received",
        "sender_name": sender_name,
        "type": type,
        "payload": payload
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

async def send_whatsapp_message(message: dict, org_id: str) -> dict:
    """
    Send a message to an Whatsapp user using the appropriate access token
    
    Args:
        Sender wa_id: The recipient's WhatsApp user ID
        message: {
            "conversation_id": str,
            "content": str,
            "sender_id": str,
        }
    """
    whatsapp_id = message.get("sender_id")

    wa_connection = db.whatsapp_connections.find_one(
        {"wa_id": whatsapp_id},
        {"wa_id": 1, "_id": 0}
    )
    
    whatsapp_business_id = wa_connection["wa_id"]
    
    try:
        conversation_id = message["conversation_id"]
        content = message["content"]
        
        # Find the WhatsApp connection to get the access token
        connection = db.whatsapp_connections.find_one({"wa_id": whatsapp_business_id})
        
        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")
        
        # Extract the recipient phone number from conversation ID
        recipient_id = conversation_id.replace("whatsapp_", "")
        
        # Send message via WhatsApp Cloud API
        url = f"https://graph.facebook.com/{IG_VERSION}/{connection['phone_id']}/messages"
        headers = {
            "Authorization": f"Bearer {connection['access_token']}",
            "Content-Type": "application/json",
        }
        data = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "text",
            "text": {"body": content}
        }
        
        response = requests.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            print(f"WhatsApp API error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to send WhatsApp message"
            )
        
        response_data = response.json()
        message_id = response_data.get("messages", [{}])[0].get("id")
        
        now = datetime.now(timezone.utc)

        wa_conversation = {
            "last_message": f"You: {content}",
            "last_message_timestamp": now,
            "last_sender": "agent",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6)
        }

        db[f"conversations_{org_id}"].update_one(
            {"conversation_id": conversation_id},
            {"$set": wa_conversation}
        )

        # Store message in WhatsApp-specific collection
        mid = await store_whatsapp_message(
            org_id=org_id,
            content=content,
            conversation_id=conversation_id,
            customer_phone_no=recipient_id,
            type="text",
            payload=None,
            sender_name="Agent",
            recipient_id=whatsapp_business_id,
            role=MessageRole.AGENT,
            message_id=message_id,
            raw_data=response_data
        )
        
        return {"message": "Message sent", "message_id": mid}
    
    except HTTPException as e:
        raise e
    except Exception as e:
        print(f"Error sending WhatsApp message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send message: {str(e)}"
        )