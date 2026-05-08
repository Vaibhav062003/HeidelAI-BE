# Built-in modules
import httpx
import asyncio
import json
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
from config.settings import IG_VERSION, VERSION, WA_VERSION, APP_ID
from schemas.models import InteractiveListMessage, InteractiveReplyButtonMessage, InteractiveUrlButtonMessage, MessageRole
from services.cloudinary_service import upload_media_to_cloudinary
from services.conversation_utils import reopen_conversation
from services.scheduler_service import abort_scheduled_message
from services.websocket_service import broadcast_main_ws
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations
from loguru import logger
from schemas.models import WhatsAppInteractiveMessage

MSG_SPLIT = "|MSG_SPLIT|"



db = get_mongo_db()
mongo_client = get_mongo_client()


async def create_or_update_whatsapp_conversation(
    org_id: str, 
    recipient_id, 
    customer_phone_no, 
    customer_name, 
    last_message, 
    last_sender,
    reply_window_expiry: Optional[datetime] = None,
    is_ad_referral: Optional[bool] = None,
    is_auto_followup: bool = False,
    mode: Optional[str] = None,
) -> str:
    """
    Create or update a conversation in the WhatsApp-specific conversations collection
    
    Args:
        org_id: Organization ID for the current context
        recipient_id: WhatsApp business account ID
        customer_phone_no: WhatsApp user phone number who sent the message        
        customer_name: Name of the customer
        last_message: The content of the last message
        last_sender: Identifier indicating who sent the last message (e.g., "agent" or "customer")
        reply_window_expiry: The expiry time for the reply window
        is_ad_referral: Whether the message originated from a CTWA ad
        is_auto_followup: Whether the message is an auto-follow-up
        mode: The mode of the message (e.g., "private")

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

    # Base fields that ALWAYS get updated
    update_set = {
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
    }

    # Fields that ONLY get set if a NEW conversation is created
    update_set_on_insert = {
        "is_ai_enabled": True,  #TODO: By default AI tick is true (Maybe have a setting to control this at org level in the future? On FE - "New conversations have AI Enabled Y/N")
        "started_at": now,
        "created_at": now,
        "closed_at": None
    }

    # Counters to increment atomically
    update_inc = {
        "message_counter": 1,
        "unread_count": 0 if is_being_viewed else 1
    }

    # Handle Agent vs Customer logic
    if existing_conversation:
        abort_result = await abort_scheduled_message(org_id, conversation_id)
        logger.success(f"Aborted Scheduled message: {abort_result}")

        reopen_result = await reopen_conversation(org_id, conversation_id)
        logger.success(f"Reopened conversation: {reopen_result}")

    if last_sender == "customer":
        update_set["reminder_stage"] = 0
        update_set["next_action_at"] = None
    elif (last_sender == "agent" or last_sender == "ai") and (not is_auto_followup):
        # Manual agent message resets the inactivity countdown 
        update_set["reminder_stage"] = 0
        update_set["next_action_at"] = now + timedelta(hours=6)

    # Assemble the final MongoDB update document safely
    mongo_update = {
        "$set": update_set,
        "$setOnInsert": update_set_on_insert,
        "$inc": update_inc
    }

    # Safely apply $max ONLY if we have a valid expiry from the customer
    if last_sender == "customer" and reply_window_expiry:
        mongo_update["$max"] = {"reply_window_ends_at": reply_window_expiry}
        update_set["is_ad_referral"] = is_ad_referral 

    # Execute atomic update
    updated_conversation = conversations_collection.find_one_and_update(
        {"conversation_id": conversation_id},
        mongo_update,
        upsert=True,
        return_document=ReturnDocument.AFTER
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
    
    # Broadcast stats update for every message to ensure live dashboard refresh
    # await broadcast_on_stats_update(org_id)
        
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
    context: Optional[dict] = None,
    bot_context: Optional[dict] = None
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
        "mode": mode,
        "ai_metadata" : bot_context if bot_context else None
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
        return message_id
    
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

async def send_whatsapp_message(
        message: dict, 
        org_id: str, 
        mode:str, 
        temp_id: Optional[str] = None, 
        context_type: Optional[str] = None, 
        context: Optional[dict] = None,
        bot_context: Optional[dict] = None,
        raise_on_partial_failure: bool = False,
        user_full_name: Optional[str] = None,
    ) -> dict:
    """
    Send a message to an Whatsapp user using the appropriate access token
    
    Args:
        Sender wa_id: The recipient's WhatsApp user ID
        message: {
            "conversation_id": str,
            "content": str | list[str],
            "sender_id": str,
        }
        raise_on_partial_failure: If True, raises HTTPException if any message in a multi-message payload fails.
    """
    whatsapp_id = message.get("sender_id")

    if not whatsapp_id:
        logger.error("Missing 'sender_id' in outbound WhatsApp message payload")
        raise HTTPException(status_code=422, detail="Missing 'sender_id'")
        
    # Query the database to validate the ID
    wa_connection = db.whatsapp_connections.find_one(
        {"wa_id": whatsapp_id},
        {"wa_id": 1, "_id": 0}
    )
    
    if not wa_connection:
        logger.warning(f"Received webhook for unregistered WhatsApp ID: {whatsapp_id}")
        raise HTTPException(status_code=403, detail="Received webhook for unregistered WhatsApp ID")
        
    # safe to use wa_id
    whatsapp_business_id = wa_connection["wa_id"]
        
    
    try:
        conversation_id = message["conversation_id"]
        original_content = message["content"]
        
        # Find the WhatsApp connection to get the access token
        connection = db.whatsapp_connections.find_one({"wa_id": whatsapp_business_id})
        
        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")

        # Extract the recipient phone number from conversation ID
        recipient_id = conversation_id.replace("whatsapp_", "")

        # Handle multi-part messages (e.g. lists returned by the agent or strings with MSG_SPLIT)
        if isinstance(original_content, list):
            contents = [str(m).strip() for m in original_content if str(m).strip()]
        elif isinstance(original_content, str) and MSG_SPLIT in original_content:
            contents = [m.strip() for m in original_content.split(MSG_SPLIT) if m.strip()]
        else:
            contents = [str(original_content)]

        if not contents:
            logger.warning(f"Empty content provided for WhatsApp message to {recipient_id}")
            return {"message": "No content to send"}

        last_stored_id = None
        all_message_ids = []

        # Send each content part as a separate bubble
        for i, content_item in enumerate(contents):
            data = {
                "messaging_product": "whatsapp",
                "to": recipient_id,
                "type": "text",
                "text": {"body": content_item}
            }

            if context_type and context:
                data["context"] = {
                    "message_id": context.get("mid")
                }
            
            if mode == "reply" or mode == "ai":
                # Send message via WhatsApp Cloud API
                url = f"https://graph.facebook.com/{IG_VERSION}/{connection['phone_id']}/messages"
                headers = {
                    "Authorization": f"Bearer {connection['access_token']}",
                    "Content-Type": "application/json",
                }
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(url, headers=headers, json=data)
                
                if response.status_code != 200:
                    logger.error(f"WhatsApp API error: {response.text}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Failed to send WhatsApp message part {i+1}"
                    )
                
                response_data = response.json()
                message_id = response_data.get("messages", [{}])[0].get("id")
            else: 
                message_id = f"privatenote_{uuid.uuid4()}"
                response_data = {"status": "Private note created, no API call made"}
            
            all_message_ids.append(message_id)
            now = datetime.now(timezone.utc)

            if mode == "private":
                last_message = f"Private Note: {content_item}"
            elif mode == "ai":
                last_message= f"AI: {content_item}"
            else:
                last_message = f"You: {content_item}"

            wa_conversation = {
                "last_message": last_message,
                "last_message_timestamp": now,
                "last_sender": "ai" if mode == "ai" else "agent" ,
                "reminder_stage": 0,
                "next_action_at": now + timedelta(hours=6),
                "last_message_is_private_note": mode == "private"
            }

            # Update conversation for each part (last one will reflect the final state)
            db[f"conversations_{org_id}"].find_one_and_update(
                {"conversation_id": conversation_id},
                {"$set": wa_conversation},
                return_document=ReturnDocument.AFTER  
            )

            # Store message in WhatsApp-specific collection
            last_stored_id = await store_whatsapp_message(
                org_id=org_id,
                content=content_item,
                conversation_id=conversation_id,
                customer_phone_no=recipient_id,
                type="text",
                payload=None,
                sender_name="AI" if mode == "ai" else user_full_name,
                recipient_id=whatsapp_business_id,
                role=MessageRole.AI if mode == "ai" else MessageRole.AGENT,
                mode=mode,
                message_id=message_id,
                raw_data=response_data,
                status="delivered" if mode == "private" else "sending",
                temp_id=temp_id,
                context=context,
                context_type=context_type,
                bot_context=bot_context
            )

            # Small delay between bubbles to ensure delivery order
            if len(contents) > 1 and i < len(contents) - 1:
                await asyncio.sleep(0.5)

        # Broadcast the final conversation state once after all bubbles are sent
        final_conversation = db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
        if final_conversation:
            if "_id" in final_conversation:
                final_conversation["_id"] = str(final_conversation["_id"])
            
            # Convert datetimes for JSON
            for key in ["last_message_timestamp", "next_action_at", "started_at", "created_at", "reply_window_ends_at"]:
                if final_conversation.get(key) and isinstance(final_conversation[key], datetime):
                    final_conversation[key] = final_conversation[key].isoformat()

            await broadcast_main_ws(
                platform_id=connection.get("wa_id"),
                platform_type="whatsapp",
                event_type="conversation_updated",
                payload={"conversation": final_conversation}
            )

        return {"message": "Messages sent", "message_ids": all_message_ids, "message_id": last_stored_id}
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send message: {str(e)}"
        )

async def send_whatsapp_template_message(recipient_id: str, template: dict, wa_id: str, org_id: str, temp_id: Optional[str], mode: str, context_type: Optional[str], context: Optional[dict]) -> dict:
    """
    Send a template message via WhatsApp Cloud API
    """
    try:
        # Find the WhatsApp connection to get the access token
        connection = db.whatsapp_connections.find_one({"wa_id": wa_id})

        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")

        # Normalize template language format if it's a string
        if "language" in template and isinstance(template["language"], str):
            template["language"] = {"code": template["language"]}

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "template",
            "template": template
        }
        
        if context_type and context:
            data["context"] = {
                "message_id": context.get("mid")
            }
        
        if mode == "reply":
            # Send message via WhatsApp Cloud API
            url = f"https://graph.facebook.com/{IG_VERSION}/{connection.get('phone_id')}/messages"
            headers = {
                "Authorization": f"Bearer {connection.get('access_token')}",
                "Content-Type": "application/json",
            }
            
            async with httpx.AsyncClient() as client:
                response = await client.post(url, headers=headers, json=data)
            
            if response.status_code != 200:
                logger.error(f"WhatsApp API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Failed to send WhatsApp template message"
                )
            
            response_data = response.json()
            message_id = response_data.get("messages", [{}])[0].get("id")
        else:
            message_id = f"privatenote_{uuid.uuid4()}"
            response_data = {"status": "Private note created, no API call made"}

        now = datetime.now(timezone.utc)
        template_name = template.get("name", "Unknown Template")
        
        wa_conversation = {
            "last_message": f"Private Note: Shared a template: {template_name}" if mode == "private" else f"You: Shared a template: {template_name}",
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

        if conversation and "_id" in conversation:
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
            content=f"Shared a template {template_name}",
            conversation_id=f"whatsapp_{recipient_id}",
            customer_phone_no=recipient_id,
            type="template",
            payload=template,
            sender_name="Agent",
            recipient_id=wa_id,
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
        logger.error(f"Error sending WhatsApp template message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send template message: {str(e)}"
        )

async def send_wa_media_message(
        recipient_id: str, 
        media_url: str, 
        media_type: str, 
        wa_id: str, 
        content: Optional[str], 
        org_id: str, 
        temp_id: Optional[str], 
        mode: str, 
        context_type: Optional[str], 
        context: Optional[dict]
    ) -> dict:
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
            
            
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=headers, json=data)
            
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
                timeout = httpx.Timeout(10.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.delete(revoke_url, params=params)
                    if not response.is_success:
                        logger.error(
                            f"Token revocation returned status {response.status_code} for org {org_id} "
                            f"and wa_id {wa_id}: {response.text}"
                        )
            except httpx.HTTPError as e:
                logger.error(f"HTTP error revoking token for org {org_id}: {e}")
            except Exception as e:
                logger.error(f"Unexpected error revoking token: {e}")

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
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
        
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

        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=body)
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
    - User-created WhatsApp templates linked to the given WhatsApp Business Account (wa_id).

    Behavior:
    - If `wa_id` is provided, the function fetches templates created under that WhatsApp account.
    - If `wa_id` is not provided or is falsy, no request is made and an empty template list is returned.
    - The provided `access_token` is used to authenticate requests to the WhatsApp (Meta) Graph API when a request is made.
    """

    
    headers = {
            "Authorization" : "Bearer " + access_token,
            "Content-Type": "application/json"
        }
        
    templates = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        if wa_id:
            org_url = f"https://graph.facebook.com/v24.0/{wa_id}/message_templates"
            response = await client.get(org_url, headers=headers)

            if response.status_code == 200:
                templates = response.json().get("data", [])
                logger.info(
                     f"User-created templates = {len( templates)}"
                )
        return {
            "templates": templates,
            "meta": {
                "has_templates": len(templates) > 0,
            }
        }
        

