# Built-in modules
import httpx
import json, requests
import uuid
import io
import cloudinary.uploader
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson.objectid import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument, UpdateOne

# Internal modules
from core.managers import manager
from database import get_mongo_db, get_mongo_client
from core.services import services
from config.settings import IG_VERSION, VERSION
from schemas.models import MessageRole
from services.cloudinary_service import upload_media_to_cloudinary
from services.conversation_utils import reopen_conversation
from services.scheduler_service import abort_scheduled_message
from services.websocket_service import broadcast_main_ws
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()
mongo_client = get_mongo_client()

async def create_or_update_whatsapp_conversation(
    org_id: str, 
    recipient_id, 
    customer_phone_no, 
    customer_name, 
    last_message, 
    last_sender,
    is_auto_followup: bool = False,
    mode: Optional[str] = None
) -> str:
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

    convo_key = (org_id, conversation_id)
    
    # Check if the viewers set exists and is not empty
    is_being_viewed = len(services.convo_viewers.get(convo_key, set())) > 0
    
    if existing_conversation:

        abort_result = await abort_scheduled_message(org_id, conversation_id)
        logger.success(f"Aborted Scheduled message: {abort_result}")

        reopen_result = await reopen_conversation(org_id, conversation_id)
        logger.success(f"Reopened conversation: {reopen_result}")

        # Update existing conversation without modifying is_ai_enabled
        wa_conversation = {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "whatsapp_id": recipient_id,
            "customer_id": customer_phone_no,
            "customer_name": customer_name,
            "last_message": last_message,
            "last_message_timestamp": now,
            "last_message_is_private_note": mode == "private",
            "last_sender": last_sender,
            "status": "open",
            "message_counter": existing_conversation.get("message_counter", 0) + 1,
            "unread_count": existing_conversation.get("unread_count", 0) + (0 if is_being_viewed else 1),
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
            "last_message_is_private_note": mode == "private",
            "unread_count": 1,
            "message_counter": 1,
            "is_ai_enabled": False,  
            "status": "open",
            "last_sender": last_sender,
            "started_at": now,
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "closed_at": None
        }

    if last_sender == "customer":
        wa_conversation["reply_window_ends_at"] = now + timedelta(hours=24)
    
    updated_conversation = conversations_collection.find_one_and_update(
        {"conversation_id": conversation_id},
        {"$set": wa_conversation},
        upsert=True,                          # Create if it doesn't exist
        return_document=ReturnDocument.AFTER  # Return the NEW/UPDATED document
    )

    logger.success(f"Whatsapp conversation updated: {updated_conversation}")

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
        platform_type="whatsapp",
        event_type="conversation_updated",
        payload={"conversation": broadcast_conversation}
    )
    
    # Broadcast to WhatsApp-specific connections
    if recipient_id in services.whatsapp_connections_map:
        await broadcast_whatsapp_message(recipient_id, "conversation_updated", {
            "conversation": broadcast_conversation
        })
        
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
    mode: Optional[str] = None,
    message_id: Optional[str] = None,
    raw_data: Optional[dict] = None,
    status: Optional[str] = "received",
    temp_id: Optional[str] = None,
    context_type: Optional[str] = None,
    context: Optional[dict] = None
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
        "context_type": context_type,
        "context": context,
        "sender_id": customer_phone_no,
        "sender_name": sender_name,
        "role": role,
        "timestamp": datetime.now(timezone.utc),
        "status": status,
        "temp_id": temp_id,
        "raw_data": raw_data,
        "mode": mode
    }   
    
    # Store message in WhatsApp-specific collection
    messages_collection.insert_one(message)

    logger.success(f"Whatsapp message stored: {message_id}")
    
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

    # Update message counter and unread count for the conversation
    # conversation = conversations_collection.find_one_and_update(
    #     {"conversation_id": conversation_id},
    #     {"$inc": {"message_counter": 1, "unread_count": 1}},
    #     return_document=True
    # )

    logger.success(f"Whatsapp conversation updated: {conversation_id}")
    
    if status == "sending":
        return
    
    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": customer_phone_no,
        "role": role,
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "sender_name": sender_name,
        "type": type,
        "temp_id": temp_id,
        "payload": payload,
        "mode": mode,
        "context_type": context_type,
        "context": context
    }

    await broadcast_main_ws(
        platform_id=recipient_id,
        platform_type="whatsapp",
        event_type="new_message",
        payload={"message": broadcast_message}
    )
    
    # Broadcast to WhatsApp-specific WebSocket connections
    if recipient_id in services.whatsapp_connections_map:
        await broadcast_whatsapp_message(recipient_id, "new_message", {
            "message": broadcast_message
        })


    # Check if counter >= 10 for conversational analytics
    # if conversation and conversation.get("message_counter", 0) >= 10:
    #     # Perform analytics tasks
    #     try:
    #         # await analyze_conversation_messages(conversation_id)
    #         # await update_product_recommendations(conversation_id)
            
    #         # Reset the counter to 0
    #         conversations_collection.update_one(
    #             {"conversation_id": conversation_id},
    #             {"$set": {"message_counter": 0}}
    #         )
    #     except Exception as e:
    #         print(f"Error during WhatsApp conversation analysis: {e}")
    
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
                    logger.success(f"WhatsApp notification sent to {connection_key}: {message_type}")
            except Exception as e:
                logger.error(f"Error notifying WhatsApp connection {connection_key}: {e}")
                disconnected_clients.append(connection_key)
        
        # Clean up disconnected clients
        for client_id in disconnected_clients:
            if client_id in manager.active_connections:
                del manager.active_connections[client_id]
            if whatsapp_id in services.whatsapp_connections_map:
                services.whatsapp_connections_map[whatsapp_id].discard(client_id)
                if not services.whatsapp_connections_map[whatsapp_id]:
                    del services.whatsapp_connections_map[whatsapp_id]

