# Built-in modules
import httpx
import asyncio
import json
import time
import uuid
import io
import cloudinary.uploader
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson.objectid import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

# Internal modules
from core.managers import manager
from database import get_mongo_db, get_mongo_client
from core.services import services
from config.settings import IG_VERSION, VERSION, WA_VERSION, APP_ID
from schemas.models import InteractiveListMessage, InteractiveReplyButtonMessage, InteractiveUrlButtonMessage, MessageRole
from services.cloudinary_service import upload_media_to_cloudinary
from services.conversation_utils import reopen_conversation
from services.followup_service import abort_scheduled_message
from services.websocket_service import broadcast_main_ws
from services.utils import name_search_prefixes, normalize_phone, phone_search_parts
from services.whatsapp_connection_service import MAX_WHATSAPP_CONNECTIONS, whatsapp_conversation_id
from services.push_notification_service import schedule_customer_message_push
from services.ai_reply_guard import AIProviderUnavailable
from services.whatsapp_reply_idempotency import ReplyOperationConflict, claim_reply, claim_reply_manifest, complete_reply, mark_unknown, record_acceptance, release_claim, start_provider
# from agents.support_agent.ai_features import analyze_conversation_messages, update_product_recommendations
from loguru import logger
from schemas.models import WhatsAppInteractiveMessage

MSG_SPLIT = "|MSG_SPLIT|"
_meta_client: Optional[httpx.AsyncClient] = None


def _meta_http_client() -> httpx.AsyncClient:
    """Reuse Meta transport connections without caching API responses."""
    global _meta_client
    if _meta_client is None or getattr(_meta_client, "is_closed", False):
        _meta_client = httpx.AsyncClient(timeout=30.0)
    return _meta_client


def _is_definitive_meta_rejection(status_code: int) -> bool:
    return 400 <= status_code < 500 and status_code != 408


async def close_meta_http_client() -> None:
    global _meta_client
    if _meta_client is not None and not getattr(_meta_client, "is_closed", False):
        await _meta_client.aclose()
    _meta_client = None


WHATSAPP_PROVIDER_CLEANUP_MAX_ATTEMPTS = 5



db = get_mongo_db()
mongo_client = get_mongo_client()


def _identifier_values(value: Optional[str]) -> list[object]:
    normalized = str(value or "").strip()
    values: list[object] = [normalized]
    if normalized.isdigit():
        values.append(int(normalized))
    return values


def active_whatsapp_connection_candidates(
    entry_wa_id: Optional[str],
    org_id: Optional[str] = None,
) -> list[dict]:
    """Load active legacy and current rows for one Meta WABA."""
    values = _identifier_values(entry_wa_id)
    if not values[0]:
        return []
    query: dict = {
        "is_active": {"$ne": False},
        "$or": [{"wa_id": {"$in": values}}, {"waba_id": {"$in": values}}],
    }
    if org_id:
        query["org_id"] = org_id
    return list(db.whatsapp_connections.find(query).limit(MAX_WHATSAPP_CONNECTIONS + 1))


def resolve_active_whatsapp_connection(
    phone_number_id: Optional[str],
    entry_wa_id: Optional[str],
    org_id: Optional[str] = None,
    *,
    allow_single_fallback: bool = True,
) -> Optional[dict]:
    """Resolve one inbound Meta sender without guessing between multiple numbers."""
    normalized_phone_id = str(phone_number_id or "").strip()
    normalized_wa_id = str(entry_wa_id or "").strip()
    if not normalized_wa_id:
        return None

    candidates = active_whatsapp_connection_candidates(normalized_wa_id, org_id)
    if normalized_phone_id:
        matches = [
            row for row in candidates
            if str(row.get("phone_id") or row.get("phone_number_id") or "").strip() == normalized_phone_id
        ]
        return matches[0] if len(matches) == 1 else None
    if not allow_single_fallback:
        return None
    return candidates[0] if len(candidates) == 1 else None