async def create_templates(org_id:str, wa_id:str, access_token:str, template_name:str, language:str,category:str, components:list):
    """
    Create a WhatsApp message template for an organization.

    Args:
        org_id: The organization ID
        wa_id: The WhatsApp Business Account ID
        access_token: The access token for the WhatsApp Business Account
        template_name: The name of the template
        language: The language of the template
        components: The components of the template
    """

    url = f"https://graph.facebook.com/v24.0/{wa_id}/message_templates"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    data = {
        "name": template_name,
        "language": language,
        "category": category,
        "components": components,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(url, headers=headers, json=data)
    except httpx.RequestError as e:
        logger.error(f"WhatsApp API request failed | org_id={org_id} wa_id={wa_id} error={str(e)}")
        raise HTTPException(status_code=502, detail="Unable to reach WhatsApp API") from e
    except Exception as e:
        logger.exception(f"Unexpected error calling WhatsApp API | org_id={org_id} | error={str(e)}")
        raise HTTPException(status_code=500, detail="Internal WhatsApp server error") from e

    # Process response outside of the network error catch-all
    try:
        response_json = response.json()
    except json.JSONDecodeError:
        response_json = {"error": {"message": response.text}}

    logger.info(
        f"WhatsApp API response | org_id={org_id} wa_id={wa_id} template={template_name} "
        f"status={response.status_code} response={response_json}"
    )

    if response.status_code != 200:
        error_body = response_json.get("error", {}) if isinstance(response_json, dict) else {}
        if not isinstance(error_body, dict):
            error_body = {}
        error_msg = (
            error_body.get("error_user_msg")
            or error_body.get("message")
            or str(response_json)
        )
        logger.error(
            f"WhatsApp API error | org_id={org_id} wa_id={wa_id} template={template_name} "
            f"status={response.status_code} response={response_json}"
        )
        raise HTTPException(status_code=response.status_code, detail=error_msg)

    logger.success(f"WhatsApp template created | org_id={org_id} wa_id={wa_id} template={template_name}")
    return response_json

# WhatsApp Interactive Messages (Free to send)

def build_list_message_payload(message_data: InteractiveListMessage) -> dict:
    """
    Constructs the Meta WhatsApp API payload for a 'list' interactive message.
    
    Args:
        message_data (InteractiveListMessage): A validated Pydantic model containing 
            the list message details (body, footer, button text, and sections/rows).
            
    Returns:
        dict: The dictionary representing the 'interactive' JSON object required 
            by the Meta Cloud API.
    """

    interactive_content = {
        "type": "list",
        "body": {
            "text": message_data.body_text
        },
        "footer": {
            "text": message_data.footer_text
        },
        "action": {
            "button": message_data.button_text,
            "sections": [section.model_dump(exclude_none=True) for section in message_data.sections]
        }
    }

    if message_data.header_text:
        interactive_content["header"] = {
            "type": "text",
            "text": message_data.header_text
        }

    return interactive_content

def build_url_button_message_payload(message_data: InteractiveUrlButtonMessage) -> dict:
    """
    Constructs the Meta WhatsApp API payload for a 'cta_url' (Call To Action URL) interactive message.
    
    Args:
        message_data (InteractiveUrlButtonMessage): A validated Pydantic model containing 
            the URL button details (optional media/text header, body, footer, button text, and URL).
            
    Returns:
        dict: The dictionary representing the 'interactive' JSON object required 
            by the Meta Cloud API.
    """

    interactive_content = {
        "type": "cta_url",
        "body": {
            "text": message_data.body_text
        },
        "footer": {
            "text": message_data.footer_text
        },
        "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": message_data.button_text,
                "url": message_data.url
            }
        }
    }

    # Dump the polymorphic header directly if it exists
    if message_data.header:
        interactive_content["header"] = message_data.header.model_dump(exclude_none=True)

    return interactive_content