async def send_whatsapp_message(message: dict, org_id: str, mode:str, temp_id: Optional[str] = None, context_type: Optional[str] = None, context: Optional[dict] = None) -> dict:
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

        data = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "text",
            "text": {"body": content}
        }

        if context_type and context:
            data["context"] = {
                "message_id": context.get("mid")
            }
        
        if mode == "reply":
            
            # Send message via WhatsApp Cloud API
            url = f"https://graph.facebook.com/{IG_VERSION}/{connection['phone_id']}/messages"
            headers = {
                "Authorization": f"Bearer {connection['access_token']}",
                "Content-Type": "application/json",
            }
            
            
            response = requests.post(url, headers=headers, json=data)
            
            if response.status_code != 200:
                logger.error(f"WhatsApp API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to send WhatsApp message"
                )
            
            response_data = response.json()
            message_id = response_data.get("messages", [{}])[0].get("id")
        else: 
            message_id = f"privatenote_{uuid.uuid4()}"
            response_data = {"status": "Private note created, no API call made"}
        
        now = datetime.now(timezone.utc)

        wa_conversation = {
            "last_message": f"Private Note: {content}" if mode == "private" else f"You: {content}",
            "last_message_timestamp": now,
            "last_sender": "agent",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "last_message_is_private_note": mode == "private"
        }

        conversation = db[f"conversations_{org_id}"].find_one_and_update(
            {"conversation_id": conversation_id},
            {"$set": wa_conversation},
            return_document=ReturnDocument.AFTER  
        )

        if "_id" in conversation:
            conversation["_id"] = str(conversation["_id"])

        await broadcast_main_ws(
            platform_id=connection.get("wa_id"),
            platform_type="whatsapp",
            event_type="conversation_updated",
            payload={"conversation": conversation}
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
            mode=mode,
            message_id=message_id,
            raw_data=response_data,
            status="delivered" if mode == "private" else "sending",
            temp_id=temp_id,
            context=context,
            context_type=context_type
        )
        
        return {"message": "Message sent", "message_id": mid}
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send message: {str(e)}"
        )