def resolve_outbound_whatsapp_connection(
    org_id: str,
    wa_id: Optional[str] = None,
    business_phone_id: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the bound sender, or the primary sender for new outbound work."""
    query = {"org_id": org_id, "is_active": {"$ne": False}}
    normalized_wa_id = str(wa_id or "").strip()
    normalized_phone_id = str(business_phone_id or "").strip()
    if normalized_wa_id:
        query["wa_id"] = normalized_wa_id
    if normalized_phone_id:
        matches = list(db.whatsapp_connections.find({
            **query,
            "phone_id": normalized_phone_id,
        }).limit(2))
        return matches[0] if len(matches) == 1 else None

    primary_matches = list(db.whatsapp_connections.find({**query, "slot": 1}).limit(2))
    if primary_matches:
        return primary_matches[0]
    legacy_matches = list(db.whatsapp_connections.find(query).limit(2))
    return legacy_matches[0] if len(legacy_matches) == 1 else None


def broadcast_phone_key(value) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits if 7 <= len(digits) <= 15 else ""


def broadcast_message_preference(content) -> str | None:
    command = "".join(character for character in str(content or "").casefold() if character.isalpha())
    if command in {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}:
        return "opted_out"
    if command in {"start", "subscribe"}:
        return "subscribed"
    return None


def get_broadcast_suppressed_phone_keys(org_id: str, phones: list[str]) -> set[str]:
    keys = {broadcast_phone_key(phone) for phone in phones}
    keys.discard("")
    if not keys:
        return set()
    suppressed = {
        str(item.get("phone") or "")
        for item in db.whatsapp_suppressions.find(
            {"org_id": org_id, "phone": {"$in": list(keys)}},
            {"_id": 0, "phone": 1},
        )
    }
    unchecked = keys - suppressed
    if unchecked:
        conversation_phone = {
            str(item["conversation_id"]): broadcast_phone_key(item.get("customer_id"))
            for item in db[f"conversations_{org_id}"].find(
                {"platform": "whatsapp", "customer_id": {"$in": list(unchecked)}},
                {"_id": 0, "conversation_id": 1, "customer_id": 1},
            )
            if item.get("conversation_id")
        }
        conversation_phone.update({f"whatsapp_{phone}": phone for phone in unchecked})
        preference_messages = db[f"messages_{org_id}"].aggregate([
            {"$match": {
                "conversation_id": {"$in": list(conversation_phone)},
                "role": "customer",
                "mode": {"$ne": "private"},
                "content": {"$regex": r"^\s*(?:stop(?:\s*all)?|unsubscribe|cancel|end|quit|start|subscribe)\s*[.!]?\s*$", "$options": "i"},
            }},
            {"$sort": {"timestamp": -1}},
            {"$group": {"_id": "$conversation_id", "content": {"$first": "$content"}}},
        ])
        now = datetime.now(timezone.utc)
        operations = []
        for item in preference_messages:
            phone = conversation_phone.get(str(item.get("_id") or ""), "")
            if phone and broadcast_message_preference(item.get("content")) == "opted_out":
                suppressed.add(phone)
                operations.append(UpdateOne(
                    {"org_id": org_id, "phone": phone},
                    {"$set": {"reason": "customer_opt_out", "source": "message_history", "updated_at": now}, "$setOnInsert": {"created_at": now}},
                    upsert=True,
                ))
        if operations:
            db.whatsapp_suppressions.bulk_write(operations, ordered=False)
    return suppressed


def is_broadcast_suppressed(org_id: str, phone: str) -> bool:
    key = broadcast_phone_key(phone)
    return bool(key and db.whatsapp_suppressions.find_one({"org_id": org_id, "phone": key}, {"_id": 1}))


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
    extra_updates: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
    business_phone_id: Optional[str] = None,
    business_phone: Optional[str] = None,
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
    # Canonical normalization to prevent duplicate conversations
    customer_phone_no = normalize_phone(customer_phone_no)
    
    phone_digits, phone_search_suffixes = phone_search_parts(customer_phone_no)
    now = datetime.now(timezone.utc)
    
    # Get the WhatsApp-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]
    
    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({
        "platform": "whatsapp",
        "customer_id": customer_phone_no,
        **({"business_phone_id": str(business_phone_id)} if business_phone_id else {}),
    })
    if not existing_conversation and business_phone_id:
        primary = resolve_active_whatsapp_connection(
            business_phone_id, str(recipient_id or ""), org_id, allow_single_fallback=False,
        )
        primary_phone_id = str((primary or {}).get("phone_id") or (primary or {}).get("phone_number_id") or "")
        if primary_phone_id == str(business_phone_id) and (primary or {}).get("slot") == 1:
            existing_conversation = conversations_collection.find_one({
                "platform": "whatsapp",
                "customer_id": customer_phone_no,
                "$or": [
                    {"business_phone_id": {"$exists": False}},
                    {"business_phone_id": None},
                ],
            })
    conversation_id = (
        existing_conversation.get("conversation_id")
        if existing_conversation
        else whatsapp_conversation_id(customer_phone_no, business_phone_id)
    )
    if (
        idempotency_key
        and existing_conversation
        and idempotency_key in (existing_conversation.get("applied_outbound_message_ids") or [])
    ):
        return conversation_id
    convo_key = (org_id, conversation_id)
    
    # Check if the viewers set exists and is not empty
    is_being_viewed = len(services.org_convo_viewers.get(org_id, {}).get(conversation_id, set())) > 0

    # Handle Name Resolution
    final_customer_name = customer_name
    
    # Check if the contact's name was locked by a VCF upload.
    # If locked, we must use the VCF name regardless of what Meta sends.
    contacts_col = db[f"contacts_{org_id}"]
    contact_doc = contacts_col.find_one(
        {"phone_number": customer_phone_no},
        {"name_locked_by_vcf": 1, "full_name": 1, "_id": 0}
    )
    is_name_locked_by_vcf = contact_doc.get("name_locked_by_vcf", False) if contact_doc else False

    if is_name_locked_by_vcf and contact_doc.get("full_name"):
        # VCF name takes absolute priority — never overwrite
        final_customer_name = contact_doc["full_name"]
    elif existing_conversation:
        stored_name = existing_conversation.get("customer_name")
        meta_verified = existing_conversation.get("meta_name_verified", False)
        
        if last_sender == "customer":
            # Webhook update: Always trust Meta's name and mark as verified
            final_customer_name = customer_name
        else:
            # Automation/Agent update: Do NOT overwrite a verified name or an already existing valid name
            if meta_verified:
                final_customer_name = stored_name
            elif stored_name and stored_name != customer_phone_no:
                final_customer_name = stored_name

    # Base fields that ALWAYS get updated
    update_set = {
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "whatsapp_id": recipient_id,
        "customer_id": customer_phone_no,
        "phone_digits": phone_digits,
        "phone_search_suffixes": phone_search_suffixes,
        "customer_name": final_customer_name,
        "customer_name_prefixes": name_search_prefixes(final_customer_name),
        "last_message": last_message,
        "last_message_timestamp": now,
        "last_message_is_private_note": mode == "private",
        "last_sender": last_sender,
        "status": existing_conversation.get("status", "open") if is_auto_followup and existing_conversation else "open",
    }
    if business_phone_id:
        update_set["business_phone_id"] = str(business_phone_id)
    if business_phone:
        update_set["business_phone"] = str(business_phone)
    
    if extra_updates:
        update_set.update(extra_updates)
    
    if last_sender == "customer":
        update_set["meta_name_verified"] = True

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
    if existing_conversation and not is_auto_followup:
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
    if idempotency_key:
        mongo_update["$addToSet"] = {"applied_outbound_message_ids": idempotency_key}

    # Safely apply $max ONLY if we have a valid expiry from the customer
    if last_sender == "customer" and reply_window_expiry:
        mongo_update["$max"] = {"reply_window_ends_at": reply_window_expiry}
        update_set["is_ad_referral"] = is_ad_referral 

    # Execute atomic update
    update_filter = {"conversation_id": conversation_id}
    if idempotency_key:
        update_filter["applied_outbound_message_ids"] = {"$ne": idempotency_key}
    try:
        updated_conversation = conversations_collection.find_one_and_update(
            update_filter,
            mongo_update,
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        if idempotency_key and conversations_collection.find_one(
            {"conversation_id": conversation_id, "applied_outbound_message_ids": idempotency_key},
            {"_id": 1},
        ):
            return conversation_id
        raise

    logger.success("Whatsapp conversation updated: org=...{} conversation=...{}", org_id[-6:], conversation_id[-6:])

    # Format the timestamp for JSON serialization
    broadcast_conversation = dict(updated_conversation)
    if "last_message_timestamp" in broadcast_conversation:
        broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
    if broadcast_conversation.get("next_action_at") is not None:
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
    recipient_id: str,
    role: MessageRole,
    mode: Optional[str] = None,
    sender_name: Optional[str] = "Agent",
    message_id: Optional[str] = None,
    raw_data: Optional[dict] = None,
    status: Optional[str] = "received",
    temp_id: Optional[str] = None,
    context_type: Optional[str] = None,
    context: Optional[dict] = None,
    bot_context: Optional[dict] = None,
    notify_push: bool = False,
    business_phone_id: Optional[str] = None,
    delivery_error: Optional[dict] = None,
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
        "ai_metadata": bot_context if bot_context else None,
        "business_phone_id": str(business_phone_id) if business_phone_id else None,
        "delivery_error": delivery_error,
    }   
    
    # Store message in WhatsApp-specific collection
    try:
        messages_collection.insert_one(message)
    except DuplicateKeyError:
        existing = messages_collection.find_one({"message_id": message_id})
        immutable_fields = ("platform", "conversation_id", "content", "type", "sender_id", "role", "mode")
        if existing and all(existing.get(field) == message.get(field) for field in immutable_fields):
            if notify_push and role == MessageRole.CUSTOMER and mode != "private":
                await schedule_customer_message_push(
                    org_id=org_id,
                    platform="whatsapp",
                    message_id=message_id,
                    conversation_id=conversation_id,
                    sender_name=sender_name,
                    content=content,
                    message_type=type,
                )
            return str(existing.get("id") or existing["message_id"])
        raise

    logger.success(f"Whatsapp message stored: {message_id}")

    if business_phone_id and role == MessageRole.CUSTOMER:
        received_at = datetime.now(timezone.utc)
        db.whatsapp_connections.update_one(
            {
                "org_id": org_id,
                "$or": [
                    {"phone_id": str(business_phone_id)},
                    {"phone_number_id": str(business_phone_id)},
                ],
                "is_active": {"$ne": False},
            },
            {
                "$set": {
                    "webhook_status": "healthy",
                    "last_webhook_at": received_at,
                    "updated_at": received_at,
                },
                "$unset": {"webhook_error": ""},
            },
        )

    if notify_push and role == MessageRole.CUSTOMER and mode != "private":
        await schedule_customer_message_push(
            org_id=org_id,
            platform="whatsapp",
            message_id=message_id,
            conversation_id=conversation_id,
            sender_name=sender_name,
            content=content,
            message_type=type,
        )
    
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
    
    # Format message for WebSocket broadcast
    broadcast_message = {
        "id": message_id,
        "platform": "whatsapp",
        "conversation_id": conversation_id,
        "content": content,
        "sender_id": customer_phone_no,
        "role": role,
        "timestamp": message["timestamp"].isoformat(),
        "status": status,
        "sender_name": sender_name,
        "type": type,
        "temp_id": temp_id,
        "payload": payload,
        "mode": mode,
        "context_type": context_type,
        "context": context,
        "ai_metadata": bot_context if bot_context else None,
        "business_phone_id": str(business_phone_id) if business_phone_id else None,
        "delivery_error": delivery_error,
    }

    await broadcast_main_ws(
        platform_id=recipient_id,
        platform_type="whatsapp",
        event_type="new_message",
        payload={"message": broadcast_message}
    )
    
    # Broadcast to WhatsApp-specific WebSocket connections (legacy)
    # This was causing double-broadcasts to clients already listening on main-ws
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


async def update_whatsapp_media_message(
    org_id: str,
    message_id: str,
    payload: dict,
    recipient_id: str,
    status: str = "received",
) -> Optional[str]:
    """Complete or fail an already-stored incoming media message."""
    message = db[f"messages_{org_id}"].find_one_and_update(
        {"message_id": message_id, "platform": "whatsapp"},
        {"$set": {"payload": payload, "status": status}},
        return_document=ReturnDocument.AFTER,
    )
    if not message:
        logger.error(f"WhatsApp media placeholder missing: {message_id}")
        return None

    broadcast_message = {
        key: message.get(key)
        for key in (
            "id", "platform", "conversation_id", "content", "sender_id", "role",
            "status", "sender_name", "type", "temp_id", "payload", "mode",
            "context_type", "context",
        )
    }
    timestamp = message.get("timestamp")
    broadcast_message["timestamp"] = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    await broadcast_main_ws(
        platform_id=recipient_id,
        platform_type="whatsapp",
        event_type="new_message",
        payload={"message": broadcast_message},
    )
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
        sender_name: Optional[str] = "Agent",
        is_auto_followup: bool = False,
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
    from services.entitlement_service import require_feature
    send_started_at = time.perf_counter()
    require_feature(org_id, "CHANNELS")
    whatsapp_id = message.get("sender_id")
    outbound_operation = None
    active_operation = None
    provider_started = False
    delivery_unknown = False
    quota_event_key = None
    quota_consumed = False
    had_replay = False
    manifest_action_job_id = None

    def mark_active_delivery_unknown() -> None:
        try:
            mark_unknown(
                db.whatsapp_outbound_sends,
                active_operation or outbound_operation,
            )
        except Exception:
            logger.exception("Could not persist uncertain WhatsApp delivery state")

    def claim_outbound_reply(claim_temp_id: str | None, content: str):
        try:
            return claim_reply(
                db.whatsapp_outbound_sends,
                org_id,
                claim_temp_id,
                message["conversation_id"],
                mode,
                [content],
            )
        except ReplyOperationConflict as exc:
            if mode == "ai":
                if exc.status == "claimed":
                    raise AIProviderUnavailable(
                        "outbound_in_progress", escalation_required=False
                    ) from exc
                raise AIProviderUnavailable(
                    "delivery_unknown"
                    if exc.status in {"provider_pending", "delivery_unknown"}
                    else "delivery_sync_failed"
                ) from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not whatsapp_id:
        logger.error("Missing 'sender_id' in outbound WhatsApp message payload")
        raise HTTPException(status_code=422, detail="Missing 'sender_id'")
        
    try:
        conversation_id = message["conversation_id"]
        conversation = db[f"conversations_{org_id}"].find_one({
            "conversation_id": conversation_id,
            "platform": "whatsapp",
        })
        if not conversation:
            raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
        conversation_wa_id = str(conversation.get("whatsapp_id") or whatsapp_id)
        if str(whatsapp_id) != conversation_wa_id:
            raise HTTPException(status_code=409, detail="The selected sender does not own this conversation")
        connection = resolve_outbound_whatsapp_connection(
            org_id,
            conversation_wa_id,
            conversation.get("business_phone_id"),
        )
        if not connection:
            raise HTTPException(status_code=409, detail="The WhatsApp number for this conversation is not connected")
        whatsapp_business_id = connection["wa_id"]
        original_content = message["content"]
        pricing_source_message_id = message.get("pricing_source_message_id")
        pricing_audit_active = False
        pricing_guard_active = False

        if mode == "ai":
            from services.pricing_decision_service import get_pricing_settings
            from services.pricing_service import (
                is_pricing_enabled,
                validate_ai_pricing_response,
            )

            try:
                pricing_mode = str((await get_pricing_settings(org_id)).get("mode") or "off")
            except Exception as exc:
                pricing_mode = "review" if is_pricing_enabled(org_id) else "off"
                logger.error(
                    f"Pricing settings lookup failed for org={org_id}; "
                    f"using {pricing_mode} fallback: {exc}"
                )
            pricing_audit_active = pricing_mode in {"shadow", "review"}
            pricing_guard_active = pricing_mode == "review"

            validation_text = (
                "\n".join(str(part) for part in original_content)
                if isinstance(original_content, list)
                else str(original_content)
            )
            if pricing_audit_active:
                try:
                    valid, reason = await validate_ai_pricing_response(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        source_message_id=pricing_source_message_id,
                        content=validation_text,
                    )
                    if pricing_guard_active and not valid:
                        logger.warning(
                            f"Blocked unsafe AI price for org={org_id}, "
                            f"conversation=...{conversation_id[-6:]}, reason={reason}"
                        )
                        raise AIProviderUnavailable(reason, escalation_required=False)
                except AIProviderUnavailable as exc:
                    if pricing_guard_active:
                        raise
                    logger.error(f"Shadow AI pricing validation could not complete for org={org_id}: {exc}")
                except Exception as exc:
                    if pricing_guard_active:
                        logger.error(f"AI pricing validation failed closed for org={org_id}: {exc}")
                        raise AIProviderUnavailable("catalogue_unavailable") from exc
                    logger.error(f"Shadow AI pricing validation failed open for org={org_id}: {exc}")
        
        recipient_id = message.get("recipient_override") or (conversation or {}).get("customer_id") or conversation_id.replace("whatsapp_", "").replace("dealer_", "")

        # Handle multi-part messages (e.g. lists returned by the agent or strings with MSG_SPLIT)
        if isinstance(original_content, list):
            contents = [str(m).strip() for m in original_content if str(m).strip()]
        elif isinstance(original_content, str) and MSG_SPLIT in original_content:
            contents = [m.strip() for m in original_content.split(MSG_SPLIT) if m.strip()]
        else:
            contents = [str(original_content)]

        if mode == "ai":
            if not all(content.strip() for content in contents):
                raise AIProviderUnavailable("empty_response")
            if any(len(content) > 4096 for content in contents):
                raise AIProviderUnavailable("response_too_long")

        if not contents:
            logger.warning(f"Empty content provided for WhatsApp message to {recipient_id}")
            return {"message": "No content to send"}

        try:
            manifest = claim_reply_manifest(
                db.whatsapp_outbound_sends,
                org_id,
                temp_id,
                conversation_id,
                mode,
                contents,
                str((bot_context or {}).get("sales_action_job_id") or "") or None,
            )
        except ReplyOperationConflict as exc:
            if mode == "ai":
                raise AIProviderUnavailable("delivery_sync_failed") from exc
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        contents = list(manifest["contents"])
        manifest_action_job_id = manifest.get("sales_action_job_id")

        outbound_operation = (
            claim_outbound_reply(temp_id, contents[0])
            if len(contents) == 1 else None
        )
        if outbound_operation and outbound_operation.get("action") == "replay":
            return {**outbound_operation["result"], "outbound_replay": True}

        if mode == "ai":
            from services.ai_policy_service import is_effective_ai_enabled
            from services.entitlement_service import reserve_quota_async

            if not await is_effective_ai_enabled(org_id, conversation_id):
                raise AIProviderUnavailable("ai_disabled", escalation_required=False)
            operation_id = str((outbound_operation or {}).get("_id") or temp_id or pricing_source_message_id or conversation_id)
            quota_event_key = f"ai:{operation_id}"
            await reserve_quota_async(org_id, "AI", "monthly_messages", len(contents), quota_event_key)
            quota_consumed = True

        # Ensure the conversation document exists in the database
        if mode != "ai":
            await create_or_update_whatsapp_conversation(
                org_id=org_id,
                recipient_id=whatsapp_business_id,
                customer_phone_no=recipient_id,
                customer_name=message.get("customer_name") or recipient_id,
                last_message=contents[0],
                last_sender="agent",
                mode=mode,
                is_auto_followup=is_auto_followup,
            )

        last_stored_id = None
        all_message_ids = []
        first_meta_accepted_ms = None

        # Send each content part as a separate bubble
        for i, content_item in enumerate(contents):
            provider_started = False
            part_operation = outbound_operation
            if len(contents) > 1:
                part_temp_id = f"{temp_id}:part:{i}" if temp_id else None
                part_operation = claim_outbound_reply(part_temp_id, content_item)
            active_operation = part_operation
            part_cached = part_operation and part_operation.get("action") == "replay"
            had_replay = had_replay or bool(
                part_cached or (part_operation and part_operation.get("action") == "resume")
            )
            if pricing_audit_active:
                try:
                    valid, reason = await validate_ai_pricing_response(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        source_message_id=pricing_source_message_id,
                        content=content_item,
                    )
                except Exception as exc:
                    if pricing_guard_active:
                        logger.error(f"Final AI pricing revalidation failed closed for org={org_id}: {exc}")
                        raise AIProviderUnavailable("catalogue_unavailable") from exc
                    logger.error(f"Final shadow AI pricing revalidation failed open for org={org_id}: {exc}")
                    valid, reason = True, "shadow_validation_error"
                if pricing_guard_active and not valid:
                    raise AIProviderUnavailable(reason, escalation_required=False)

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

            preview_url = message.get("preview_url")
            if preview_url:
                data["preview_url"] = preview_url

            if mode == "reply" or mode == "ai":
                # Send message via WhatsApp Cloud API
                url = f"https://graph.facebook.com/{IG_VERSION}/{connection['phone_id']}/messages"
                headers = {
                    "Authorization": f"Bearer {connection['access_token']}",
                    "Content-Type": "application/json",
                }
                
                if part_cached:
                    cached_result = part_operation.get("result") or {}
                    response_data = cached_result.get("response_data") or part_operation.get("response_data") or {}
                    message_id = cached_result.get("message_id") or part_operation.get("provider_message_id")
                elif part_operation and part_operation.get("action") == "resume":
                    response_data = part_operation.get("response_data") or {}
                    message_id = part_operation.get("provider_message_id")
                else:
                    if mode == "ai":
                        from services.ai_policy_service import is_effective_ai_enabled

                        if not await is_effective_ai_enabled(org_id, conversation_id):
                            raise AIProviderUnavailable("ai_disabled", escalation_required=False)
                    if not start_provider(db.whatsapp_outbound_sends, part_operation):
                        raise HTTPException(status_code=409, detail="This reply is already being sent")
                    provider_started = True
                    try:
                        provider_started_at = time.perf_counter()
                        response = await _meta_http_client().post(
                            url, headers=headers, json=data, timeout=30.0
                        )
                    except httpx.RequestError:
                        delivery_unknown = True
                        mark_unknown(db.whatsapp_outbound_sends, part_operation)
                        raise AIProviderUnavailable("delivery_unknown")

                    if response.status_code != 200:
                        if not _is_definitive_meta_rejection(response.status_code):
                            delivery_unknown = True
                            mark_unknown(db.whatsapp_outbound_sends, part_operation)
                            raise AIProviderUnavailable(
                                "delivery_unknown", upstream_status=response.status_code
                            )
                        if quota_consumed and quota_event_key:
                            from services.entitlement_service import release_quota_async
                            await release_quota_async(org_id, "monthly_messages", quota_event_key)
                            quota_consumed = False
                        release_claim(
                            db.whatsapp_outbound_sends,
                            part_operation,
                            provider_started=True,
                        )
                        provider_started = False
                        if mode == "ai" and temp_id and temp_id.startswith("ai:"):
                            services.processed_message_ids.pop(temp_id.removeprefix("ai:"), None)
                        logger.error("WhatsApp API rejected outbound message with status {}", response.status_code)
                        if mode == "ai":
                            raise AIProviderUnavailable(
                                "delivery_error",
                                upstream_status=response.status_code,
                            )
                        raise HTTPException(
                            status_code=response.status_code,
                            detail=f"Failed to send WhatsApp message part {i+1}"
                        )

                    response_data = response.json()
                    message_id = response_data.get("messages", [{}])[0].get("id")
                    if not message_id:
                        delivery_unknown = True
                        mark_unknown(db.whatsapp_outbound_sends, part_operation)
                        raise AIProviderUnavailable("delivery_unknown")
                    provider_accepted_ms = round(
                        (time.perf_counter() - provider_started_at) * 1000
                    )
                    if first_meta_accepted_ms is None:
                        first_meta_accepted_ms = round(
                            (time.perf_counter() - send_started_at) * 1000
                        )
                    logger.info(
                        "Meta accepted outbound request for ...{} in {}ms",
                        conversation_id[-6:],
                        provider_accepted_ms,
                    )
                    if part_operation:
                        accepted_operation = record_acceptance(db.whatsapp_outbound_sends, part_operation, message_id, response_data)
                        if not accepted_operation:
                            delivery_unknown = True
                            mark_unknown(db.whatsapp_outbound_sends, part_operation)
                            raise AIProviderUnavailable("delivery_unknown")
                        part_operation = accepted_operation
                        active_operation = part_operation
            else: 
                message_id = f"privatenote_{uuid.uuid4()}"
                response_data = {"status": "Private note created, no API call made"}
            
            all_message_ids.append(message_id)
            now = datetime.now(timezone.utc)

            if mode == "ai":
                try:
                    from services.entitlement_service import commit_quota_async, record_ai_usage_async

                    await commit_quota_async(org_id, "monthly_messages", quota_event_key, str(i))

                    usage = message.get("ai_usage") if isinstance(message.get("ai_usage"), dict) else {}
                    await record_ai_usage_async(
                        org_id,
                        f"{quota_event_key or f'ai:{message_id}'}:{i}",
                        "whatsapp",
                        1,
                        int(usage.get("input_tokens", 0)) if i == 0 else 0,
                        int(usage.get("output_tokens", 0)) if i == 0 else 0,
                        int(usage.get("recorded_cost_microusd", 0)) if i == 0 else 0,
                    )
                except Exception as exc:
                    logger.error(f"Could not record WhatsApp AI analytics for org={org_id}: {exc}")

            if mode == "private":
                last_message = f"Private Note: {content_item}"
            elif mode == "ai":
                last_message= f"AI: {content_item}"
            else:
                last_message = f"{sender_name}: {content_item}"

            wa_conversation = {
                "last_message": last_message,
                "last_message_timestamp": now,
                "last_sender": "ai" if mode == "ai" else "agent" ,
                "reminder_stage": 0,
                "next_action_at": None if is_auto_followup else now + timedelta(hours=6),
                "last_message_is_private_note": mode == "private"
            }

            # Update conversation for each part (last one will reflect the final state)
            db[f"conversations_{org_id}"].find_one_and_update(
                {"conversation_id": conversation_id},
                {"$set": wa_conversation},
                return_document=ReturnDocument.AFTER  
            )

            # Store message in WhatsApp-specific collection
            stored_bot_context = dict(bot_context or {})
            if pricing_source_message_id:
                stored_bot_context["pricing_source_message_id"] = pricing_source_message_id
            stored_message = db[f"messages_{org_id}"].find_one({"message_id": message_id}) if part_operation else None
            if stored_message:
                last_stored_id = stored_message.get("id") or stored_message.get("message_id")
            else:
                try:
                    last_stored_id = await store_whatsapp_message(
                        org_id=org_id,
                        content=content_item,
                        conversation_id=conversation_id,
                        customer_phone_no=recipient_id,
                        type="text",
                        payload=None,
                        sender_name="AI" if mode == "ai" else (sender_name),
                        recipient_id=whatsapp_business_id,
                        role=MessageRole.AI if mode == "ai" else MessageRole.AGENT,
                        mode=mode,
                        message_id=message_id,
                        raw_data=response_data,
                        status="delivered" if mode == "private" else "sending",
                        temp_id=temp_id,
                        context=context,
                        context_type=context_type,
                        bot_context=stored_bot_context or None
                    )
                except Exception as exc:
                    stored_message = db[f"messages_{org_id}"].find_one({"message_id": message_id}) if part_operation else None
                    if not stored_message:
                        if mode == "ai" and part_operation:
                            raise AIProviderUnavailable("delivery_sync_failed") from exc
                        raise
                    last_stored_id = stored_message.get("id") or stored_message.get("message_id")

            if len(contents) > 1 and not complete_reply(db.whatsapp_outbound_sends, part_operation, {"message_id": message_id, "response_data": response_data}):
                existing_part = db.whatsapp_outbound_sends.find_one({"_id": part_operation["_id"], "org_id": org_id}) if part_operation else None
                if not existing_part or existing_part.get("status") != "completed":
                    if mode == "ai":
                        raise AIProviderUnavailable("delivery_sync_failed")
                    raise HTTPException(status_code=409, detail="A message bubble was delivered and is still being synchronized. Refresh the conversation.")

            if pricing_audit_active:
                try:
                    from services.pricing_service import record_accepted_ai_response

                    await record_accepted_ai_response(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        inbound_message_id=pricing_source_message_id,
                        outbound_message_id=message_id,
                        content=content_item,
                    )
                except Exception as exc:
                    logger.error(f"Failed to record accepted AI pricing message: {exc}")

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

        result = {
            "message": "Messages sent",
            "message_ids": all_message_ids,
            "message_id": last_stored_id,
            "delivery_state": "sent",
            "local_sync_pending": False,
        }
        if first_meta_accepted_ms is not None:
            result["product_timings"] = {
                "first_meta_accepted_ms": first_meta_accepted_ms,
            }
        if had_replay:
            result["outbound_replay"] = True
        if manifest_action_job_id:
            result["sales_action_job_id"] = manifest_action_job_id
        if len(contents) == 1 and not complete_reply(db.whatsapp_outbound_sends, outbound_operation, result):
            existing_operation = db.whatsapp_outbound_sends.find_one({"_id": outbound_operation["_id"], "org_id": org_id}) if outbound_operation else None
            if existing_operation and existing_operation.get("status") == "completed" and isinstance(existing_operation.get("result"), dict):
                return {**existing_operation["result"], "outbound_replay": True}
            if mode == "reply" and last_stored_id:
                return {
                    **result,
                    "message": "Message accepted; delivery status is still synchronizing",
                    "delivery_state": "accepted_pending_sync",
                    "local_sync_pending": True,
                }
            if mode == "ai":
                raise AIProviderUnavailable("delivery_sync_failed")
            raise HTTPException(status_code=409, detail="The reply was delivered and is still being synchronized. Refresh the conversation.")
        return result

    except AIProviderUnavailable:
        if quota_consumed and quota_event_key and not delivery_unknown and not provider_started:
            from services.entitlement_service import release_quota_async
            await release_quota_async(org_id, "monthly_messages", quota_event_key)
        if provider_started:
            mark_active_delivery_unknown()
        else:
            release_claim(db.whatsapp_outbound_sends, active_operation or outbound_operation)
        raise
    except asyncio.CancelledError:
        if quota_consumed and quota_event_key and not delivery_unknown and not provider_started:
            from services.entitlement_service import release_quota_async
            await release_quota_async(org_id, "monthly_messages", quota_event_key)
        if provider_started:
            mark_active_delivery_unknown()
        else:
            release_claim(db.whatsapp_outbound_sends, active_operation or outbound_operation)
        raise
    except HTTPException as e:
        if quota_consumed and quota_event_key and not delivery_unknown and not provider_started:
            from services.entitlement_service import release_quota_async
            await release_quota_async(org_id, "monthly_messages", quota_event_key)
        if provider_started:
            mark_active_delivery_unknown()
            raise AIProviderUnavailable("delivery_unknown") from e
        release_claim(db.whatsapp_outbound_sends, active_operation or outbound_operation)
        raise e
    except Exception as e:
        if quota_consumed and quota_event_key and not delivery_unknown and not provider_started:
            from services.entitlement_service import release_quota_async
            await release_quota_async(org_id, "monthly_messages", quota_event_key)
        if provider_started:
            mark_active_delivery_unknown()
            raise AIProviderUnavailable("delivery_unknown") from e
        release_claim(db.whatsapp_outbound_sends, active_operation or outbound_operation)
        logger.error(f"Error sending WhatsApp message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send message: {str(e)}"
        )

async def send_whatsapp_template_message(recipient_id: str, template: dict, wa_id: str, org_id: str, temp_id: Optional[str], mode: str, context_type: Optional[str], context: Optional[dict], sender_name: Optional[str] = "Agent", conversation_id: Optional[str] = None) -> dict:
    """
    Send a template message via WhatsApp Cloud API
    """
    try:
        conversation = db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}) if conversation_id else None
        connection = resolve_outbound_whatsapp_connection(
            org_id,
            (conversation or {}).get("whatsapp_id") or wa_id,
            (conversation or {}).get("business_phone_id"),
        )

        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")
        if not conversation:
            conversation = db[f"conversations_{org_id}"].find_one({
                "platform": "whatsapp",
                "customer_id": normalize_phone(recipient_id),
                "business_phone_id": connection.get("phone_id"),
            })
        resolved_conversation_id = (conversation or {}).get("conversation_id") or whatsapp_conversation_id(
            recipient_id,
            connection.get("phone_id"),
        )

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
            "last_message": f"Private Note: Shared a template: {template_name}" if mode == "private" else f"{sender_name}: Shared a template: {template_name}",
            "last_message_timestamp": now,
            "last_sender": "agent",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "last_message_is_private_note": mode == "private"
        }
        if mode != "private":
            wa_conversation["needs_reply"] = False
        wa_conversation.update({
            "platform": "whatsapp",
            "conversation_id": resolved_conversation_id,
            "whatsapp_id": connection.get("wa_id"),
            "business_phone_id": connection.get("phone_id"),
            "business_phone": connection.get("phone"),
            "customer_id": normalize_phone(recipient_id),
            "status": "open",
        })

        conversation = db[f"conversations_{org_id}"].find_one_and_update(
            {"conversation_id": resolved_conversation_id},
            {
                "$set": wa_conversation,
                "$setOnInsert": {
                    "customer_name": normalize_phone(recipient_id),
                    "is_ai_enabled": True,
                    "started_at": now,
                    "created_at": now,
                    "unread_count": 0,
                    "closed_at": None,
                    "message_counter": 0,
                },
            },
            upsert=True,
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
            conversation_id=resolved_conversation_id,
            customer_phone_no=recipient_id,
            type="template",
            payload=template,
            sender_name=sender_name,
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
        context: Optional[dict],
        sender_name: Optional[str] = "Agent",
        media_kind: Optional[str] = None,
        duration_ms: Optional[int] = None,
        mime_type: Optional[str] = None,
        file_name: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ) -> dict:
    """
    Send a media message via WhatsApp Cloud API
    """
    try:
        existing_conversation = (
            db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
            if conversation_id else None
        )
        connection = resolve_outbound_whatsapp_connection(
            org_id,
            (existing_conversation or {}).get("whatsapp_id") or wa_id,
            (existing_conversation or {}).get("business_phone_id"),
        )

        api_media_type = media_type if media_type in {"image", "video", "audio", "document"} else "document"
        media_payload = {
            "link": media_url,
        }
        if content and api_media_type in {"image", "video", "document"}:
            media_payload["caption"] = content
        if media_kind == "voice_note" and api_media_type == "audio" and (mime_type or "").startswith("audio/ogg") and "opus" in (mime_type or ""):
            media_payload["voice"] = True
        if file_name and api_media_type == "document":
            media_payload["filename"] = file_name

        data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": api_media_type,
            api_media_type: media_payload
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
            "last_message": f"Private Note: {content if content else 'Shared an attachment'}" if mode == "private" else f"{sender_name}: {content if content else ('Sent a voice note' if media_kind == 'voice_note' else 'Shared an attachment')}",
            "last_message_timestamp": now,
            "last_sender": "agent",
            "reminder_stage": 0,
            "next_action_at": now + timedelta(hours=6),
            "last_message_is_private_note": mode == "private"
        }
        if mode != "private":
            wa_conversation["needs_reply"] = False

        stored_conversation_id = conversation_id or whatsapp_conversation_id(
            recipient_id,
            connection.get("phone_id"),
        )
        wa_conversation.update({
            "platform": "whatsapp",
            "conversation_id": stored_conversation_id,
            "whatsapp_id": connection.get("wa_id"),
            "business_phone_id": connection.get("phone_id"),
            "business_phone": connection.get("phone"),
            "customer_id": normalize_phone(recipient_id),
            "status": "open",
        })
        conversation = db[f"conversations_{org_id}"].find_one_and_update(
            {"conversation_id": stored_conversation_id},
            {
                "$set": wa_conversation,
                "$setOnInsert": {
                    "customer_name": normalize_phone(recipient_id),
                    "is_ai_enabled": True,
                    "started_at": now,
                    "created_at": now,
                    "unread_count": 0,
                    "closed_at": None,
                    "message_counter": 0,
                },
            },
            upsert=True,
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

        mid = await store_whatsapp_message(
            org_id=org_id,
            content=content,
            conversation_id=stored_conversation_id,
            customer_phone_no=recipient_id,
            type="voice" if media_kind == "voice_note" else api_media_type,
            payload={
                "url": media_url,
                "caption": content,
                "mime_type": mime_type,
                "file_name": file_name,
                "duration_ms": duration_ms,
                "media_kind": media_kind,
            },
            sender_name=sender_name,
            recipient_id=connection["wa_id"],
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

async def upload_and_send_wa_media_message(recipient_id: str, file: any, wa_id: str, org_id: str, content: Optional[str], temp_id: Optional[str], mode: str, context_type: Optional[str], context: Optional[dict], sender_name: Optional[str] = "Agent", media_kind: Optional[str] = None, duration_ms: Optional[int] = None, conversation_id: Optional[str] = None) -> dict:
    """
    Upload media to Cloudinary and send it as a media message
    """
    try:
        # Upload media to Cloudinary
        result = await upload_media_to_cloudinary(
            file, platform="whatsapp", org_id=org_id, media_kind=media_kind
        )

        if not result:
            return {"error": "Media upload failed", "success": False}
        media_url = result.get("file_url")
        media_type = result.get("file_type")

        if not media_url:
            logger.error("Media upload failed, cannot send WhatsApp media message.")
            return {"error": "Media upload failed", "success": False}
        
        # Send the media message via WhatsApp API
        response = await send_wa_media_message(
            recipient_id, media_url, media_type, wa_id, content, org_id, temp_id,
            mode, context_type, context, sender_name, media_kind, duration_ms,
            result.get("mime_type"), result.get("file_name"), conversation_id,
        )
        
        return response
    except Exception as e:
        logger.error(f"Error in upload_and_send_wa_media_message: {e}")
        return {"error": str(e), "success": False}
 
async def update_whatsapp_message_status(
    org_id: str,
    message_id: str,
    status: str,
    delivery_error: Optional[dict] = None,
):
    """
    Update the status of a WhatsApp message in the database
    """
    try:
        messages_collection = db[f"messages_{org_id}"]

        status = str(status).lower()
        rank = {"sending": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}.get(status)
        status_filter = {"message_id": message_id, "platform": "whatsapp"}
        if rank is not None:
            rank_limit = 2 if status == "failed" else rank
            status_filter["$or"] = [
                {"delivery_rank": {"$lt": rank_limit}},
                {"delivery_rank": {"$exists": False}},
            ]
        update = {"$set": {"status": status, **({"delivery_rank": rank} if rank is not None else {})}}
        if status == "failed" and delivery_error:
            update["$set"]["delivery_error"] = delivery_error
        elif status in {"sent", "delivered", "read"}:
            update["$unset"] = {"delivery_error": ""}

        result = messages_collection.find_one_and_update(
            status_filter,
            update,
            return_document=ReturnDocument.AFTER
        )
        
        if result:
            logger.success(f"WhatsApp message {message_id} status updated to {status}")
            return result
        return None
    
    except Exception as e:
        logger.error(f"Error updating WhatsApp message status: {e}")

async def archive_whatsapp_data(org_id: str, phone_id: str, expire_days=30):
    """Archive only the conversations owned by one WhatsApp phone number."""
    archive_id = None
    try:
        connection = db.whatsapp_connections.find_one({
            "org_id": org_id, "phone_id": phone_id, "is_active": {"$ne": False},
        })
        if not connection:
            raise HTTPException(status_code=404, detail="WhatsApp connection not found")

        now = datetime.now(timezone.utc)
        conversations_collection_name = f"conversations_{org_id}"
        messages_collection_name = f"messages_{org_id}"
        private_notes_collection_name = f"private_notes_{org_id}"
        active_count = db.whatsapp_connections.count_documents({
            "org_id": org_id, "wa_id": connection["wa_id"], "is_active": {"$ne": False},
        })
        phone_filter = {"platform": "whatsapp", "business_phone_id": phone_id}
        if active_count == 1:
            phone_filter = {
                "platform": "whatsapp",
                "$or": [
                    {"business_phone_id": phone_id},
                    {"business_phone_id": {"$exists": False}},
                    {"business_phone_id": None},
                ],
            }

        with mongo_client.start_session() as session:
            with session.start_transaction():
                existing_archive = db.archived_tracker.find_one({
                    "org_id": org_id,
                    "platform": "whatsapp",
                    "whatsapp_id": phone_id,
                    "status": "active",
                }, session=session)
                archive_id = existing_archive.get("archive_id") if existing_archive else uuid.uuid4().hex
                tracker_fields = {
                    "org_id": org_id,
                    "platform": "whatsapp",
                    "whatsapp_id": phone_id,
                    "phone_id": phone_id,
                    "waba_id": connection.get("wa_id"),
                    "phone": connection.get("phone"),
                    "label": connection.get("label"),
                    "slot": connection.get("slot"),
                    "wa_bot_flow_id": connection.get("wa_bot_flow_id"),
                    "expires_at": now + timedelta(days=expire_days),
                    "status": "active",
                }
                db.archived_tracker.update_one(
                    {"archive_id": archive_id},
                    {"$set": tracker_fields, "$setOnInsert": {"archive_id": archive_id, "created_at": now}},
                    upsert=True,
                    session=session,
                )

                conversations_to_archive = list(db[conversations_collection_name].find(
                    phone_filter, {"_id": 0}, session=session,
                ))
                conversation_ids = [item["conversation_id"] for item in conversations_to_archive]
                message_filter = {"conversation_id": {"$in": conversation_ids}}
                note_filter = {"conversation_id": {"$in": conversation_ids}}
                messages_to_archive = []
                for item in db[messages_collection_name].find(message_filter, session=session):
                    archived = dict(item)
                    archived["message_id"] = str(archived.get("message_id") or archived.get("id") or archived.get("_id"))
                    archived.pop("_id", None)
                    messages_to_archive.append(archived)
                notes_to_archive = []
                for item in db[private_notes_collection_name].find(note_filter, session=session):
                    archived = dict(item)
                    archived["note_id"] = str(archived.get("note_id") or archived.get("_id"))
                    archived.pop("_id", None)
                    notes_to_archive.append(archived)

                archived_conversations = db[f"archived_conversations_{org_id}_{archive_id}"]
                archived_messages = db[f"archived_messages_{org_id}_{archive_id}"]
                archived_notes = db[f"archived_private_notes_{org_id}_{archive_id}"]
                if conversations_to_archive:
                    archived_conversations.bulk_write([
                        UpdateOne({"conversation_id": item["conversation_id"]}, {"$set": item}, upsert=True)
                        for item in conversations_to_archive
                    ], session=session)
                message_ops = [
                    UpdateOne({"message_id": item["message_id"]}, {"$set": item}, upsert=True)
                    for item in messages_to_archive
                ]
                if message_ops:
                    archived_messages.bulk_write(message_ops, session=session)
                note_ops = [
                    UpdateOne({"note_id": item["note_id"]}, {"$set": item}, upsert=True)
                    for item in notes_to_archive
                ]
                if note_ops:
                    archived_notes.bulk_write(note_ops, session=session)

                if conversation_ids:
                    db[messages_collection_name].delete_many(message_filter, session=session)
                    db[private_notes_collection_name].delete_many(note_filter, session=session)
                    db[conversations_collection_name].delete_many(
                        {"conversation_id": {"$in": conversation_ids}}, session=session,
                    )
                    contacts = db[f"contacts_{org_id}"]
                    for contact in contacts.find({"$or": [
                        {"conversation_id": {"$in": conversation_ids}},
                        {"conversation_ids": {"$in": conversation_ids}},
                    ]}, session=session):
                        remaining = [item for item in contact.get("conversation_ids", []) if item not in conversation_ids]
                        update = {"conversation_ids": remaining}
                        if contact.get("conversation_id") in conversation_ids:
                            update["conversation_id"] = remaining[-1] if remaining else None
                        contacts.update_one({"_id": contact["_id"]}, {"$set": update}, session=session)

        logger.success(f"Archived WhatsApp phone {phone_id} for org {org_id} with archive_id {archive_id}")
        return {"archive_id": archive_id, "conversation_count": len(conversation_ids)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error archiving WhatsApp phone {phone_id} for org {org_id}: {e}")
        raise

async def _unsubscribe_whatsapp_waba(access_token: str, org_id: str, wa_id: str) -> tuple[bool, str]:
    """Remove this app's WABA subscription without revoking the shared user token."""
    if not wa_id:
        return False, "missing_waba_id"
    try:
        version = WA_VERSION or VERSION or "v24.0"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.delete(
                f"https://graph.facebook.com/{version}/{wa_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.is_success:
            return True, ""
        try:
            error = (response.json() or {}).get("error") or {}
        except ValueError:
            error = {}
        message = str(error.get("message") or "").casefold()
        if response.status_code == 404 or "not subscribed" in message or "does not exist" in message:
            return True, ""
        return False, f"Meta returned status {response.status_code}"
    except Exception as exc:
        logger.error("WhatsApp WABA unsubscribe failed for org {} and WABA {} ({})", org_id, wa_id, type(exc).__name__)
        return False, type(exc).__name__


async def finalize_whatsapp_revocation(org_id: str, phone_id: str) -> dict:
    """Finish provider cleanup after the local line is already disconnected."""
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    claimable = {"$or": [
        {"provider_revoke_status": {"$in": ["pending", "failed"]}},
        {"provider_revoke_status": "processing", "provider_revoke_updated_at": {"$lt": stale_before}},
        {"provider_revoke_status": "processing", "provider_revoke_updated_at": {"$exists": False}},
    ]}
    connection = db.whatsapp_connections.find_one({
        "org_id": org_id,
        "phone_id": phone_id,
        "is_active": False,
        **claimable,
    })
    if not connection:
        return {"provider_cleanup": "not_pending"}
    wa_id = str(connection.get("wa_id") or connection.get("waba_id") or "")
    active_sibling = db.whatsapp_connections.find_one({
        "_id": {"$ne": connection["_id"]},
        "$or": [{"wa_id": wa_id}, {"waba_id": wa_id}],
        "is_active": {"$ne": False},
    })
    if active_sibling:
        db.whatsapp_connections.update_one(
            {"_id": connection["_id"], "is_active": False},
            {"$set": {
                "provider_revoke_status": "not_needed",
                "provider_revoke_error": None,
                "provider_revoke_updated_at": datetime.now(timezone.utc),
            }, "$unset": {"access_token": ""}},
        )
        return {"provider_cleanup": "not_needed"}
    attempts = int(connection.get("provider_revoke_attempts") or 0)
    if attempts >= WHATSAPP_PROVIDER_CLEANUP_MAX_ATTEMPTS:
        db.whatsapp_connections.update_one(
            {"_id": connection["_id"], "is_active": False},
            {"$set": {
                "provider_revoke_status": "abandoned",
                "provider_revoke_error": "retry_limit_reached",
                "provider_revoke_updated_at": datetime.now(timezone.utc),
            }, "$unset": {"access_token": ""}},
        )
        return {"provider_cleanup": "abandoned"}
    access_token = str(connection.get("access_token") or "")
    if not access_token:
        db.whatsapp_connections.update_one(
            {"_id": connection["_id"], "is_active": False},
            {"$set": {"provider_revoke_status": "complete", "provider_revoke_error": None}},
        )
        db.whatsapp_waba_owners.delete_one({"_id": wa_id, "org_id": org_id})
        return {"provider_cleanup": "complete"}

    claim = db.whatsapp_connections.update_one(
        {
            "_id": connection["_id"],
            "is_active": False,
            **claimable,
        },
        {"$set": {
            "provider_revoke_status": "processing",
            "provider_revoke_updated_at": datetime.now(timezone.utc),
        }, "$inc": {"provider_revoke_attempts": 1}},
    )
    if not claim.modified_count:
        return {"provider_cleanup": "processing"}
    success, error = await _unsubscribe_whatsapp_waba(access_token, org_id, wa_id)
    update = {
        "$set": {
            "provider_revoke_status": "complete" if success else "failed",
            "provider_revoke_error": None if success else error,
            "provider_revoke_updated_at": datetime.now(timezone.utc),
        },
    }
    if success:
        update["$unset"] = {"access_token": ""}
    db.whatsapp_connections.update_one(
        {"_id": connection["_id"], "is_active": False, "provider_revoke_status": "processing"},
        update,
    )
    if success:
        db.whatsapp_waba_owners.delete_one({"_id": wa_id, "org_id": org_id})
    return {"provider_cleanup": "complete" if success else "failed"}


async def reconcile_pending_whatsapp_revocations(limit: int = 10) -> int:
    # ponytail: one bounded startup batch; add a scheduled worker if this backlog becomes sustained.
    try:
        pending = list(db.whatsapp_connections.find({
            "is_active": False,
            "$or": [
                {"provider_revoke_status": {"$in": ["pending", "failed"]}},
                {
                    "provider_revoke_status": "processing",
                    "provider_revoke_updated_at": {"$lt": datetime.now(timezone.utc) - timedelta(minutes=5)},
                },
                {"provider_revoke_status": "processing", "provider_revoke_updated_at": {"$exists": False}},
            ],
        }, {"org_id": 1, "phone_id": 1}).limit(limit))
        if pending:
            await asyncio.gather(*(
                finalize_whatsapp_revocation(str(row.get("org_id") or ""), str(row.get("phone_id") or ""))
                for row in pending
            ))
        return len(pending)
    except Exception as exc:
        logger.error("Pending WhatsApp revocation reconciliation failed ({})", type(exc).__name__)
        return 0


async def run_whatsapp_revocation_reconciler() -> None:
    # ponytail: one small periodic worker; move to a queue if cleanup volume becomes sustained.
    while True:
        await reconcile_pending_whatsapp_revocations()
        await asyncio.sleep(60)


def _deactivate_whatsapp_connection_sync(org_id: str, phone_id: str) -> dict:
    """Disconnect one sender immediately while retaining its conversation history."""
    connection = db.whatsapp_connections.find_one({
        "org_id": org_id,
        "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
    })
    if not connection:
        raise HTTPException(status_code=404, detail="WhatsApp connection not found")
    active_query = {"org_id": org_id, "is_active": {"$ne": False}, "_id": {"$ne": connection["_id"]}}
    remaining_rows = list(db.whatsapp_connections.find(active_query).sort([("slot", 1), ("connected_at", 1)]))
    if connection.get("is_active") is False:
        return {
            "already_disconnected": True,
            "remaining_connections": len(remaining_rows),
            "provider_cleanup": connection.get("provider_revoke_status") or "complete",
        }

    now = datetime.now(timezone.utc)
    provider_cleanup = "pending" if connection.get("access_token") else "complete"
    current_primary = db.whatsapp_connections.find_one({
        "org_id": org_id, "slot": 1, "is_active": {"$ne": False},
    }) or next(iter(db.whatsapp_connections.find(
        {"org_id": org_id, "is_active": {"$ne": False}},
    ).sort([("connected_at", 1)])), connection)
    with mongo_client.start_session() as session:
        with session.start_transaction():
            if current_primary.get("_id") == connection.get("_id"):
                db[f"conversations_{org_id}"].update_many(
                    {
                        "platform": "whatsapp",
                        "$or": [
                            {"business_phone_id": {"$exists": False}},
                            {"business_phone_id": None},
                        ],
                    },
                    {"$set": {
                        "business_phone_id": str(connection.get("phone_id") or connection.get("phone_number_id") or phone_id),
                        "business_phone": connection.get("phone"),
                    }},
                    session=session,
                )
            update: dict = {
                "$set": {
                    "phone_id": str(connection.get("phone_id") or connection.get("phone_number_id") or phone_id),
                    "phone_number_id": str(connection.get("phone_id") or connection.get("phone_number_id") or phone_id),
                    "wa_id": str(connection.get("wa_id") or connection.get("waba_id") or ""),
                    "waba_id": str(connection.get("wa_id") or connection.get("waba_id") or ""),
                    "is_active": False,
                    "last_slot": connection.get("slot"),
                    "disconnected_at": now,
                    "updated_at": now,
                    "provider_revoke_status": provider_cleanup,
                    "provider_revoke_error": None,
                },
                "$unset": {"slot": "", "label_key": "", "wa_bot_flow_id": ""},
            }
            if provider_cleanup != "pending":
                update["$unset"]["access_token"] = ""
            db.whatsapp_connections.update_one({"_id": connection["_id"]}, update, session=session)

            for index, remaining in enumerate(remaining_rows, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": remaining["_id"]},
                    {"$set": {"slot": -index, "updated_at": now}},
                    session=session,
                )
            for slot, remaining in enumerate(remaining_rows, start=1):
                db.whatsapp_connections.update_one(
                    {"_id": remaining["_id"]},
                    {"$set": {"slot": slot, "updated_at": now}},
                    session=session,
                )
            primary = remaining_rows[0] if remaining_rows else None
            db.organizations.update_one(
                {"org_id": org_id},
                {"$set": {"wa_id": str(primary.get("wa_id") or primary.get("waba_id") or "") if primary else None}},
                session=session,
            )

            bound_flow_id = connection.get("wa_bot_flow_id")
            if bound_flow_id and not any(row.get("wa_bot_flow_id") == bound_flow_id for row in remaining_rows):
                db[f"wa_bot_flows_{org_id}"].update_one(
                    {"flow_id": bound_flow_id, "org_id": org_id},
                    {"$set": {"status": "draft", "published_at": None, "updated_at": now}},
                    session=session,
                )

    return {
        "already_disconnected": False,
        "remaining_connections": len(remaining_rows),
        "provider_cleanup": provider_cleanup,
    }


async def deactivate_whatsapp_connection(org_id: str, phone_id: str) -> dict:
    """Run the local transactional disconnect outside the async event loop."""
    return await asyncio.to_thread(_deactivate_whatsapp_connection_sync, org_id, phone_id)

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
        if not message:
            logger.error("Message not found")
            return {"status": "error", "message": "Message not found"}
        
        conversation = db[f"conversations_{org_id}"].find_one({
            "conversation_id": message.get("conversation_id")
        })
        if not conversation:
            raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
        recipient_id = conversation.get("customer_id") or message.get("sender_id")
        connection = resolve_outbound_whatsapp_connection(
            org_id,
            conversation.get("whatsapp_id"),
            conversation.get("business_phone_id"),
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
        
    conversation_doc = db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
    if not conversation_doc:
        raise HTTPException(status_code=404, detail="WhatsApp conversation not found")
    if str(conversation_doc.get("whatsapp_id") or whatsapp_id) != str(whatsapp_id):
        raise HTTPException(status_code=409, detail="The selected sender does not own this conversation")
    wa_connection = resolve_outbound_whatsapp_connection(
        org_id,
        conversation_doc.get("whatsapp_id") or whatsapp_id,
        conversation_doc.get("business_phone_id"),
    )
    if not wa_connection:
        logger.warning(f"Received webhook for unregistered WhatsApp ID: {whatsapp_id}")
        raise HTTPException(status_code=403, detail="Received webhook for unregistered WhatsApp ID")
    
    whatsapp_business_id = wa_connection.get("wa_id")
    access_token = wa_connection.get("access_token")
    phone_id = wa_connection.get("phone_id")
    recipient_id = conversation_doc.get("customer_id")

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

async def read_message_and_typing_indicator(whatsapp_id: str, mode: Optional[str] = None, message_id: Optional[str] = None, conversation_id: Optional[str] = None):
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
            if not conversation_id:
                logger.warning(
                    f"Missing conversation_id for platform typing indicator on WhatsApp ID: {whatsapp_id}; skipping typing indicator"
                )
                return

            query_filter = {
                "role": MessageRole.CUSTOMER,
                "status": {"$ne": "read"},
                "platform": "whatsapp",
                "conversation_id": conversation_id,
            }

            message_doc = db[messages_collection].find_one(
                query_filter,
                {
                    "message_id": 1, 
                    "_id": 0
                },
                sort=[("timestamp", -1)]
            )
            message_id = message_doc.get("message_id") if message_doc else None

            if not message_id:
                logger.debug(
                    f"No unread customer message found for WhatsApp ID: {whatsapp_id}; skipping typing indicator"
                )
                return

        if not message_id:
            logger.error(f"No message_id available when trying to send typing indicator for WhatsApp ID: {whatsapp_id}")
            return
        
        conversation = (
            db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
            if conversation_id else None
        )
        connection = resolve_outbound_whatsapp_connection(
            org_id,
            (conversation or {}).get("whatsapp_id") or whatsapp_id,
            (conversation or {}).get("business_phone_id"),
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

def ensure_whatsapp_call_indexes() -> None:
    try:
        db.whatsapp_calls.create_index(
            [("org_id", 1), ("call_id", 1)],
            unique=True,
            name="org_id_call_id_unique",
            partialFilterExpression={"org_id": {"$type": "string"}, "call_id": {"$type": "string"}},
        )
        db.whatsapp_calls.create_index(
            [("org_id", 1), ("updated_at", -1), ("call_id", -1)],
            name="org_call_history",
        )
        db.whatsapp_calls.create_index(
            [("org_id", 1), ("conversation_id", 1), ("updated_at", -1), ("call_id", -1)],
            name="org_conversation_call_history",
        )
    except Exception as e:
        logger.warning(f"Failed to create WhatsApp call index: {e}")


def ensure_whatsapp_outbound_indexes() -> None:
    try:
        db.whatsapp_outbound_sends.create_index("expires_at", expireAfterSeconds=0)
    except Exception as e:
        logger.warning(f"Failed to create WhatsApp outbound-send index: {e}")


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
        "skipped": 0,
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
    sender_connection = await asyncio.to_thread(
        db.whatsapp_connections.find_one,
        {
            "org_id": org_id,
            "wa_id": wa_id,
            "phone_id": phone_id,
            "is_active": {"$ne": False},
        },
        {"phone": 1, "_id": 0},
    )
    if not sender_connection:
        logger.error(f"Bulk WA | job={job_id} | sender connection is no longer active")
        await asyncio.to_thread(
            db.bulk_jobs.update_one,
            {"job_id": job_id, "org_id": org_id},
            {"$set": {"status": "failed", "error": "sender_disconnected", "updated_at": datetime.now(timezone.utc)}},
        )
        return
    business_phone = sender_connection.get("phone")
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
                
                suppressed = await asyncio.to_thread(
                    get_broadcast_suppressed_phone_keys,
                    org_id,
                    [recipient.phone_number for recipient in chunk],
                )
                tasks = []
                batch_results = []
                for idx, recipient in enumerate(chunk):
                    if broadcast_phone_key(recipient.phone_number) in suppressed:
                        batch_results.append(({
                            "job_id": job_id,
                            "phone_number": recipient.phone_number,
                            "status": "skipped",
                            "error": "Recipient opted out",
                            "created_at": datetime.now(timezone.utc),
                        }, {"$inc": {"skipped": 1}, "$set": {"updated_at": datetime.now(timezone.utc)}}))
                    else:
                        tasks.append(send_to_recipient(i + idx, recipient, client))
                if tasks:
                    batch_results.extend(await asyncio.gather(*tasks))
                
                # Batch DB operations
                entries = [res[0] for res in batch_results]
                
                total_sent = sum(res[1]["$inc"].get("sent", 0) for res in batch_results)
                total_failed = sum(res[1]["$inc"].get("failed", 0) for res in batch_results)
                total_skipped = sum(res[1]["$inc"].get("skipped", 0) for res in batch_results)
                
                # Prepare message and conversation bulk operations for successful sends
                message_docs = []
                convo_updates = []
                now = datetime.now(timezone.utc)
                
                for entry, _ in batch_results:
                    if entry["status"] == "sent":
                        phone_number = entry["phone_number"]
                        msg_id = entry["message_id"]
                        convo_id = whatsapp_conversation_id(phone_number, phone_id)
                        
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
                                    "business_phone_id": phone_id,
                                    "business_phone": business_phone,
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
                if total_skipped > 0: combined_update["$inc"]["skipped"] = total_skipped

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