def build_reply_button_message_payload(message_data: InteractiveReplyButtonMessage) -> dict:
    """
    Constructs the Meta WhatsApp API payload for a 'button' (Quick Reply) interactive message.
    
    Args:
        message_data (InteractiveReplyButtonMessage): A validated Pydantic model containing 
            the reply button details (optional media/text header, body, footer, and up to 3 buttons).
            
    Returns:
        dict: The dictionary representing the 'interactive' JSON object required 
            by the Meta Cloud API.
    """

    interactive_content = {
        "type": "button",
        "body": {
            "text": message_data.body_text
        },
        "footer": {
            "text": message_data.footer_text
        },
        "action": {
            "buttons": [button.model_dump(exclude_none=True) for button in message_data.buttons]
        }
    }

    # Dump the polymorphic header directly if it exists
    if message_data.header:
        interactive_content["header"] = message_data.header.model_dump(exclude_none=True)

    return interactive_content

# For routing to different interactive message types
ROUTER_MAP = {
    "list": build_list_message_payload,
    "cta_url": build_url_button_message_payload,
    "button": build_reply_button_message_payload,
}

async def send_interactive_whatsapp_message(
        org_id: str, 
        conversation_id: str, 
        sender_id: str, 
        interactive_message: WhatsAppInteractiveMessage, 
    ) -> dict:
    """
    Routes and sends a polymorphic interactive WhatsApp message via the Meta Cloud API,
    and logs the interaction in the database.

    Args:
        org_id (str): Organization ID of the sender's HeidelAI org (used to store the 
            sent message in the appropriate org's collections).
        conversation_id (str): Formatted as 'whatsapp_{recipient_phone_number}', used 
            to extract the recipient ID and update the correct conversation thread.
        sender_id (str): The WhatsApp Business Account ID (wa_id) used to fetch the 
            sender's access token and phone number from the database.
        interactive_message (WhatsAppInteractiveMessage): A discriminated union Pydantic 
            model representing the message data. It routes based on the 'type' field 
            (e.g., "list", "cta_url", "button").

    Returns:
        dict: A dictionary containing a success message and the internal database 'message_id'.

    Raises:
        HTTPException: If the sender_id is unregistered, the connection is misconfigured,
            the message type is unsupported, or the Meta API rejects the request.
    """

    whatsapp_id = sender_id
    if not whatsapp_id:
        logger.error("Webhook payload is missing 'sender_id'")
        raise HTTPException(status_code=404, detail="Error: Webhook payload is missing 'sender_id'")
        
    # Query the database to validate the ID
    wa_connection = db.whatsapp_connections.find_one({"wa_id": whatsapp_id})
    if not wa_connection:
        logger.warning(f"Received webhook for unregistered WhatsApp ID: {whatsapp_id}")
        raise HTTPException(status_code=403, detail="Received webhook for unregistered WhatsApp ID")
    
    whatsapp_business_id = wa_connection.get("wa_id")
    access_token = wa_connection.get("access_token")
    phone_id = wa_connection.get("phone_id")
    recipient_id = conversation_id.replace("whatsapp_", "")

    if not access_token or not phone_id or not whatsapp_business_id:
        raise HTTPException(status_code=404, detail="WhatsApp connection not properly configured")
    
    try:    
        # Route to the appropriate handler function based on the interactive message type
        handler_function = ROUTER_MAP.get(interactive_message.type)
        
        if handler_function:
            interactive_content = handler_function(interactive_message)
        else:
            raise HTTPException(status_code=400, detail="Unsupported interactive message type")

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "interactive",
            "interactive": interactive_content
        }

        # Send message via WhatsApp Cloud API
        url = f"https://graph.facebook.com/{IG_VERSION}/{phone_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        } 
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            logger.error(f"WhatsApp API error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to send WhatsApp message"
            )
        
        response_data = response.json()
        message_id = response_data.get("messages", [{}])[0].get("id")
        
        now = datetime.now(timezone.utc)
        last_message = f"AI: Sent an Interactive Message"

        wa_conversation = {
            "last_message": last_message,
            "last_message_timestamp": now,
            "last_sender": "ai",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "last_message_is_private_note": False
        }

        conversation = db[f"conversations_{org_id}"].find_one_and_update(
            {"conversation_id": conversation_id},
            {"$set": wa_conversation},
            return_document=ReturnDocument.AFTER  
        )

        if "_id" in conversation:
            conversation["_id"] = str(conversation["_id"])

        await broadcast_main_ws(
            platform_id=whatsapp_business_id,
            platform_type="whatsapp",
            event_type="conversation_updated",
            payload={"conversation": conversation}
        )

        # Store message in WhatsApp-specific collection
        mid = await store_whatsapp_message(
            org_id=org_id,
            content=interactive_message.body_text,
            conversation_id=conversation_id,
            customer_phone_no=recipient_id,
            type="interactive",
            payload=interactive_message.model_dump(exclude_none=True),
            sender_name="AI",
            recipient_id=whatsapp_business_id,
            role=MessageRole.AI,
            mode="reply",
            message_id=message_id,
            status="sending",
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

async def read_message_and_typing_indicator(whatsapp_id: str, mode: Optional[str] = None, message_id: Optional[str] = None):
    """
    Marks a message as read and sends a typing indicator to the recipient.
    """
    try:
        if not whatsapp_id:
            logger.error("Missing 'whatsapp_id' when trying to send typing indicator")
            return
        
        org_connection = db.organizations.find_one(
            {"wa_id": whatsapp_id}
        )
        
        if not org_connection:
            return

        org_id = org_connection.get("org_id")

        messages_collection = "messages_" + org_id

        if mode == "platform":
            message_doc = db[messages_collection].find_one(
                {
                    "role": MessageRole.CUSTOMER, 
                    "status": {"$ne": "read"}
                },
                {
                    "message_id": 1, 
                    "_id": 0
                },
                sort=[("timestamp", -1)]
            )
            message_id = message_doc.get("message_id") if message_doc else None

        if not message_id:
            logger.error(f"No message_id available when trying to send typing indicator for WhatsApp ID: {whatsapp_id}")
            return
        
        # Query the database to validate the ID
        connection = db.whatsapp_connections.find_one(
            {"wa_id": whatsapp_id}
        )
        
        if not connection:
            logger.warning(f"Trying to process webhook for unregistered WhatsApp ID: {whatsapp_id}")
            return

        url = f"https://graph.facebook.com/{WA_VERSION}/{connection['phone_id']}/messages"
        headers = {
            "Authorization": f"Bearer {connection['access_token']}",
            "Content-Type": "application/json",
        }
        data={
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {
                "type": "text"
            }
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, headers=headers, json=data)
        
        if response.status_code != 200:
            logger.error(f"WhatsApp API error: {response.text}")
            return
        
        message = db[messages_collection].find_one({"message_id": message_id})
        if not message:
            logger.error(f"Message with ID {message_id} not found for read receipt")
            return

        # Update the message status to "read"
        db[messages_collection].update_one(
            {"message_id": message_id},
            {"$set": {"status": "read", "updated_at": datetime.now(timezone.utc)}}
        )

    except Exception as e:
        logger.error(f"Error while sending typing indicator: {e}")

# Bulk jobs are now stored in the "bulk_jobs" collection in MongoDB.
# This ensures they survive restarts and work across multiple instances.
# A TTL index is created on "created_at" to clean up old jobs after 7 days (604800 seconds).
_bulk_job_indexes_initialized = False

def ensure_bulk_job_indexes() -> None:
    """
    Ensure indexes for bulk_jobs and bulk_job_results collections exist.
    This should be called from an application startup hook or migration step,
    rather than at module import time, to avoid redundant index creation in
    multi-worker deployments and to prevent noisy startup warnings if the
    database is temporarily unavailable.
    """
    global _bulk_job_indexes_initialized
    if _bulk_job_indexes_initialized:
        return
    try:
        db.bulk_jobs.create_index("created_at", expireAfterSeconds=604800)
        # Index bulk_jobs by job_id and (job_id, org_id) to support common lookups/updates.
        db.bulk_jobs.create_index([("job_id", 1)])
        db.bulk_jobs.create_index([("job_id", 1), ("org_id", 1)])
        # Also index job_results by job_id for performance and by created_at for TTL cleanup.
        db.bulk_job_results.create_index("job_id")
        db.bulk_job_results.create_index("created_at", expireAfterSeconds=604800)
        _bulk_job_indexes_initialized = True
    except Exception as e:
        # Preserve existing behavior of logging a warning without raising.
        logger.warning(
            f"Failed to create indices for bulk_jobs or bulk_job_results: {e}"
        )

def recover_stale_bulk_jobs() -> None:
    """
    To avoid interfering with jobs that are still actively being processed by other
    workers in a multi-instance deployment, only jobs that appear stale (i.e., have
    not been updated for longer than a heartbeat threshold) are recovered.
    """
    try:
        now = datetime.now(timezone.utc)
        # Consider running jobs stale if they have not been updated for more than 10 minutes.
        # This assumes workers periodically update `updated_at` while processing.
        stale_before = now - timedelta(minutes=10)
        result = db.bulk_jobs.update_many(
            {
                "status": "running",
                "updated_at": {"$lt": stale_before},
            },
            {
                "$set": {
                    "status": "failed",
                    "error": "Job interrupted (server restart or crash)",
                    "updated_at": now,
                }
            },
        )
        if result.modified_count > 0:
            logger.info(
                f"Bulk WA | Recovered {result.modified_count} stale running jobs "
                f"(no updates since before {stale_before.isoformat()})"
            )
    except Exception as e:
        logger.warning(f"Failed to recover stale bulk jobs: {e}")


def create_bulk_job(
    org_id: str,
    job_id: str,
    template_name: str,
    language_code: str,
    total: int,
) -> None:
    """
    Create a new bulk-send job record in MongoDB so the frontend can poll it.
    """
    now = datetime.now(timezone.utc)
    job_data = {
        "job_id": job_id,
        "org_id": org_id,
        "template_name": template_name,
        "language_code": language_code,
        "status": "pending",   # pending → running → completed / failed
        "total": total,
        "sent": 0,
        "failed": 0,
        # RESULTS SHOULD BE STORED IN A SEPARATE COLLECTION TO AVOID 16MB LIMIT
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    try:
        db.bulk_jobs.insert_one(job_data)
    except Exception as e:
        logger.error(f"Bulk WA | job={job_id} | org={org_id} | Failed to create bulk job record: {e}")
        # Optionally re-raise or handle as needed. 
        # In this context, the caller expects the job to be created.
        raise


def get_bulk_job(
    job_id: str,
    org_id: Optional[str] = None,
    include_results: bool = True,
) -> Optional[dict]:
    """
    Retrieves a bulk job from MongoDB by ID, verifying org_id if provided.
    """
    query = {"job_id": job_id}
    if org_id:
        query["org_id"] = org_id
        
    job = db.bulk_jobs.find_one(query)
    if not job:
        return None
    
    # Remove _id (or convert to string) for serialization
    if "_id" in job:
        job["_id"] = str(job["_id"])
    
    # Format datetimes for serializability
    for key in ["created_at", "updated_at", "completed_at"]:
        if job.get(key) and isinstance(job[key], datetime):
            job[key] = job[key].isoformat()

    # Optionally fetch a capped tail of recent results from the separate collection
    if include_results:
        try:
            # Fetch the most recent 1000 results for this job_id
            cursor = (
                db.bulk_job_results.find({"job_id": job_id})
                .sort("_id", -1)
                .limit(1000)
            )
            # Reverse to return results in chronological order
            results = list(cursor)[::-1]
            for r in results:
                if "_id" in r:
                    r["_id"] = str(r["_id"])
                # Format datetimes in result documents for serializability
                for key in ["created_at", "updated_at", "completed_at"]:
                    if r.get(key) and isinstance(r[key], datetime):
                        r[key] = r[key].isoformat()
                # Remove sensitive fields such as phone numbers before exposing results
                if "phone_number" in r:
                    r.pop("phone_number", None)
            job["results"] = results
        except Exception:
            # Graceful degradation if results fetch fails
            job.setdefault("results", [])

    return job


async def run_bulk_send_job(
    wa_id: str,
    job_id: str,
    org_id: str,
    phone_id: str,
        access_token: str,
    template_name: str,
    language_code: str,
    recipients: list,
    components: Optional[list] = None,
    delay_seconds: float = 8.0,
    max_concurrent: int = 50,
    message_text: Optional[str] = None,
) -> None:
    """
    Background worker: sends WhatsApp templates using a bounded worker queue,
    offloading sync DB updates to threads to avoid blocking the event loop.
    All successful sends are saved to the messages and conversations collections in bulk.
    """
    total_recipients = len(recipients)
    if total_recipients > 2000:
        logger.warning(
            f"Bulk WA | job={job_id} | Large broadcast detected: {total_recipients} recipients. "
            f"Estimated completion time: ~{(total_recipients / max_concurrent * (delay_seconds + 1)) / 60:.1f} minutes."
        )
    
    url = f"https://graph.facebook.com/{IG_VERSION}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Offload sync MongoDB calls to threads to avoid blocking the loop
    job_record = await asyncio.to_thread(db.bulk_jobs.find_one, {"job_id": job_id, "org_id": org_id})
    if job_record is None:
        logger.error(f"Bulk WA | job={job_id} | org={org_id} | missing job record in MongoDB")
        return
    await asyncio.to_thread(
        db.bulk_jobs.update_one,
        {"job_id": job_id, "org_id": org_id},
        {"$set": {"status": "running", "updated_at": datetime.now(timezone.utc)}}
    )

    def mask_phone(phone: str) -> str:
        if not phone or len(phone) < 4:
            return "****"
        return f"+XXXXX{phone[-4:]}"

    async def send_to_recipient(index, recipient, client):
        phone_number = recipient.phone_number
        masked_phone = mask_phone(phone_number)
        
        if message_text:
            # Plain-text mode (used by Google Sheet automations)
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone_number,
                "type": "text",
                "text": {"body": message_text},
            }
        else:
            # Standard template mode
            template_payload = {
                "name": template_name,
                "language": {"code": language_code},
            }
            if components:
                template_payload["components"] = components

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone_number,
                "type": "template",
                "template": template_payload,
            }

        try:
            logger.debug(f"Bulk WA | job={job_id} | [{index + 1}] sending to {masked_phone}")
            response = await client.post(url, headers=headers, json=payload)
            
            if response.status_code == 200:
                messages = response.json().get("messages") or [{}]
                message_id = messages[0].get("id")
                result_entry = {
                    "job_id": job_id,
                    "phone_number": phone_number,
                    "status": "sent",
                    "message_id": message_id,
                    "created_at": datetime.now(timezone.utc)
                }
                update_fields = {"$inc": {"sent": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}
            else:
                try:
                    error_json = response.json()
                    if isinstance(error_json, dict):
                        error_detail = error_json.get("error", {}).get("message") or response.text
                    else:
                        error_detail = str(error_json)
                except (ValueError, json.JSONDecodeError):
                    # Fallback when response body is not valid JSON
                    error_detail = response.text
                logger.error(f"Bulk WA | job={job_id} | failed for {masked_phone} | error={error_detail}")
                result_entry = {
                    "job_id": job_id,
                    "phone_number": phone_number,
                    "status": "failed",
                    "error": error_detail,
                    "created_at": datetime.now(timezone.utc)
                }
                update_fields = {"$inc": {"failed": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}
        except Exception as e:
            logger.error(f"Bulk WA | job={job_id} | exception for {masked_phone} | error={str(e)}")
            result_entry = {
                "job_id": job_id,
                "phone_number": phone_number,
                "status": "failed",
                "error": str(e),
                "created_at": datetime.now(timezone.utc)
            }
            update_fields = {"$inc": {"failed": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}

        return result_entry, update_fields

    try:
        messages_collection_name = f"messages_{org_id}"
        conversations_collection_name = f"conversations_{org_id}"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Process in chunks of max_concurrent
            for i in range(0, len(recipients), max_concurrent):
                chunk = recipients[i : i + max_concurrent]
                current_batch = (i // max_concurrent) + 1
                total_batches = (total_recipients + max_concurrent - 1) // max_concurrent
                
                logger.info(f"Bulk WA | job={job_id} | processing batch {current_batch}/{total_batches} | size={len(chunk)}")
                
                tasks = [send_to_recipient(i + idx, r, client) for idx, r in enumerate(chunk)]
                batch_results = await asyncio.gather(*tasks)
                
                # Batch DB operations
                entries = [res[0] for res in batch_results]
                
                total_sent = sum(res[1]["$inc"].get("sent", 0) for res in batch_results)
                total_failed = sum(res[1]["$inc"].get("failed", 0) for res in batch_results)
                
                # Prepare message and conversation bulk operations for successful sends
                message_docs = []
                convo_updates = []
                now = datetime.now(timezone.utc)
                
                for entry, _ in batch_results:
                    if entry["status"] == "sent":
                        phone_number = entry["phone_number"]
                        msg_id = entry["message_id"]
                        convo_id = f"whatsapp_{phone_number}"
                        
                        # Prepare individual message doc
                        message_doc = {
                            "id": msg_id,
                            "message_id": msg_id,
                            "platform": "whatsapp",
                            "conversation_id": convo_id,
                            "content": f"Shared a template {template_name}",
                            "type": "template",
                            "payload": {
                                "name": template_name,
                                "language": {"code": language_code},
                                "components": components
                            },
                            "sender_id": phone_number, # Matching existing convention for agent messages
                            "sender_name": "AI",
                            "role": MessageRole.AI,
                            "timestamp": now,
                            "status": "sent",
                            "mode": "reply",
                            "context": None,
                            "context_type": None
                        }
                        message_docs.append(message_doc)
                        
                        # Prepare individual conversation update (upsert if session doesn't exist)
                        convo_updates.append(UpdateOne(
                            {"conversation_id": convo_id},
                            {
                                "$set": {
                                    "platform": "whatsapp",
                                    "whatsapp_id": wa_id,
                                    "customer_id": phone_number,
                                    "last_message": f"AI: Shared a template {template_name}",
                                    "last_message_timestamp": now,
                                    "last_sender": "ai",
                                    "status": "open",
                                    "reminder_stage": 0,
                                    "next_action_at": now + timedelta(hours=6),
                                    "last_message_is_private_note": False
                                },
                                "$setOnInsert": {
                                    "is_ai_enabled": True,
                                    "started_at": now,
                                    "created_at": now,
                                    "unread_count": 0,
                                    "closed_at": None,
                                    "message_counter": 0
                                }
                            },
                            upsert=True
                        ))

                # Combine atomic updates for bulk_jobs
                combined_update = {
                    "$inc": {},
                    "$set": {"updated_at": datetime.now(timezone.utc)}
                }
                if total_sent > 0: combined_update["$inc"]["sent"] = total_sent
                if total_failed > 0: combined_update["$inc"]["failed"] = total_failed

                db_tasks = [
                    asyncio.to_thread(db.bulk_job_results.insert_many, entries),
                    asyncio.to_thread(db.bulk_jobs.update_one, {"job_id": job_id, "org_id": org_id}, combined_update)
                ]
                
                # Execute bulk writes for messages and conversations if there are successes
                if message_docs:
                    db_tasks.append(asyncio.to_thread(db[messages_collection_name].insert_many, message_docs, ordered=False))
                if convo_updates:
                    db_tasks.append(asyncio.to_thread(db[conversations_collection_name].bulk_write, convo_updates, ordered=False))

                try:
                    await asyncio.gather(*db_tasks)
                except Exception as db_err:
                    logger.error(f"Bulk WA | job={job_id} | database error during batch tracking: {db_err}")
                    # We don't raise here to allow subsequent batches to try, 
                    # but the job results might be incomplete in DB.

                # Delay between batches if there are more messages left
                if i + max_concurrent < len(recipients) and delay_seconds > 0:
                    logger.info(f"Bulk WA | job={job_id} | sleeping for {delay_seconds}s before next batch")
                    await asyncio.sleep(delay_seconds)

        # Mark job complete
        now = datetime.now(timezone.utc)
        await asyncio.to_thread(
            db.bulk_jobs.update_one,
            {"job_id": job_id, "org_id": org_id},
            {"$set": {"status": "completed", "updated_at": now, "completed_at": now}}
        )
        logger.success(f"Bulk WA | job={job_id} | complete | org={org_id} | total={len(recipients)}")

    except Exception as e:
        logger.error(f"Bulk WA | job={job_id} | fatal error: {e}")
        await asyncio.to_thread(
            db.bulk_jobs.update_one,
            {"job_id": job_id, "org_id": org_id},
            {"$set": {"status": "failed", "error": str(e), "updated_at": datetime.now(timezone.utc)}}
        )
    finally:
        pass


async def bulk_job_status_generator(job_id: str, org_id: Optional[str] = None):
    """
    Yields the status of a bulk WhatsApp send job over SSE from MongoDB.
    """
    logger.info(f"SSE | Starting status stream for job={job_id}")
    
    poll_interval = 2
    retries = 0
    max_retries = None # Estimated dynamically
    
    while True:
        job = await asyncio.to_thread(get_bulk_job, job_id, org_id=org_id, include_results=False)
        if not job:
            logger.warning(f"SSE | Job not found in MongoDB: {job_id}")
            yield f"data: {json.dumps({'status': 'error', 'message': 'Job not found'})}\n\n"
            return

        # Compute dynamic timeout on first fetch
        if max_retries is None:
            total = job.get("total") or 0
            # Safety buffer: 1 minute per 10 recipients (assuming 20 concurrent sending)
            # plus 15 mins base buffer for connection/overhead.
            estimated_duration = (total / 10) * 60 + 900 
            if estimated_duration < 1200: # Min 20 minutes
                estimated_duration = 1200
            max_retries = int(estimated_duration / poll_interval)
            logger.info(f"SSE | Computed max_retries={max_retries} (~{estimated_duration/60:.1f} min) for job={job_id}")

        # Stream current job state, excluding potentially large per-recipient results
        job_view = {k: v for k, v in job.items() if k != "results"}
        yield f"data: {json.dumps(job_view)}\n\n"

        if job.get("status") in ["completed", "failed"]:
            break

        if retries >= max_retries:
            logger.warning(f"SSE | Job tracking timed out for job={job_id}")
            yield f"data: {json.dumps({'status': 'timeout', 'message': 'Job tracking timed out. Refresh to resume.'})}\n\n"
            break

        await asyncio.sleep(poll_interval)
        retries += 1