async def send_wa_media_message(recipient_id: str, media_url: str, media_type: str, wa_id: str, content: Optional[str], org_id: str, temp_id: Optional[str], mode: str, context_type: Optional[str], context: Optional[dict]) -> dict:
    """
    Send a media message via WhatsApp Cloud API
    """
    try:
        # Find the WhatsApp connection to get the access token
        connection = db.whatsapp_connections.find_one({"wa_id": wa_id})

        media_payload = {
            "link": media_url,
        }
        if content:
            media_payload["caption"] = content

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": media_type,
            media_type: media_payload
        }
        if context_type and context:
            data["context"] = {
                "message_id": context.get("mid")
            }
        
        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")

        if mode == "reply":
            # Send media message via WhatsApp Cloud API
            url = f"https://graph.facebook.com/{IG_VERSION}/{connection.get('phone_id')}/messages"
            headers = {
                "Authorization": f"Bearer {connection.get('access_token')}",
                "Content-Type": "application/json",
            }
            
            
            response = requests.post(url, headers=headers, json=data)
            
            if response.status_code != 200:
                logger.error(f"WhatsApp API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to send WhatsApp media message"
                )
            
            response_data = response.json()
            logger.info(f"WhatsApp media message response: {response.status_code}, {response_data}")

            message_id = response_data.get("messages", [{}])[0].get("id")
        else:
            message_id = f"privatenote_{uuid.uuid4()}"
            response_data = {"status": "Private note created, no API call made"}

        now = datetime.now(timezone.utc)

        wa_conversation = {
            "last_message": f"Private Note: {content if content else 'Shared an image'}" if mode == "private" else f"You: {content if content else 'Shared an image'}",
            "last_message_timestamp": now,
            "last_sender": "agent",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "last_message_is_private_note": mode == "private"
        }

        conversation = db[f"conversations_{org_id}"].find_one_and_update(
            {"conversation_id": f"whatsapp_{recipient_id}"},
            {"$set": wa_conversation},
            return_document=ReturnDocument.AFTER  
        )

        if "_id" in conversation:
            conversation["_id"] = str(conversation["_id"])

        await broadcast_main_ws(
            platform_id=connection.get("wa_id"),
            platform_type="whatsapp",
            event_type="conversation_updated",
            payload={"conversation": conversation}
        )

        mid = await store_whatsapp_message(
            org_id=org_id,
            content=content,
            conversation_id=f"whatsapp_{recipient_id}",
            customer_phone_no=recipient_id,
            type=media_type,
            payload={"url": media_url, "caption": content} if content else {"url": media_url},
            sender_name="Agent",
            recipient_id=recipient_id,
            role=MessageRole.AGENT,
            mode=mode,
            message_id=message_id,
            raw_data=response_data,
            status="delivered" if mode == "private" else "sending",
            temp_id=temp_id,
            context=context,
            context_type=context_type
        )
        
        return {"message": "Sending message", "message_id": mid}
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error sending WhatsApp media message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send media message: {str(e)}"
        )

async def upload_and_send_wa_media_message(recipient_id: str, file: any, wa_id: str, org_id: str, content: Optional[str], temp_id: Optional[str], mode: str, context_type: Optional[str], context: Optional[dict]) -> dict:
    """
    Upload media to Cloudinary and send it as a media message
    """
    try:
        # Upload media to Cloudinary
        result = await upload_media_to_cloudinary(file, platform="whatsapp", org_id=org_id)

        media_url = result.get("file_url")
        media_type = result.get("file_type")

        if not media_url:
            logger.error("Media upload failed, cannot send WhatsApp media message.")
            return {"error": "Media upload failed", "success": False}
        
        # Send the media message via WhatsApp API
        response = await send_wa_media_message(recipient_id, media_url, media_type, wa_id, content, org_id, temp_id, mode, context_type, context)
        
        return response
    except Exception as e:
        logger.error(f"Error in upload_and_send_wa_media_message: {e}")
        return {"error": str(e), "success": False}
 
