# Built-in modules
import uuid
import requests
import json
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson.objectid import ObjectId
from services.cloudinary_service import upload_media_to_cloudinary
from services.websocket_service import broadcast_main_ws
from supabase import create_client, Client
from pymongo import UpdateOne, ReturnDocument
from fastapi import HTTPException

# Internal modules
from core.managers import manager
from schemas.models import MessageRole
from database import get_mongo_db, get_mongo_client
from core.services import services
from services.scheduler_service import abort_scheduled_message
from services.conversation_utils import reopen_conversation
from config.settings import (
    IG_ID, 
    IG_ACCESS_TOKEN, 
    IG_VERSION,
    SUPABASE_URL, 
    SUPABASE_KEY, 
    SUPABASE_INSTA_DM_TABLE,  
    IG_ACCESS_TOKEN
)
from core.logger import get_logger

logger = get_logger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

db = get_mongo_db()

mongo_client = get_mongo_client()

async def send_ig_message(id, message_text, instagram_id, mode, org_id) -> dict:
    """
    Send a message to an Instagram user using the appropriate access token
    
    Args:
        id: The recipient's Instagram user ID
        message_text: The message content to send
        instagram_id: Instagram business account ID to use for sending
    """
    try:
        # If an Instagram business account ID is provided, look up its access token
        if instagram_id:
            # Query the specific Instagram connection to get its access token
            connection = db.instagram_connections.find_one(
                {"instagram_id": instagram_id, "is_active": True},
                {"page_access_token": 1},
                sort=[("last_updated", -1)]
            )

            logger.success(f"Retrieved IG Connection: {connection}")
            
            if connection and connection.get("page_access_token"):
                # Use the connection-specific token and page ID
                access_token = connection["page_access_token"]
                # page_id = connection.get("page_id", instagram_id)
                logger.info(f"Using Instagram-specific token for {instagram_id}")
            else:
                # Fallback to default token if connection not found
                logger.error(f"No Instagram ID specified, cannot send message")
                raise HTTPException(status_code=400, detail="Instagram ID not specified")
                # return 1
                # access_token = IG_ACCESS_TOKEN
                # page_id = IG_ID
                # print(f"Instagram connection not found for {instagram_id}, using default token")
        else:
            # Use default token if no Instagram ID specified
            logger.error(f"No Instagram ID specified, cannot send message")
            return 1
            # access_token = IG_ACCESS_TOKEN
            # page_id = IG_ID
            # print("Using default Instagram token")
        
        if mode == "reply":
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
            logger.info(f"Instagram message response: {response.status_code}, {response.json()}")
            
            # Return the response for further processing
            sent_message_id = response.json().get("message_id")
            return response.json()
        else:
            message_id = f"privatenote_{uuid.uuid4()}"

            user_details = await get_ig_username(id, access_token)
            username = user_details.get('username')
            name = user_details.get('name', username)

            # Create or update conversation
            conversation_id = await create_or_update_instagram_conversation(
                org_id=org_id,
                recipient_id=instagram_id,
                sender_id=id,
                customer_name=name,
                username=username,
                last_message=f"Private Note: {message_text}",
                last_sender=MessageRole.AGENT,
                mode=mode
            )
            
            # Store in Instagram-specific collections
            await store_instagram_message(
                org_id=org_id,
                content=message_text,
                type="text",
                payload=None,
                conversation_id=conversation_id,
                sender_id=id,
                sender_name="Agent",
                sender_username="agent_username",
                recipient_id=instagram_id,
                role= MessageRole.AGENT,
                message_id=message_id,  # Use the ID from the webhook for deduplication
                mode=mode
            )

            return {"message_id": message_id, "success": True, "status": "sent"}
        
    except Exception as e:
        logger.error(f"Error sending Instagram message: {e}")
        # Return an error response that can be handled by the caller
        return {"error": str(e), "success": False}
    
