# Built-in modules
import json, requests
from typing import Optional
from datetime import datetime
from bson.objectid import ObjectId
from supabase import create_client, Client

# Internal modules
from core.managers import manager
from schemas.models import MessageRole
from database import get_mongo_db
from core.services import services
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations
from config.settings import (
    SUPABASE_URL, 
    SUPABASE_KEY, 
    SUPABASE_INSTA_DM_TABLE,  
    IG_ACCESS_TOKEN
)

db = get_mongo_db()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

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

# Fetch data from Supabase
def fetch_media_data():
    try:
        response = supabase.table(SUPABASE_INSTA_DM_TABLE).select('*').execute()
        if response.data:
            return {
                str(item['post_id']): {
                    'keyword': item['keyword'],
                    'reply': item['reply'],
                    'link': item.get('link'),
                    'button_title': item.get('button_title', 'View Offer')  # Default fallback
                }
                for item in response.data
            }
        else:
            print("No data found in Supabase table.")
            return {}
    except Exception as e:
        print(f"Error fetching data from Supabase: {e}")
        return {}

media_data = fetch_media_data()

async def reply_to_comment(comment_id, message, ig_account_id):
    if ig_account_id:
        # Query the specific Instagram connection to get its access token
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )

        print(f"Retrieved IG Connection: {connection}")
        
        if connection and connection.get("page_access_token"):
            # Use the connection-specific token
            access_token = connection["page_access_token"]
            print(f"Using Instagram-specific token for {ig_account_id}")
        else:
            # Fallback to default token if connection not found
            access_token = IG_ACCESS_TOKEN
            print(f"Instagram connection not found for {ig_account_id}, using default token")

    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    payload= {
      "message": message,
      "access_token": access_token
    }

    response = requests.post(url, data=payload)
    print("Comment reply response:", response.json())

async def send_private_reply(comment_id,ig_account_id, message_text=None, link_url=None, button_title="View Offer"):
    """Send private message to user who commented"""
    if ig_account_id:
        # Query the specific Instagram connection to get its access token
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )

        print(f"Retrieved IG Connection: {connection}")
        
        if connection and connection.get("page_access_token"):
            # Use the connection-specific token
            access_token = connection["page_access_token"]
            print(f"Using Instagram-specific token for {ig_account_id}")
        else:
            # Fallback to default token if connection not found
            access_token = IG_ACCESS_TOKEN
            print(f"Instagram connection not found for {ig_account_id}, using default token")
    
    url = f"https://graph.instagram.com/v23.0/{ig_account_id}/messages"

    # Validate and clean the link_url
    has_valid_link = (
        link_url is not None and 
        isinstance(link_url, str) and 
        link_url.strip() and 
        (link_url.startswith('http://') or link_url.startswith('https://'))
    )

    if has_valid_link:
        # Ensure button_title is valid
        if not button_title or not isinstance(button_title, str):
            button_title = "View Link"  # Default fallback
        
        # Trim button title if too long (Instagram has limits)
        button_title = button_title[:20] if len(button_title) > 20 else button_title
        
        payload = {
            "recipient": {
                "comment_id": comment_id
            },
            "message": {
                "attachment": {
                    "type": "template",
                    "payload": {
                        "template_type": "button",
                        "text": message_text,
                        "buttons": [
                            {  
                                "type": "web_url",
                                "url": link_url.strip(),
                                "title": button_title.strip()
                            }
                        ]
                    }
                }
            },
            "access_token": access_token
        }
        print(f"Sending button message with link: {link_url}")
    else:
        # Send simple text message when no valid link is provided
        payload = {
            "recipient": { 
                "comment_id": comment_id
            },
            "message": { 
                "text": message_text
            },
            "access_token": access_token
        }
        print("Sending simple text message (no valid link provided)")

    try:
        response = requests.post(url, json=payload)
        print(f"Private message response: {response.status_code}")
        
        if response.status_code == 200:
            print("Message sent successfully!")
            return True
        else:
            print(f"Error sending message: {response.json()}")
            return False
            
    except Exception as e:
        print(f"Exception while sending message: {e}")
        return False

async def process_comments_events(value, ig_account_id): 
    comment_id = value.get('id')
    text = value.get('text', '').lower()
    from_user = value.get('from', {}).get('username', 'unknown_user')
    media_id = value.get('media', {}).get('id')
    user_id = value.get('from', {}).get('id')

    print(f"new comment by @{from_user}: {text} on media_id {media_id}")

    if not media_id or not user_id:
        print("Missing media_id or user_id.")
        return

    media_info = media_data.get(str(media_id))

    if media_info and media_info['keyword'].lower() in text:
        # Initialize the set if media_id not seen before
        if str(media_id) not in services.responded_users_by_media:
            services.responded_users_by_media[str(media_id)] = set()

        if user_id not in services.responded_users_by_media[str(media_id)]:
            print(f"Keyword match found: '{media_info['keyword']}' in comment text. First time responding.")
            reply_message = media_info['reply']
            link_url = media_info.get('link')
            button_title = media_info.get('button_title', 'View Offer')

            await reply_to_comment(comment_id,reply_message, ig_account_id)

            await send_private_reply(
                comment_id=comment_id,
                ig_account_id=ig_account_id,
                message_text=reply_message,
                link_url=link_url,
                button_title=button_title
            )

            services.responded_users_by_media[str(media_id)].add(user_id)
        else:
            print(f"Already responded to user_id {user_id} for media_id {media_id}. Skipping.")
    else:
        print("No matching keyword or media not tracked.")