async def update_whatsapp_message_status(org_id: str, message_id: str, status: str):
    """
    Update the status of a WhatsApp message in the database
    """
    try:
        messages_collection = db[f"messages_{org_id}"]
        
        result = messages_collection.find_one_and_update(
            {"message_id": message_id, "platform": "whatsapp"},
            {"$set": {"status": status}},
            return_document=ReturnDocument.AFTER
        )
        
        if result:
            logger.success(f"WhatsApp message {message_id} status updated to {status}")
            return result
        return None
    
    except Exception as e:
        logger.error(f"Error updating WhatsApp message status: {e}")

async def archive_whatsapp_data(org_id: str, wa_id: str, expire_days=30):
    """
    Archives Whatsapp converstions, messages and private notes for a specific connection of an org.
    If a previous active archive exists for the same wa_id, reuse and extend it.
    """
    try:
        now = datetime.now(timezone.utc)

        with mongo_client.start_session() as session:
            with session.start_transaction():

                archived_id = None

                existing_archive = db["archived_tracker"].find_one({
                        "org_id": org_id,
                        "platform": "whatsapp",
                        "whatsapp_id": wa_id,
                        "status": "active",
                    },
                    session=session
                )

                if existing_archive:
                    archived_id = existing_archive["archive_id"]
                    logger.success(f"Found existing archive {archived_id} for whatsapp_id {wa_id}, extending expiration")

                    new_expiry = now + timedelta(days=expire_days)

                    db.archived_tracker.update_one(
                        {"archive_id": archived_id},
                        {"$set": {"expires_at": new_expiry}},
                        session=session
                    )
                else:
                    archive_id = uuid.uuid4().hex
                    logger.success(f"Creating new archive {archive_id} for whatsapp_id {wa_id}")

                    db.archived_tracker.insert_one({
                            "archive_id": archive_id,
                            "org_id": org_id,
                            "platform": "whatsapp",
                            "whatsapp_id": wa_id,
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
                            {"platform": "whatsapp"}, 
                            {"_id": 0},
                            session=session
                        )
                    )
                
                if messages_to_archive:
                    db[archived_messages].insert_many(messages_to_archive, session=session)
                    db[messages_collection_name].delete_many({"platform": "whatsapp"}, session=session)

                # Archiving conversations collection
                conversations_to_archive = list(
                    db[conversations_collection_name].find(
                            {"platform": "whatsapp", "whatsapp_id": wa_id}, 
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
                        
                    db[conversations_collection_name].delete_many({"platform": "whatsapp", "whatsapp_id": wa_id}, session=session)
                
                # Archiving private notes collection
                private_notes_to_archive = list(
                    db[private_notes_collection_name].find(
                        {"platform": "whatsapp", "user_id": wa_id},
                        {"_id": 0},
                        session=session
                    )
                )

                if private_notes_to_archive:
                    db[archived_private_notes].insert_many(private_notes_to_archive, session=session)
                    db[private_notes_collection_name].delete_many({"platform": "whatsapp", "user_id": wa_id}, session=session)

                logger.success(f"Archived WhatsApp data for org {org_id} with archive_id {archive_id}")

    except Exception as e:
         logger.error(f"Error archiving WhatsApp data for org {org_id} with archive_id {archive_id}: {str(e)}")

async def revoke_whatsapp_access(org_id: str, wa_id: str):
    """
    Revoke whatsapp access token through Meta App for an organization
    """
    try:
        connection = db.whatsapp_connections.find_one({"wa_id": wa_id, "is_active": True, "org_id": org_id})

        if not connection:
            logger.error(f"No active WhatsApp connection found for org {org_id} and wa_id {wa_id}")
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")
        
        access_token = connection.get("access_token")
        if access_token:
            try:
                # Call Facebook's token revocation endpoint
                revoke_url = "https://graph.facebook.com/v17.0/me/permissions"
                params = {"access_token": access_token}
                requests.delete(revoke_url, params=params)
            except Exception as e:
                logger.error(f"Error revoking token: {e}")

        return {"message": "WhatsApp connection removed successfully"}
        
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Error disconnecting WhatsApp: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect WhatsApp: {str(e)}")
    
async def remove_whatsapp_connection(org_id: str, wa_id: str):
    """
    Remove WhatsApp connection record from the organizations collection and whatsapp_connections collection
    """
    try:
        delete_result = db.whatsapp_connections.delete_many(
            {"wa_id": wa_id, "org_id": org_id}
            )
        logger.success(f"Deleted {delete_result.deleted_count} whatsapp connections for org {org_id}")

        org_update_result = db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"wa_id": None}}
        )

        if org_update_result.modified_count:
            logger.success(f"Cleared wa_id for organization {org_id}")
        else:
            logger.info(f"No organization updated for org_id {org_id}")

        return {
            "deleted_connections": delete_result.deleted_count,
            "organization_updated": org_update_result.modified_count > 0
        }
    
    except Exception as e:
        logger.error(f"Error removing WhatsApp connection: {str(e)}")
        raise