async def send_ig_media_message(recipient, media_url, media_type, instagram_id, mode, org_id) -> dict:
    """
    Send a media message to an Instagram user using the appropriate access token
    
    Args:
        recipient: The recipient's Instagram user ID
        media_url: The URL of the media to send
        instagram_id: Instagram business account ID to use for sending
    """
    try:
        connection = db.instagram_connections.find_one(
            {"instagram_id": instagram_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )

        logger.success(f"Retrieved IG Connection: {connection}")
        
        if connection and connection.get("page_access_token"):
            # Use the connection-specific token
            access_token = connection["page_access_token"]
            logger.info(f"Using Instagram-specific token for {instagram_id}")
        else:
            # Fallback to default token if connection not found
            access_token = IG_ACCESS_TOKEN
            logger.error(f"Instagram connection not found for {instagram_id}, using default token")

        if mode == "reply":
            # Query the specific Instagram connection to get its access token
        
            url = f"https://graph.instagram.com/{IG_VERSION}/{instagram_id}/messages"

            headers = {
                "Authorization" : "Bearer " + access_token,
                "Content-Type" : "application/json",
            }

            data = {
                "recipient":{
                    "id":f"{recipient}"
                },
                "message":{
                    "attachment":{
                        "type":media_type,
                        "payload":{
                            "url":media_url
                        }
                    }
                }
            }

            # Send the media message
            response = requests.post(url, headers=headers, json=data)
            logger.info(f"Instagram media message response: {response.status_code}, {response.json()}")
            
            # Return the response for further processing
            return response.json()
        else:
            message_id = f"privatenote_{uuid.uuid4()}"

            if media_type == "image":
                content = "Shared an image"
            elif media_type == "video":
                content = "Shared a video"

            user_details = await get_ig_username(recipient, access_token)
            username = user_details.get('username')
            name = user_details.get('name', username)

            # Create or update conversation
            conversation_id = await create_or_update_instagram_conversation(
                org_id=org_id,
                recipient_id=instagram_id,
                sender_id=recipient,
                customer_name=name,
                username=username,
                last_message=f"Private Note: {content}",
                last_sender=MessageRole.AGENT,
                mode=mode
            )
            
            # Store in Instagram-specific collections
            await store_instagram_message(
                org_id=org_id,
                content=content,
                type=media_type,
                payload={"url": media_url},
                conversation_id=conversation_id,
                sender_id=recipient,
                sender_name="Agent",
                sender_username="agent_username",
                recipient_id=instagram_id,
                role= MessageRole.AGENT,
                message_id=message_id,  # Use the ID from the webhook for deduplication
                mode=mode
            )
        
    except Exception as e:
        logger.error(f"Error sending Instagram media message: {e}")
        # Return an error response that can be handled by the caller
        return {"error": str(e), "success": False}

async def upload_and_send_ig_media_message(recipient, file, instagram_id, org_id, mode) -> dict:
    """
    Upload media to Cloudinary and send it as an Instagram message
    
    Args:
        recipient: The recipient's Instagram user ID
        file: The media file to upload and send
        instagram_id: Instagram business account ID to use for sending
        org_id: Organization ID for context
    """
    try:
        # Upload media to Cloudinary
        result = await upload_media_to_cloudinary(file, platform="instagram", org_id=org_id)

        file_url = result.get("file_url")
        file_type = result.get("file_type")
        
        if not file_url:
            logger.error("Media upload failed, cannot send Instagram media message.")
            return {"error": "Media upload failed", "success": False}
        
        # Send the media message via Instagram API
        response = await send_ig_media_message(recipient, file_url, file_type, instagram_id, mode, org_id)
        
        if response.get("message_id"):
            logger.success("Instagram media message sent successfully.")

            return {"success": True, "response": response}


        return {"error": "Sending media message failed", "success": False}
        
    except Exception as e:
        logger.error(f"Error in upload_and_send_ig_media_message: {e}")
        return {"error": str(e), "success": False}

async def store_instagram_message(
    org_id: str,
    content: str,
    conversation_id: str,
    sender_id: str,
    type: Optional[str],
    payload: Optional[dict],
    sender_name: str,
    sender_username: str,
    recipient_id: str,
    role: MessageRole,
    mode: Optional[str] = None,
    message_id: Optional[str] = None,
    context_type: Optional[str] = None,
    context: Optional[dict] = None
    ) -> str:
    """
    Store a message in the Instagram-specific collection and notify appropriate WebSocket clients
    
    Args:
        org_id: Organization ID for the current context
        content: The message content
        conversation_id: The ID of the conversation
        sender_id: Instagram user ID of the sender
        type: Type of the message (share -> post, ig_reel, ig_story)
        payload: Additional payload for the message (only exists if type is provided, contains attachment info)
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
    conversation_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversation_collection_name]
    messages_collection = db[f"messages_{org_id}"]
    
    # Create message object
    message = {
        "id": message_id,
        "message_id": message_id, 
        "platform": "instagram",
        "conversation_id": conversation_id,
        "content": content,
        "type": type,
        "payload": payload,
        "context_type": context_type,
        "context": context,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "role": role,
        "timestamp": datetime.now(),
        "status": "sent",
        "mode": mode
    }
    
    # Store message in Instagram-specific collection
    messages_collection.insert_one(message)

    logger.success(f"Instagram message stored: {message_id}")
    
    # Keep only latest 500 messages per conversation
    messages_count = messages_collection.count_documents({"conversation_id": conversation_id})
    if messages_count > 500:
        # Find and delete oldest message
        oldest_message = messages_collection.find_one(
            {"conversation_id": conversation_id},
            sort=[("timestamp", 1)]  # 1 for ascending order
        )
        if oldest_message:
            messages_collection.delete_one({"_id": oldest_message["_id"]})
    

    # # Update message counter for the conversation
    # conversation = conversations_collection.find_one_and_update(
    #     {"conversation_id": conversation_id},
    #     {"$inc": {"message_counter": 1, "unread_count": 1}},
    #     return_document=True
    # )

    logger.success(f"Instagram conversation updated: {conversation_id}")

    if mode == "private":
        return

    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "instagram",
        "conversation_id": conversation_id,
        "content": content,
        "type": type,
        "payload": payload,
        "context_type": context_type,
        "context": context,
        "sender_id": sender_username,  # Use username for the frontend
        "role": role,
        "timestamp": datetime.now().isoformat(),
        "status": "sent",
        "sender_name": sender_name,
        "mode": mode
    }
    
    # Broadcast to /main-ws clients
    await broadcast_main_ws(
        platform_id=recipient_id,
        platform_type="instagram",
        event_type="new_message",
        payload={"message": broadcast_message}
    )
    
    # Broadcast to Instagram-specific WebSocket connections
    if recipient_id in services.instagram_connections_map:
        logger.info(f"Broadcasting message to Instagram user {recipient_id}")
        await broadcast_instagram_message(recipient_id, "new_message", {
            "message": broadcast_message
        })
    
    # Check if counter >= 10 for conversational analytics
    # if conversation and conversation.get("message_counter", 0) >= 10:
    #     # Perform analytics tasks
    #     # await analyze_conversation_messages(conversation_id)
    #     # await update_product_recommendations(conversation_id)
        
    #     # Reset the counter to 0
    #     conversations_collection.update_one(
    #         {"conversation_id": conversation_id},
    #         {"$set": {"message_counter": 0}}
    #     )
    
    return message_id

async def create_or_update_instagram_conversation(org_id: str, 
                                                  recipient_id: str, 
                                                  sender_id: str, 
                                                  customer_name: str, 
                                                  username: str, 
                                                  last_message: str, 
                                                  last_sender: str,
                                                  mode: Optional[str] = None,
                                                  is_auto_followup: bool = False) -> str:
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

    now = datetime.now(timezone.utc)
    
    # Get the org-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]

    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    convo_key = (org_id, conversation_id)

    is_being_viewed = len(services.convo_viewers.get(convo_key, set())) > 0

    if existing_conversation:
        
        abort_result = await abort_scheduled_message(org_id, conversation_id)
        logger.success(f"Aborted Scheduled message: {abort_result}")

        reopen_result = await reopen_conversation(org_id, conversation_id)
        logger.success(f"Reopened conversation: {reopen_result}")

        # Update existing conversation without modifying is_ai_enabled
        ig_conversation = {
            "platform": "instagram",
            "conversation_id": conversation_id,
            "instagram_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "customer_username": username,
            "last_message": last_message,
            "last_message_timestamp": now,
            "last_sender": last_sender,
            "status": "open",
            "message_counter": existing_conversation.get("message_counter", 0) + 1,
            "unread_count": existing_conversation.get("unread_count", 0) + (0 if is_being_viewed else 1),
            "last_message_is_private_note": mode == "private"

        }

        if last_sender == "customer":
            # Reset inactivity reminders on customer reply
            ig_conversation["reminder_stage"] = 0
            ig_conversation["next_action_at"] = None

        elif last_sender == "agent" and not is_auto_followup:
            # Manual agent message resets the inactivity countdown
            ig_conversation["reminder_stage"] = 0
            ig_conversation["next_action_at"] = now + timedelta(hours=6)

    else:
        # Create new conversation with is_ai_enabled set to True
        ig_conversation = {
            "platform": "instagram",
            "conversation_id": conversation_id,
            "instagram_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "customer_username": username,
            "last_message": last_message,
            "last_message_timestamp": now,
            "unread_count": 1,
            "is_ai_enabled": False,
            "status": "open",
            "last_sender": last_sender,
            "started_at": now,
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "closed_at": None,
            "last_message_is_private_note": mode == "private"
        }

    
    if last_sender == "customer":
        ig_conversation["reply_window_ends_at"] = now + timedelta(hours=24)
 
    # Use $set to update only specified fields
    conversations_collection.update_one(
        {"conversation_id": conversation_id},
        {"$set": ig_conversation},
        upsert=True
    )

    logger.success(f"Conversation updated: {ig_conversation}")
    
    # Get the updated conversation for broadcasting
    updated_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    # Format the timestamp for JSON serialization
    broadcast_conversation = dict(updated_conversation)
    if "last_message_timestamp" in broadcast_conversation:
        broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
    if broadcast_conversation["next_action_at"] is not None:
        broadcast_conversation["next_action_at"] = broadcast_conversation["next_action_at"].isoformat()
    if "started_at" in broadcast_conversation:
        broadcast_conversation["started_at"] = broadcast_conversation["started_at"].isoformat()
    if broadcast_conversation.get("reply_window_ends_at"):
        broadcast_conversation["reply_window_ends_at"] = broadcast_conversation["reply_window_ends_at"].isoformat()
    if "_id" in broadcast_conversation:
        broadcast_conversation["_id"] = str(broadcast_conversation["_id"])

    await broadcast_main_ws(
        platform_id=recipient_id,
        platform_type="instagram",
        event_type="conversation_updated",
        payload={"conversation": broadcast_conversation}
    )

    # Broadcast to Instagram-specific connections
    if recipient_id in services.instagram_connections_map:
            
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
    if instagram_id in services.instagram_connections_map and services.instagram_connections_map[instagram_id]:
        disconnected_clients = []
        
        # Convert any datetime objects in the data to ISO format strings
        serializable_data = json.loads(json.dumps(data, default=lambda obj: obj.isoformat() if isinstance(obj, datetime) else str(obj) if isinstance(obj, ObjectId) else None))
        
        # Send to all Instagram-specific connections
        for connection_key in services.instagram_connections_map[instagram_id]:
            try:
                if connection_key in manager.active_connections:
                    await manager.active_connections[connection_key].send_json({
                        "type": message_type,
                        "platform": "instagram",
                        **serializable_data
                    })
                    logger.success(f"Instagram notification sent to {connection_key}: {message_type}")
            except Exception as e:
                logger.error(f"Error notifying Instagram connection {connection_key}: {e}")
                disconnected_clients.append(connection_key)
        
        # Clean up disconnected clients
        for client_id in disconnected_clients:
            if client_id in manager.active_connections:
                del manager.active_connections[client_id]
            if instagram_id in services.instagram_connections_map:
                services.instagram_connections_map[instagram_id].discard(client_id)
                if not services.instagram_connections_map[instagram_id]:
                    del services.instagram_connections_map[instagram_id]

async def get_ig_username(sender_id, page_access_token):
    if page_access_token == None:
        page_access_token = IG_ACCESS_TOKEN
    url = f"https://graph.instagram.com/{sender_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "username, name, profile_pic",
        "access_token" : page_access_token
    }

    response = requests.get(url, headers=headers, params=params)
    logger.info(f"Response: {response.status_code}, {response.json()}")
    response = response.json()
    return response

async def get_ig_media_owner(media_id, page_access_token):
    url = f"https://graph.instagram.com/{media_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "username, owner",
        "access_token" : page_access_token
    }

    response = requests.get(url, headers=headers, params=params)
    response = response.json()
    if 'username' in response:
        owner_username = response['username']
    if 'owner' in response:
        owner_id = response['owner']['id']
        profile_picture_url = await get_ig_profile_picture(owner_id, page_access_token)
        return {
            "username": owner_username,
            "profile_picture_url": profile_picture_url
        }
    return {
        "username": None,
        "profile_picture_url": None
    }

async def get_ig_profile_picture(user_id, page_access_token):
    url = f"https://graph.instagram.com/{user_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "profile_picture_url",
        "access_token" : page_access_token
    }

    response = requests.get(url, headers=headers, params=params)
    response = response.json()
    if 'profile_picture_url' in response:
        return response['profile_picture_url']
    return None

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
            logger.warning("No data found in Supabase table.")
            return {}
    except Exception as e:
        logger.error(f"Error fetching data from Supabase: {e}")
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

        logger.success(f"Retrieved IG Connection: {connection}")
        
        if connection and connection.get("page_access_token"):
            # Use the connection-specific token
            access_token = connection["page_access_token"]
            logger.info(f"Using Instagram-specific token for {ig_account_id}")
        else:
            # Fallback to default token if connection not found
            access_token = IG_ACCESS_TOKEN
            logger.error(f"Instagram connection not found for {ig_account_id}, using default token")
    url = f"https://graph.facebook.com/v21.0/{comment_id}/replies"
    payload= {
      "message": message,
      "access_token": access_token
    }

    response = requests.post(url, data=payload)
    logger.info(f"Comment reply response: {response.json()}")

async def send_private_reply(comment_id,ig_account_id, message_text=None, link_url=None, button_title="View Offer"):
    """Send private message to user who commented"""
    if ig_account_id:
        # Query the specific Instagram connection to get its access token
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )

        logger.success(f"Retrieved IG Connection: {connection}")
        
        if connection and connection.get("page_access_token"):
            # Use the connection-specific token
            access_token = connection["page_access_token"]
            logger.info(f"Using Instagram-specific token for {ig_account_id}")
        else:
            # Fallback to default token if connection not found
            access_token = IG_ACCESS_TOKEN
            logger.error(f"Instagram connection not found for {ig_account_id}, using default token")
    
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
        logger.info(f"Sending button message with link: {link_url}")
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
        logger.info("Sending simple text message (no valid link provided)")

    try:
        response = requests.post(url, json=payload)
        logger.info(f"Private message response: {response.status_code}")
        
        if response.status_code == 200:
            logger.success("Message sent successfully!")
            return True
        else:
            logger.error(f"Error sending message: {response.json()}")
            return False
            
    except Exception as e:
        logger.error(f"Exception while sending message: {e}")
        return False

async def process_comments_events(value, ig_account_id): 
    comment_id = value.get('id')
    text = value.get('text', '').lower()
    from_user = value.get('from', {}).get('username', 'unknown_user')
    media_id = value.get('media', {}).get('id')
    user_id = value.get('from', {}).get('id')

    logger.info(f"new comment by @{from_user}: {text} on media_id {media_id}")

    if not media_id or not user_id:
        logger.error("Missing media_id or user_id.")
        return

    media_info = media_data.get(str(media_id))

    if media_info and media_info['keyword'].lower() in text:
        # Initialize the set if media_id not seen before
        if str(media_id) not in services.responded_users_by_media:
            services.responded_users_by_media[str(media_id)] = set()

        if user_id not in services.responded_users_by_media[str(media_id)]:
            logger.success(f"Keyword match found: '{media_info['keyword']}' in comment text. First time responding.")
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
            logger.info(f"Already responded to user_id {user_id} for media_id {media_id}. Skipping.")
    else:
        logger.warning("No matching keyword or media not tracked.")

# === Disconnect and Archive helpers for Instagram ===

async def revoke_instagram_access(org_id: str, user_id: str):
    """
    Revoke the Instagram access token through Meta App for an organization
    """

    try:
        connection = db.instagram_connections.find_one(
            {"instagram_id": user_id, "is_active": True, "org_id": org_id}
        )

        if not connection: 
            logger.error("No active instagram connection found")
            return {"message": "Instagram connection not found"}
        
        access_token = connection.get("page_access_token")
        if access_token:
            try:
                response = requests.delete(
                    f"https://graph.instagram.com/{user_id}/permissions",
                    params={"access_token": access_token}
                )
                logger.info(f"Token revocation response: {response.status_code} - {response.text}")
            except Exception as revoke_error:
                logger.error(f"Error revoking Instagram token: {revoke_error}")

    except Exception as e:
        logger.error(f"Error in revoke_instagram_access: {str(e)}")

async def archive_instagram_data(org_id: str, instagram_id: str, expire_days=30):
    """
    Archive messages & conversations for a specific org and Instagram connection.
    If a previous active archive exists for the same instagram_id, reuse and extend it.
    """
    try:
        now = datetime.now(timezone.utc)

        with mongo_client.start_session() as session:
            with session.start_transaction():

                archive_id = None

                existing_archive = db["archived_tracker"].find_one({
                        "org_id": org_id,
                        "platform": "instagram",
                        "instagram_id": instagram_id,
                        "status": "active",
                    },
                    session=session
                )

                if existing_archive:
                    archive_id = existing_archive["archive_id"]
                    logger.info(f"Found existing archive {archive_id} for instagram_id {instagram_id}, extending expiration")

                    db.archived_tracker.update_one(
                        {"archive_id": archive_id},
                        {"$set": {"expires_at": now + timedelta(days=expire_days)}},
                        session=session
                    )
                else:

                    archive_id = uuid.uuid4().hex
                    logger.info(f"Creating new archive {archive_id} for instagram_id {instagram_id}")

                    db.archived_tracker.insert_one({
                            "archive_id": archive_id,
                            "org_id": org_id,
                            "platform": "instagram",
                            "instagram_id": instagram_id,
                            "created_at": now,
                            "expires_at": now + timedelta(days=expire_days),
                            "status": "active",
                        }, 
                        session=session
                    )

                messages_collection_name = f"messages_{org_id}"
                conversations_collection_name = f"conversations_{org_id}"
                private_notes_collection_name = f"private_notes_{org_id}"
                
                archived_messages = f"archived_messages_{org_id}_{archive_id}"
                archived_conversations = f"archived_conversations_{org_id}_{archive_id}"
                archived_private_notes = f"archived_private_notes_{org_id}_{archive_id}"

                # Archiving messages collection
                messages_to_archive = list(
                    db[messages_collection_name].find(
                            {"platform": "instagram"}, 
                            {"_id": 0},
                            session=session
                        )
                    )
                
                if messages_to_archive:
                    db[archived_messages].insert_many(messages_to_archive, session=session)
                    db[messages_collection_name].delete_many({"platform": "instagram"}, session=session)

                # Archiving conversations collection
                conversations_to_archive = list(
                    db[conversations_collection_name].find(
                            {"platform": "instagram", "instagram_id": instagram_id}, 
                            {"_id": 0},
                            session=session
                        )
                    )
                
                if conversations_to_archive:
                    bulk_ops = []
                    for convo in conversations_to_archive:
                        bulk_ops.append(
                            UpdateOne(
                                {"conversation_id": convo["conversation_id"]},  
                                {"$set": convo},
                                upsert=True
                            )
                        )

                    if bulk_ops:
                        db[archived_conversations].bulk_write(bulk_ops, session=session)
                        
                    db[conversations_collection_name].delete_many({"platform": "instagram", "instagram_id": instagram_id}, session=session)
                
                # Archiving private notes collection
                private_notes_to_archive = list(
                    db[private_notes_collection_name].find(
                        {"platform": "instagram", "user_id": instagram_id},
                        {"_id": 0},
                        session=session
                    )
                )

                if private_notes_to_archive:
                    db[archived_private_notes].insert_many(private_notes_to_archive, session=session)
                    db[private_notes_collection_name].delete_many({"platform": "instagram", "user_id": instagram_id}, session=session)

                logger.success(f"Archived Instagram data for org {org_id} with archive_id {archive_id}")

    except Exception as e:
         logger.error(f"Error archiving Instagram data for org {org_id} with archive_id {archive_id}: {str(e)}")

async def remove_instagram_connection(org_id: str, instagram_id: str):
    """
    Delete Instagram connection and clear the org's instagram_id field
    """

    try:
        delete_result = db.instagram_connections.delete_many(
            {"instagram_id": instagram_id, "org_id": org_id}
            )
        logger.success(f"Deleted {delete_result.deleted_count} Instagram connections for org {org_id}")

        org_update_result = db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"ig_id": None}}
        )

        if org_update_result.modified_count:
            logger.success(f"Cleared ig_id for organization {org_id}")
        else:
            logger.warning(f"No organization updated for org_id {org_id}")

        return {
            "deleted_connections": delete_result.deleted_count,
            "organization_updated": org_update_result.modified_count > 0
        }
    
    except Exception as e:
        logger.error(f"Error removing Instagram connection: {str(e)}")
        raise

async def handle_reaction_messages():
    pass

async def send_instagram_reaction(org_id, message_id, emoji):
    """Send a reaction to an Instagram message"""
    try: 

        messages_collection = db[f"messages_{org_id}"]

        # Find the message first
        message = messages_collection.find_one({"message_id": message_id})
        recipient_id = message.get("sender_id")

        if not message:
            logger.error("Message not found")
            return {"status": "error", "message": "Message not found"}
        
        org_document = db.organizations.find_one({"org_id": org_id})
        instagram_id = org_document.get("ig_id")

        if not instagram_id:
            raise HTTPException(status_code=400, detail="Instagram ID not configured for this organization")

        # Get the page access token for this Instagram account
        connection = db.instagram_connections.find_one(
            {"instagram_id": instagram_id, "is_active": True, "org_id": org_id},
            {"page_access_token": 1,"access_token" : 1, "page_id": 1}
        )

        if not connection or not connection.get("page_access_token"):
            raise HTTPException(status_code=404, detail="Instagram connection not found or inactive")
        
        page_access_token = connection["page_access_token"]

        url = f"https://graph.instagram.com/me"
        params = {
            "access_token": page_access_token,
            "fields": "name,profile_picture_url,username"
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"Instagram API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch Instagram profile: {response.json().get('error', {}).get('message')}"
            )
        
        owner_details = response.json()

        # If reaction exists and same user reacted before → remove it
        if message.get("reaction"):
            current_reaction = message["reaction"]
            if current_reaction.get("username") == owner_details.get("username") and current_reaction.get("emoji") == "❤️":
                response = await send_message_reaction(message_id, instagram_id, page_access_token, action="unreact", recipient_id=recipient_id)

                if response == "error":
                    return {"status": "error", "message": "Failed to remove reaction IG API Exception"}
                # Unset the reaction
                broadcast_message = messages_collection.find_one_and_update(
                    {"message_id": message_id},
                    {"$unset": {"reaction": 1}}, 
                    return_document=ReturnDocument.AFTER
                )
                logger.success(f"Reaction removed for message {message_id}")

                if "_id" in broadcast_message:
                    broadcast_message["_id"] = str(broadcast_message["_id"])

                await broadcast_main_ws(
                    platform_id=instagram_id,
                    platform_type="instagram",
                    event_type="message_reaction",
                    payload={"message": broadcast_message}
                )

                # Broadcast the reaction update to connected clients
                if instagram_id in services.instagram_connections_map:
                    logger.info(f"Broadcasting message_reaction event to Frontend for {instagram_id}")
                    await broadcast_instagram_message(instagram_id, "message_reaction", {
                        "message": broadcast_message
                    })

                logger.success(f"Reaction removed for message {message_id} with emoji ❤️")


                return {"status": "success", "message": "sent"}

        response = await send_message_reaction(message_id, instagram_id, page_access_token, action="react", recipient_id=recipient_id)

        if response == "error":
            return {"status": "error", "message": "Failed to remove reaction IG API Exception"}

        # Otherwise set a new reaction
        broadcast_message = messages_collection.find_one_and_update(
            {"message_id": message_id},
            {"$set": {
                "reaction": {
                    "emoji": "❤️",
                    "username": owner_details.get("username"),
                    "full_name": owner_details.get("name"),
                    "profile_picture_url": owner_details.get("profile_picture_url"),
                    "timestamp": datetime.now()
                }
            }}, 
            return_document=ReturnDocument.AFTER
        )

        if "_id" in broadcast_message:
            broadcast_message["_id"] = str(broadcast_message["_id"])

        await broadcast_main_ws(
            platform_id=instagram_id,
            platform_type="instagram",
            event_type="message_reaction",
            payload={"message": broadcast_message}
        )

        # Broadcast the reaction update to connected clients
        if instagram_id in services.instagram_connections_map:
            logger.info(f"Broadcasting message_reaction event to Frontend for {instagram_id}")
            await broadcast_instagram_message(instagram_id, "message_reaction", {
                "message": broadcast_message
            })

        logger.success(f"Reaction updated for message {message_id} with emoji ❤️")
        return {"status": "success", "message": "sent"}
        
    
    except Exception as e:
        logger.error(f"Error sending Instagram reaction: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send reaction: {str(e)}"
        )

async def send_message_reaction(message_id: str, instagram_id: str, page_access_token: str, action: str, recipient_id: str):
    """
    Send a reaction to a message
    """
    try:
        url = f"https://graph.instagram.com/v24.0/{instagram_id}/messages"

        headers = {
            "Authorization" : "Bearer " + page_access_token,
        }

        body = {
            "recipient":{
                "id": recipient_id
            },
            "sender_action": action,
            "payload": {
                "message_id": message_id,
                "reaction": "love"
            }
        }

        response = requests.post(url, headers=headers, json=body)
        if response.status_code != 200:
            logger.error(f"Error sending message reaction: {response.text}")
            return "error"
        logger.info(f"Message reaction response: {response.status_code}, {response.json()}")
        return "success"

    except Exception as e:
        logger.error(f"Error sending message reaction: {str(e)}")
        return "error"