async def upload_whatsapp_media_to_cloudinary(media_url: str, org_id: str, media_id: str, access_token: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    # 1. Download bytes from WhatsApp
    async with httpx.AsyncClient() as client:
        response = await client.get(media_url, headers=headers)
        
        if response.status_code != 200:
            logger.error(f"Failed to download from WhatsApp: {response.text}")
            return None

    # 2. Convert bytes to a file-like object
    # Cloudinary expects a file object or path. We use BytesIO for in-memory files.
    file_obj = io.BytesIO(response.content)
    file_obj.name = media_id 

    try:
        # 3. Upload to Cloudinary
        # Note: Cloudinary SDK is synchronous, so this blocks the thread slightly.
        # For high-scale, you might want to run this in a background task.
        cloudinary_result = cloudinary.uploader.upload(
            file_obj,
            folder=f"instagram_media/{org_id}", # Or whatsapp_media
            public_id=media_id,                 # Optional: use WhatsApp ID as Cloudinary ID
            resource_type="auto"
        )
        
        # 4. Return the new public URL
        return cloudinary_result.get("secure_url")

    except Exception as e:
        logger.error(f"Cloudinary upload error: {e}")
        return None
    

async def send_whatsapp_reaction(org_id, message_id, emoji):
    """Send a reaction to a WhatsApp message"""
    try: 

        messages_collection = db[f"messages_{org_id}"]

        # Find the message first
        message = messages_collection.find_one({"message_id": message_id})
        recipient_id = message.get("sender_id")

        if not message:
            logger.error("Message not found")
            return {"status": "error", "message": "Message not found"}
        
        org_document = db.organizations.find_one({"org_id": org_id})
        whatsapp_id = org_document.get("wa_id")


        if not whatsapp_id:
            raise HTTPException(status_code=400, detail="WhatsApp ID not configured for this organization")

        # Get the page access token for this WhatsApp account
        connection = db.whatsapp_connections.find_one(
            {"wa_id": whatsapp_id, "org_id": org_id},
            {"access_token" : 1, "phone_id": 1, "name": 1, "phone": 1,"_id": 0}
        )



        if not connection or not connection.get("access_token") or not connection.get("phone_id"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        access_token = connection["access_token"]
        phone_id = connection["phone_id"]
        name = connection["name"]
        phone = connection["phone"]

        url = f"https://graph.facebook.com/{VERSION}/{phone_id}/whatsapp_business_profile"
        params = {
            "access_token": access_token,
            "fields": "profile_picture_url"
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"WhatsApp API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch WhatsApp profile: {response.json().get('error', {}).get('message')}"
            )
        
        owner_details = response.json()
        owner_details = owner_details.get("data")
        profile_picture_url = None
        for data in owner_details:
            if data.get("profile_picture_url"):
                profile_picture_url = data.get("profile_picture_url")
                break
        

        # If reaction exists and same user reacted before → remove it
        if message.get("reaction"):
            current_reaction = message["reaction"]
            if current_reaction.get("username") == phone and current_reaction.get("emoji") == emoji:

                response = await send_message_reaction(message_id, phone_id, access_token, action="unreact", recipient_id=recipient_id, emoji=emoji)

                if response == "error":
                    return {"status": "error", "message": "Failed to remove reaction WhatsApp API Exception"}
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
                    platform_id=whatsapp_id,
                    platform_type="whatsapp",
                    event_type="message_reaction",
                    payload={"message": broadcast_message}
                )

                # Broadcast the reaction update to connected clients
                if whatsapp_id in services.whatsapp_connections_map:
                    logger.info(f"Broadcasting message_reaction event to Frontend for {whatsapp_id}")
                    await broadcast_whatsapp_message(whatsapp_id, "message_reaction", {
                        "message": broadcast_message
                    })

                logger.success(f"Reaction removed for message {message_id} with emoji {emoji}")

                return {"status": "success", "message": "sent"}

        response = await send_message_reaction(message_id, phone_id, access_token, action="react", recipient_id=recipient_id, emoji=emoji)

        if response == "error":
            return {"status": "error", "message": "Failed to remove reaction WhatsApp API Exception"}

        # Otherwise set a new reaction
        broadcast_message = messages_collection.find_one_and_update(
            {"message_id": message_id},
            {"$set": {
                "reaction": {
                    "emoji": emoji,
                    "username": phone,
                    "full_name": name,
                    "profile_picture_url": profile_picture_url,
                    "timestamp": datetime.now()
                }
            }}, 
            return_document=ReturnDocument.AFTER
        )

        if "_id" in broadcast_message:
            broadcast_message["_id"] = str(broadcast_message["_id"])

        await broadcast_main_ws(
            platform_id=whatsapp_id,
            platform_type="whatsapp",
            event_type="message_reaction",
            payload={"message": broadcast_message}
        )

        # Broadcast the reaction update to connected clients
        if whatsapp_id in services.whatsapp_connections_map:
            logger.info(f"Broadcasting message_reaction event to Frontend for {whatsapp_id}")
            await broadcast_whatsapp_message(whatsapp_id, "message_reaction", {
                "message": broadcast_message
            })

        logger.success(f"Reaction updated for message {message_id} with emoji {emoji}")
        return {"status": "success", "message": "sent"}
        
    
    except Exception as e:
        logger.error(f"Error sending WhatsApp reaction: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send reaction: {str(e)}"
        )

async def send_message_reaction(message_id: str, phone_id: str, access_token: str, action: str, recipient_id: str, emoji: str):
    """
    Send a reaction to a message
    """
    try:
        url = f"https://graph.facebook.com/v24.0/{phone_id}/messages"

        headers = {
            "Authorization" : "Bearer " + access_token,
            "Content-Type": "application/json"
        }

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "reaction",
            "reaction": {
                "message_id": message_id,
                "emoji": emoji if action == "react" else "",
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

async def get_templates(org_id: str, wa_id: str, access_token: str):
    """
    Fetch WhatsApp message templates associated with an organization.

    This function retrieves:
    1. Pre-built (Meta-provided) WhatsApp message templates.
    2. User-created WhatsApp templates linked to the given WhatsApp Business Account (wa_id).

    Behavior:
    - If `wa_id` is provided, the function fetches templates created under that WhatsApp account.
    - If `wa_id` is None or no user-created templates exist, only pre-built templates are returned.
    - Uses the provided `access_token` to authenticate requests to the WhatsApp (Meta) Graph API.
    """

    
    headers = {
            "Authorization" : "Bearer " + access_token,
            "Content-Type": "application/json"
        }
        
    user_created = []
    prebuilt =[]

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        if wa_id:
            org_url = f"https://graph.facebook.com/v24.0/{wa_id}/message_templates"
            response = await client.get(org_url, headers=headers)

            if response.status_code == 200:
                user_created = response.json().get("data", [])
                logger.info(
                     f"User-created templates = {len(user_created)}"
                )

        global_url = "https://graph.facebook.com/v24.0/message_template_library"
        response = await client.get(global_url, headers=headers)

        if response.status_code == 200:
            prebuilt = response.json().get("data", [])
            logger.info(
                 f"Pre-built templates = {len(prebuilt)}"
            )

        return {
            "user_created": user_created,
            "prebuilt": prebuilt,
            "meta": {
                "has_user_created": len(user_created) > 0,
                "has_prebuilt": len(prebuilt) > 0,
            }
        }
        