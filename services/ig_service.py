# Built-in modules
import hashlib
import uuid
import httpx
import json
from typing import Optional
from datetime import datetime, timezone, timedelta
from bson.objectid import ObjectId
from services.cloudinary_service import upload_media_to_cloudinary
from services.websocket_service import broadcast_main_ws
from supabase import create_client, Client
from pymongo import UpdateOne, ReturnDocument
from pymongo.errors import DuplicateKeyError
from fastapi import HTTPException
from fastapi import BackgroundTasks


# Internal modules
from core.managers import manager
from schemas.models import MessageRole
from database import get_mongo_db, get_mongo_client
from core.services import services
from services.followup_service import abort_scheduled_message
from services.conversation_utils import reopen_conversation
from services.push_notification_service import schedule_customer_message_push
from services.ai_reply_guard import AIProviderUnavailable
from services.utils import name_search_prefixes, serialize_mongo

from config.settings import (
    IG_ID, 
    IG_ACCESS_TOKEN, 
    IG_VERSION,
    SUPABASE_URL, 
    SUPABASE_KEY, 
    SUPABASE_INSTA_DM_TABLE,  
    IG_ACCESS_TOKEN
)
from loguru import logger


supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

db = get_mongo_db()

mongo_client = get_mongo_client()

_indexed_instagram_outboxes = set()


def _instagram_access_token(org_id: str, instagram_id: str) -> str:
    connection = db.instagram_connections.find_one(
        {"org_id": org_id, "instagram_id": instagram_id, "is_active": True},
        {"page_access_token": 1},
        sort=[("last_updated", -1)],
    )
    if not connection or not connection.get("page_access_token"):
        raise HTTPException(status_code=400, detail="Instagram connection is unavailable")
    return connection["page_access_token"]


def _claim_instagram_send(
    org_id: str,
    conversation_id: str,
    temp_id: Optional[str],
    kind: str,
    fingerprint: str,
    content: Optional[str] = None,
    authoritative_replay: bool = False,
    sales_action_job_id: Optional[str] = None,
):
    if not temp_id:
        raise ValueError("temp_id is required for Instagram replies")
    collection = db[f"instagram_outbox_{org_id}"]
    if org_id not in _indexed_instagram_outboxes:
        collection.create_index("expires_at", expireAfterSeconds=0)
        _indexed_instagram_outboxes.add(org_id)
    request_id = f"{conversation_id}:{temp_id}:{kind}"
    now = datetime.now(timezone.utc)
    claim_token = str(uuid.uuid4())
    try:
        existing = collection.find_one_and_update(
            {"_id": request_id},
            {"$setOnInsert": {
                "status": "sending",
                "fingerprint": fingerprint,
                "content": content,
                "sales_action_job_id": sales_action_job_id,
                "claim_token": claim_token,
                "claimed_at": now,
                "created_at": now,
                "expires_at": now + timedelta(days=30),
            }},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        existing = collection.find_one({"_id": request_id})
    if not existing:
        return request_id, claim_token, None
    if existing.get("fingerprint") != fingerprint and not authoritative_replay:
        return request_id, None, {
            "error": "temp_id was already used for a different Instagram message",
            "success": False,
        }
    if existing.get("result"):
        result = dict(existing["result"])
        if authoritative_replay:
            result["_authoritative_content"] = existing.get("content")
            result["sales_action_job_id"] = existing.get("sales_action_job_id")
        return request_id, None, result
    if authoritative_replay:
        return request_id, None, {
            "error": "The previous Instagram delivery could not be confirmed. Check the chat before sending again.",
            "success": False,
            "delivery_unknown": True,
        }
    stale_before = now - timedelta(minutes=2)
    claimed_at = existing.get("claimed_at")
    if not claimed_at or claimed_at < stale_before:
        unknown_result = {
            "error": "The previous Instagram delivery could not be confirmed. Check the chat before sending again.",
            "success": False,
            "delivery_unknown": True,
        }
        reconciled = collection.update_one(
            {
                "_id": request_id,
                "status": "sending",
                "claim_token": existing.get("claim_token"),
                "claimed_at": claimed_at,
            },
            {"$set": {"status": "delivery_unknown", "result": unknown_result, "updated_at": now}},
        )
        if reconciled.modified_count:
            return request_id, None, unknown_result
    return request_id, None, {
        "error": "This Instagram send is already being processed",
        "success": False,
        "duplicate_suppressed": True,
    }


def _finish_instagram_send(
    org_id: str,
    request_id: Optional[str],
    claim_token: Optional[str],
    result: dict,
) -> None:
    if request_id and claim_token:
        try:
            db[f"instagram_outbox_{org_id}"].update_one(
                {"_id": request_id, "claim_token": claim_token},
                {"$set": {
                    "status": (
                        "sent" if result.get("success")
                        else "delivery_unknown" if result.get("delivery_unknown")
                        else "failed"
                    ),
                    "result": result,
                    "updated_at": datetime.now(timezone.utc),
                }},
            )
        except Exception:
            logger.exception("Could not persist Instagram outbound result")


def _instagram_conversation_id(org_id: str, recipient_id: str) -> Optional[str]:
    conversation = db[f"conversations_{org_id}"].find_one(
        {"platform": "instagram", "customer_id": recipient_id},
        {"conversation_id": 1},
    )
    return conversation.get("conversation_id") if conversation else None


async def sync_instagram_outbound_conversation(
    org_id: str,
    conversation_id: str,
    instagram_id: str,
    message_id: str,
) -> None:
    messages = db[f"messages_{org_id}"]
    message = messages.find_one({"id": message_id, "platform": "instagram"})
    if not message or not message.get("queue_sync_pending"):
        return
    timestamp = message.get("timestamp") or datetime.now(timezone.utc)
    activity = (message.get("content") or "Shared an attachment").strip()
    sender_name = message.get("sender_name") or "Agent"
    is_auto_followup = bool(message.get("is_auto_followup"))
    update = {
        "last_message": f"{sender_name}: {activity}",
        "last_message_timestamp": timestamp,
        "last_sender": "agent",
        "last_message_is_private_note": False,
        "needs_reply": False,
    }
    if not is_auto_followup:
        update.update({
            "status": "open",
            "reminder_stage": 0,
            "next_action_at": timestamp + timedelta(hours=6),
        })
    mongo_update = {"$set": update}
    if not is_auto_followup:
        mongo_update["$unset"] = {"closed_at": "", "closure_reason": ""}
    conversation = db[f"conversations_{org_id}"].find_one_and_update(
        {
            "conversation_id": conversation_id,
            "$or": [
                {"last_message_timestamp": {"$lte": timestamp}},
                {"last_message_timestamp": {"$exists": False}},
            ],
        },
        mongo_update,
        return_document=ReturnDocument.AFTER,
    )
    if conversation:
        if not is_auto_followup:
            await abort_scheduled_message(org_id, conversation_id)
        await broadcast_main_ws(
            platform_id=instagram_id,
            platform_type="instagram",
            event_type="conversation_updated",
            payload={"conversation": serialize_mongo(conversation)},
        )
    messages.update_one({"id": message_id, "platform": "instagram"}, {"$set": {"queue_sync_pending": False}})


async def _record_confirmed_instagram_outbound(
    org_id: str,
    conversation_id: Optional[str],
    instagram_id: str,
    recipient_id: str,
    sender_name: str,
    message_id: str,
    content: str,
    message_type: str = "text",
    payload: Optional[dict] = None,
    temp_id: Optional[str] = None,
    context_type: Optional[str] = None,
    context: Optional[dict] = None,
    is_auto_followup: bool = False,
) -> None:
    conversation_id = conversation_id or _instagram_conversation_id(org_id, recipient_id)
    if not conversation_id:
        raise RuntimeError("Instagram conversation not found after send")
    await store_instagram_message(
        org_id=org_id,
        content=content,
        conversation_id=conversation_id,
        sender_id=instagram_id,
        type=message_type,
        payload=payload,
        sender_name=sender_name or "Agent",
        sender_username=sender_name or "Agent",
        recipient_id=instagram_id,
        role=MessageRole.AGENT,
        mode="reply",
        message_id=message_id,
        context_type=context_type,
        context=context,
        temp_id=temp_id,
        is_auto_followup=is_auto_followup,
    )
    await sync_instagram_outbound_conversation(
        org_id,
        conversation_id,
        instagram_id,
        message_id,
    )

async def send_ig_message(
    id,
    message_text,
    instagram_id,
    mode,
    org_id,
    temp_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sender_name: str = "Agent",
    context_type: Optional[str] = None,
    context: Optional[dict] = None,
    is_auto_followup: bool = False,
    enforce_ai_state: bool = False,
    ai_usage: Optional[dict] = None,
    bot_context: Optional[dict] = None,
) -> dict:
    """
    Send a message to an Instagram user using the appropriate access token
    
    Args:
        id: The recipient's Instagram user ID
        message_text: The message content to send
        instagram_id: Instagram business account ID to use for sending
        mode: The mode of the message (reply or private message)
        org_id: The organization ID
    """
    from services.entitlement_service import require_feature
    require_feature(org_id, "CHANNELS")
    request_id = claim_token = quota_event_key = None
    quota_consumed = False
    provider_started = False
    try:
        access_token = _instagram_access_token(org_id, instagram_id)
        
        if mode == "reply":
            conversation_id = conversation_id or _instagram_conversation_id(org_id, id)
            fingerprint = hashlib.sha256(f"{id}\0text\0{message_text}".encode()).hexdigest()
            request_id, claim_token, cached = _claim_instagram_send(
                org_id, conversation_id or str(id), temp_id, "text", fingerprint,
                content=message_text, authoritative_replay=enforce_ai_state,
                sales_action_job_id=str(
                    (bot_context or {}).get("sales_action_job_id") or ""
                ) or None,
            )
            if cached:
                if cached.get("success") and cached.get("message_id"):
                    confirmed_text = cached.pop("_authoritative_content", None) or message_text
                    try:
                        await _record_confirmed_instagram_outbound(
                            org_id, conversation_id, instagram_id, id, sender_name,
                            cached["message_id"], confirmed_text, temp_id=temp_id,
                            context_type=context_type, context=context,
                            is_auto_followup=is_auto_followup,
                        )
                    except Exception:
                        logger.exception("Retry could not reconcile a confirmed Instagram message")
                        cached["local_sync_pending"] = True
                return cached
            if (bot_context or {}).get("sales_action_job_id"):
                from services.sales_workflow_service import validate_delivery
                await validate_delivery(org_id, bot_context["sales_action_job_id"])
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
            if enforce_ai_state:
                from services.ai_policy_service import is_effective_ai_enabled
                from services.entitlement_service import reserve_quota_async

                if not await is_effective_ai_enabled(org_id, conversation_id):
                    db[f"instagram_outbox_{org_id}"].delete_one({
                        "_id": request_id,
                        "claim_token": claim_token,
                        "status": "sending",
                    })
                    raise AIProviderUnavailable("ai_disabled", escalation_required=False)
                quota_event_key = f"ai:{request_id or temp_id or conversation_id}"
                try:
                    await reserve_quota_async(org_id, "AI", "monthly_messages", 1, quota_event_key)
                    quota_consumed = True
                except HTTPException as exc:
                    raise AIProviderUnavailable("quota_exhausted", escalation_required=False) from exc
            async with httpx.AsyncClient(timeout=30.0) as client:
                provider_started = True
                response = await client.post(url, headers=headers, json=data)
            if (
                400 <= response.status_code < 500
                and response.status_code != 408
                and quota_consumed
                and quota_event_key
            ):
                from services.entitlement_service import release_quota_async

                provider_started = False
                await release_quota_async(org_id, "monthly_messages", quota_event_key)
                quota_consumed = False
            response.raise_for_status()
            response_data = response.json()
            message_id = response_data.get("message_id") or response_data.get("messages", [{}])[0].get("id")
            if not message_id:
                raise RuntimeError("Instagram did not return a message ID")
            logger.info(f"Instagram message response: {response.status_code}, {response.text}")
            result = {"message_id": message_id, "success": True, "status": "sent", "response": response_data}
            if (bot_context or {}).get("sales_action_job_id"):
                result["sales_action_job_id"] = bot_context["sales_action_job_id"]
            _finish_instagram_send(org_id, request_id, claim_token, result)
            if enforce_ai_state:
                try:
                    from services.entitlement_service import commit_quota_async, record_ai_usage_async

                    await commit_quota_async(org_id, "monthly_messages", quota_event_key, "0")

                    usage = ai_usage if isinstance(ai_usage, dict) else {}
                    await record_ai_usage_async(
                        org_id,
                        quota_event_key or f"ai:{message_id}",
                        "instagram",
                        1,
                        int(usage.get("input_tokens", 0)),
                        int(usage.get("output_tokens", 0)),
                        int(usage.get("recorded_cost_microusd", 0)),
                    )
                except Exception as exc:
                    logger.error(f"Could not record Instagram AI analytics for org={org_id}: {exc}")
            try:
                await _record_confirmed_instagram_outbound(
                    org_id, conversation_id, instagram_id, id, sender_name,
                    message_id, message_text, temp_id=temp_id,
                    context_type=context_type, context=context,
                    is_auto_followup=is_auto_followup,
                )
            except Exception:
                logger.exception("Instagram message sent but local synchronization is pending")
                result["local_sync_pending"] = True
            return result
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
        
    except AIProviderUnavailable:
        raise
    except Exception as e:
        logger.error(f"Error sending Instagram message: {e}")
        result = {"error": str(e), "success": False}
        if provider_started:
            result["delivery_unknown"] = True
        _finish_instagram_send(org_id, request_id, claim_token, result)
        if provider_started and enforce_ai_state:
            raise AIProviderUnavailable("delivery_unknown") from e
        return result
    
async def send_ig_media_message(
    recipient,
    media_url,
    media_type,
    instagram_id,
    mode,
    org_id,
    temp_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sender_name: str = "Agent",
    context_type: Optional[str] = None,
    context: Optional[dict] = None,
    is_auto_followup: bool = False,
    _request_id: Optional[str] = None,
    _claim_token: Optional[str] = None,
) -> dict:
    """
    Send a media message to an Instagram user using the appropriate access token
    
    Args:
        recipient: The recipient's Instagram user ID
        media_url: The URL of the media to send
        instagram_id: Instagram business account ID to use for sending
    """
    request_id, claim_token = _request_id, _claim_token
    try:
        access_token = _instagram_access_token(org_id, instagram_id)
        provider_media_type = "file" if media_type == "document" else media_type
        content = {
            "audio": "Shared an audio",
            "file": "Shared a file",
            "document": "Shared a file",
            "image": "Shared an image",
            "video": "Shared a video",
        }.get(media_type, "Shared an attachment")

        if mode == "reply":
            conversation_id = conversation_id or _instagram_conversation_id(org_id, recipient)
            if not request_id:
                fingerprint = hashlib.sha256(
                    f"{recipient}\0{provider_media_type}\0{media_url}".encode()
                ).hexdigest()
                request_id, claim_token, cached = _claim_instagram_send(
                    org_id, conversation_id or str(recipient), temp_id, "media", fingerprint
                )
                if cached:
                    if cached.get("success") and cached.get("message_id"):
                        try:
                            await _record_confirmed_instagram_outbound(
                                org_id, conversation_id, instagram_id, recipient, sender_name,
                                cached["message_id"], content, message_type=provider_media_type,
                                payload={"url": cached.get("media_url") or media_url}, temp_id=temp_id,
                                context_type=context_type, context=context,
                                is_auto_followup=is_auto_followup,
                            )
                        except Exception:
                            logger.exception("Retry could not reconcile confirmed Instagram media")
                            cached["local_sync_pending"] = True
                    return cached
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
                        "type":provider_media_type,
                        "payload":{
                            "url":media_url
                        }
                    }
                }
            }

            # Send the media message
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=headers, json=data)
            response.raise_for_status()
            response_data = response.json()
            message_id = response_data.get("message_id") or response_data.get("messages", [{}])[0].get("id")
            if not message_id:
                raise RuntimeError("Instagram did not return a message ID")
            logger.info(f"Instagram media message response: {response.status_code}, {response.text}")
            result = {
                "message_id": message_id,
                "success": True,
                "status": "sent",
                "response": response_data,
                "media_url": media_url,
                "media_type": media_type,
            }
            _finish_instagram_send(org_id, request_id, claim_token, result)
            try:
                await _record_confirmed_instagram_outbound(
                    org_id, conversation_id, instagram_id, recipient, sender_name,
                    message_id, content, message_type=provider_media_type,
                    payload={"url": media_url}, temp_id=temp_id,
                    context_type=context_type, context=context,
                    is_auto_followup=is_auto_followup,
                )
            except Exception:
                logger.exception("Instagram media sent but local synchronization is pending")
                result["local_sync_pending"] = True
            return result
        else:
            message_id = f"privatenote_{uuid.uuid4()}"

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
            return {"message_id": message_id, "success": True, "status": "sent"}
        
    except Exception as e:
        logger.error(f"Error sending Instagram media message: {e}")
        # Return an error response that can be handled by the caller
        result = {"error": str(e), "success": False}
        _finish_instagram_send(org_id, request_id, claim_token, result)
        return result

async def upload_and_send_ig_media_message(
    recipient,
    file,
    instagram_id,
    org_id,
    mode,
    temp_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    sender_name: str = "Agent",
    context_type: Optional[str] = None,
    context: Optional[dict] = None,
    is_auto_followup: bool = False,
) -> dict:
    """
    Upload media to Cloudinary and send it as an Instagram message
    
    Args:
        recipient: The recipient's Instagram user ID
        file: The media file to upload and send
        instagram_id: Instagram business account ID to use for sending
        org_id: Organization ID for context
    """
    request_id = claim_token = None
    try:
        if mode == "reply":
            conversation_id = conversation_id or _instagram_conversation_id(org_id, recipient)
            digest = hashlib.sha256(
                f"{recipient}\0{getattr(file, 'content_type', '')}\0{getattr(file, 'filename', '')}\0".encode()
            )
            while chunk := await file.read(1024 * 1024):
                digest.update(chunk)
            await file.seek(0)
            request_id, claim_token, cached = _claim_instagram_send(
                org_id, conversation_id or str(recipient), temp_id, "media", digest.hexdigest()
            )
            if cached:
                if cached.get("success") and cached.get("message_id"):
                    try:
                        await _record_confirmed_instagram_outbound(
                            org_id, conversation_id, instagram_id, recipient, sender_name,
                            cached["message_id"], "Shared an attachment",
                            message_type=cached.get("media_type") or "file",
                            payload={"url": cached.get("media_url")}, temp_id=temp_id,
                            context_type=context_type, context=context,
                            is_auto_followup=is_auto_followup,
                        )
                    except Exception:
                        logger.exception("Retry could not reconcile confirmed Instagram upload")
                        cached["local_sync_pending"] = True
                return cached

        # Upload media to Cloudinary
        result = await upload_media_to_cloudinary(file, platform="instagram", org_id=org_id)

        if not result:
            failure = {"error": "Media upload failed", "success": False}
            _finish_instagram_send(org_id, request_id, claim_token, failure)
            return failure

        file_url = result.get("file_url")
        file_type = result.get("file_type")
        
        if not file_url:
            logger.error("Media upload failed, cannot send Instagram media message.")
            failure = {"error": "Media upload failed", "success": False}
            _finish_instagram_send(org_id, request_id, claim_token, failure)
            return failure
        
        # Send the media message via Instagram API
        response = await send_ig_media_message(
            recipient,
            file_url,
            file_type,
            instagram_id,
            mode,
            org_id,
            temp_id=temp_id,
            conversation_id=conversation_id,
            sender_name=sender_name,
            context_type=context_type,
            context=context,
            is_auto_followup=is_auto_followup,
            _request_id=request_id,
            _claim_token=claim_token,
        )
        
        if response.get("message_id"):
            logger.success("Instagram media message sent successfully.")

            return {
                **response,
                "media_url": file_url,
                "media_type": file_type,
            }


        return response
        
    except Exception as e:
        logger.error(f"Error in upload_and_send_ig_media_message: {e}")
        failure = {"error": str(e), "success": False}
        _finish_instagram_send(org_id, request_id, claim_token, failure)
        return failure


def _instagram_message_storage_id(message_id: str) -> str:
    return f"instagram:{message_id}"


def claim_instagram_webhook_message(org_id: str, message_id: str) -> Optional[str]:
    collection = db[f"messages_{org_id}"]
    if collection.find_one({"id": message_id, "_processing": {"$ne": True}}, {"_id": 1}):
        return None
    storage_id = _instagram_message_storage_id(message_id)
    now = datetime.now(timezone.utc)
    claim_token = str(uuid.uuid4())
    try:
        existing = collection.find_one_and_update(
            {"_id": storage_id},
            {"$setOnInsert": {
                "id": message_id,
                "message_id": message_id,
                "platform": "instagram",
                "_processing": True,
                "processing_at": now,
                "claim_token": claim_token,
            }},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
    except DuplicateKeyError:
        existing = collection.find_one({"_id": storage_id})
    if existing is None:
        return claim_token
    processing_at = existing.get("processing_at")
    if existing.get("_processing") and (not processing_at or processing_at < now - timedelta(minutes=2)):
        reclaimed = collection.update_one(
            {
                "_id": storage_id,
                "_processing": True,
                "claim_token": existing.get("claim_token"),
                "processing_at": processing_at,
            },
            {"$set": {"processing_at": now, "claim_token": claim_token}},
        )
        if reclaimed.modified_count:
            return claim_token
    return None


def release_instagram_webhook_claim(org_id: str, message_id: str, claim_token: str) -> None:
    db[f"messages_{org_id}"].delete_one({
        "_id": _instagram_message_storage_id(message_id),
        "_processing": True,
        "claim_token": claim_token,
    })


def claim_instagram_webhook_downstream(org_id: str, message_id: str) -> Optional[str]:
    collection = db[f"messages_{org_id}"]
    message = collection.find_one({"id": message_id})
    if not message or message.get("webhook_status") not in {"pending", "processing"}:
        return None
    now = datetime.now(timezone.utc)
    claimed_at = message.get("webhook_claimed_at")
    if claimed_at and claimed_at >= now - timedelta(minutes=2):
        return None
    claim_token = str(uuid.uuid4())
    updated = collection.update_one(
        {
            "_id": message["_id"],
            "webhook_status": message.get("webhook_status"),
            "webhook_claim_token": message.get("webhook_claim_token"),
        },
        {"$set": {
            "webhook_status": "processing",
            "webhook_claim_token": claim_token,
            "webhook_claimed_at": now,
        }},
    )
    return claim_token if updated.modified_count else None


def complete_instagram_webhook_message(org_id: str, message_id: str, claim_token: str) -> None:
    db[f"messages_{org_id}"].update_one(
        {"id": message_id, "webhook_claim_token": claim_token},
        {"$set": {"webhook_status": "complete", "webhook_completed_at": datetime.now(timezone.utc)}},
    )


def release_instagram_webhook_downstream(org_id: str, message_id: str, claim_token: str) -> None:
    db[f"messages_{org_id}"].update_one(
        {
            "id": message_id,
            "webhook_status": "processing",
            "webhook_claim_token": claim_token,
        },
        {
            "$set": {"webhook_status": "pending"},
            "$unset": {"webhook_claim_token": "", "webhook_claimed_at": ""},
        },
    )

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
    context: Optional[dict] = None,
    notify_push: bool = False,
    temp_id: Optional[str] = None,
    claim_token: Optional[str] = None,
    is_auto_followup: bool = False,
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
        "timestamp": datetime.now(timezone.utc),
        "status": "sent",
        "mode": mode,
        "is_auto_followup": is_auto_followup,
        "queue_sync_pending": role == MessageRole.AGENT and mode == "reply",
        "webhook_status": "processing" if claim_token else "complete",
    }
    if claim_token:
        message.update({
            "webhook_claim_token": claim_token,
            "webhook_claimed_at": datetime.now(timezone.utc),
        })

    if temp_id:
        message["temp_id"] = temp_id

    existing = messages_collection.find_one(
        {"id": message_id, "_processing": {"$ne": True}},
        {"_id": 1},
    )
    if existing:
        return message_id
    storage_filter = {"_id": _instagram_message_storage_id(message_id)}
    if claim_token:
        storage_filter["claim_token"] = claim_token
    stored = messages_collection.update_one(
        storage_filter,
        {"$set": message, "$unset": {"_processing": "", "processing_at": "", "claim_token": ""}},
        upsert=not claim_token,
    )
    if claim_token and not stored.modified_count:
        raise RuntimeError("Instagram webhook claim is no longer owned by this worker")
    inserted = stored.upserted_id is not None or stored.modified_count > 0
    if inserted:
        logger.success(f"Instagram message stored: {message_id}")
    else:
        return message_id

    if inserted and notify_push and role == MessageRole.CUSTOMER and mode != "private":
        await schedule_customer_message_push(
            org_id=org_id,
            platform="instagram",
            message_id=message_id,
            conversation_id=conversation_id,
            sender_name=sender_name,
            content=content,
            message_type=type,
        )
    
    # Keep only latest 500 messages per conversation
    messages_count = messages_collection.count_documents({"conversation_id": conversation_id}) if inserted else 0
    if inserted and messages_count > 500:
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
        "timestamp": message["timestamp"].isoformat(),
        "status": "sent",
        "sender_name": sender_name,
        "mode": mode
    }
    if temp_id:
        broadcast_message["temp_id"] = temp_id
    
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

    saved_contact = await asyncio.to_thread(
        db[f"contacts_{org_id}"].find_one,
        {"instagram_id": sender_id, "name_locked_by_user": True, "deleted_at": {"$exists": False}},
        {"full_name": 1, "_id": 0},
    )
    if saved_contact and saved_contact.get("full_name"):
        customer_name = saved_contact["full_name"]

    now = datetime.now(timezone.utc)
    
    # Get the org-specific collection
    conversations_collection_name = f"conversations_{org_id}"
    conversations_collection = db[conversations_collection_name]

    # Check if conversation already exists
    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
    
    convo_key = (org_id, conversation_id)

    is_being_viewed = len(services.org_convo_viewers.get(org_id, {}).get(conversation_id, set())) > 0

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
            "customer_name_prefixes": name_search_prefixes(customer_name),
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
            "customer_name_prefixes": name_search_prefixes(customer_name),
            "customer_username": username,
            "last_message": last_message,
            "last_message_timestamp": now,
            "unread_count": 1,
            "is_ai_enabled": False,
            "status": "open",
            "last_sender": last_sender,
            "started_at": now,
            "created_at": now,
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
        
    # Broadcast stats update for every message to ensure live dashboard refresh
    # await broadcast_on_stats_update(org_id)
        
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers, params=params)
        
    logger.info(f"Response: {response.status_code}, {response.text}")
    return response.json()

async def get_ig_media_owner(media_id, page_access_token):
    url = f"https://graph.instagram.com/{media_id}"
    headers = {
        "Content-Type" : "application/json"
    }
    params = {
        "fields" : "username, owner",
        "access_token" : page_access_token
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers, params=params)
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

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers=headers, params=params)
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

# ============================================================================
# INSTAGRAM COMMENT AUTOMATION FUNCTIONS
# ============================================================================



async def reply_to_comment(comment_id: str, reply_text: str, ig_account_id: str) -> bool:
    """
    Reply to an Instagram comment
    
    Args:
        comment_id: Instagram comment ID
        reply_text: Reply message text
        ig_account_id: Instagram business account ID
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get page access token
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )
        
        if not connection:
            logger.error(f"No Instagram connection found for {ig_account_id}")
            return False
        
        page_access_token = connection.get("page_access_token")
        
        if not page_access_token:
            logger.error("No page access token found")
            return False
        
        # Instagram Graph API v22.0 - Reply to comment
        url = f"https://graph.instagram.com/v22.0/{comment_id}/replies"
        
        params = {
            "message": reply_text,
            "access_token": page_access_token
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, params=params)
            
            if response.status_code == 200:
                result = response.json()
                logger.success(f"✅ Comment reply sent: {result.get('id')}")
                return True
            else:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = response.text
                logger.error(f"❌ Failed to reply to comment (status {response.status_code}): {error_data}")
                return False
    
    except Exception as e:
        logger.error(f"❌ Error replying to comment: {str(e)}")
        return False


async def send_private_reply(
    comment_id: str,
    ig_account_id: str,
    message_text: str,
    link_url: str = None,
    button_title: str = None
) -> bool:
    """
    Send a private DM to the user who commented
    
    Args:
        comment_id: Instagram comment ID
        ig_account_id: Instagram business account ID
        message_text: DM text
        link_url: Optional link URL
        button_title: Optional button title
    
    Returns:
        True if successful
    """
    try:
        # Get page access token
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            {"page_access_token": 1},
            sort=[("last_updated", -1)]
        )
        
        if not connection:
            logger.error(f"No Instagram connection found for {ig_account_id}")
            return False
        
        page_access_token = connection.get("page_access_token")
        
        if not page_access_token:
            logger.error("No page access token found")
            return False
        
        # Step 1: Get commenter ID from comment
        comment_url = f"https://graph.instagram.com/v22.0/{comment_id}"
        comment_params = {
            "fields": "from,text",
            "access_token": page_access_token
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            comment_response = await client.get(comment_url, params=comment_params)
            
            if comment_response.status_code != 200:
                logger.error(f"Failed to fetch comment: {comment_response.text}")
                return False
            
            comment_data = comment_response.json()
            commenter_id = comment_data.get("from", {}).get("id")
            
            if not commenter_id:
                logger.error("Could not extract commenter ID")
                return False
            
            logger.info(f"Sending DM to commenter: {commenter_id}")
            
            # Step 2: Send DM
            message_url = f"https://graph.instagram.com/v22.0/me/messages"
            
            # Build message payload
            message_payload = {
                "recipient": {"id": commenter_id},
                "message": {},
                "access_token": page_access_token
            }
            
            # Add message content
            if link_url:
                # Send with link button
                message_payload["message"] = {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "generic",
                            "elements": [{
                                "title": message_text[:80],
                                "buttons": [{
                                    "type": "web_url",
                                    "url": link_url,
                                    "title": button_title or "View Link"
                                }]
                            }]
                        }
                    }
                }
            else:
                # Send text-only message
                message_payload["message"] = {
                    "text": message_text
                }
            
            # Send DM
            dm_response = await client.post(message_url, json=message_payload)
            
            if dm_response.status_code == 200:
                result = dm_response.json()
                logger.success(f"✅ DM sent: {result.get('message_id')}")
                return True
            else:
                try:
                    error_data = dm_response.json()
                except Exception:
                    error_data = dm_response.text
                logger.error(f"❌ Failed to send DM (status {dm_response.status_code}): {error_data}")
                return False
    
    except Exception as e:
        logger.error(f"❌ Error sending DM: {str(e)}")
        return False


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
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.delete(
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
                        
                    db["v5_memory"].delete_many({"org_id": org_id, "conversation_id": {
                        "$in": [item["conversation_id"] for item in conversations_to_archive]}}, session=session)
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
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
        
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
                    "timestamp": datetime.now(timezone.utc)
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=body)
        if response.status_code != 200:
            logger.error(f"Error sending message reaction: {response.text}")
            return "error"
        logger.info(f"Message reaction response: {response.status_code}, {response.json()}")
        return "success"

    except Exception as e:
        logger.error(f"Error sending message reaction: {str(e)}")
        return "error"
    


    
# # ============================================================================
# # INSTAGRAM COMMENT REPLY FUNCTIONS
# # ============================================================================

# async def reply_to_comment(comment_id: str, reply_text: str, ig_account_id: str) -> bool:
#     """
#     Reply to an Instagram comment
    
#     Args:
#         comment_id: Instagram comment ID
#         reply_text: Reply message text
#         ig_account_id: Instagram business account ID
    
#     Returns:
#         True if successful, False otherwise
#     """
#     try:
#         # Get page access token for this Instagram account
#         connection = db.instagram_connections.find_one(
#             {"instagram_id": ig_account_id, "is_active": True}
#         )
        
#         if not connection:
#             logger.error(f"No active Instagram connection found for {ig_account_id}")
#             return False
        
#         page_access_token = connection.get("page_access_token")
        
#         if not page_access_token:
#             logger.error("No page access token found")
#             return False
        
#         # Instagram Graph API v22.0 - Reply to comment
#         url = f"https://graph.instagram.com/v22.0/{comment_id}/replies"
        
#         payload = {
#             "message": reply_text,
#             "access_token": page_access_token
#         }
        
#         async with httpx.AsyncClient(timeout=30.0) as client:
#             response = await client.post(url, data=payload)
            
#             if response.status_code == 200:
#                 result = response.json()
#                 logger.success(f"✅ Comment reply sent: {result.get('id')}")
#                 return True
#             else:
#                 error_data = response.json()
#                 logger.error(f"❌ Failed to reply to comment: {error_data}")
#                 return False
    
#     except Exception as e:
#         logger.error(f"❌ Error replying to comment: {str(e)}")
#         return False


# async def send_private_reply(
#     comment_id: str,
#     ig_account_id: str,
#     message_text: str,
#     link_url: str = None,
#     button_title: str = None
# ) -> bool:
#     """
#     Send a private DM to the user who commented (via comment ID)
    
#     Args:
#         comment_id: Instagram comment ID
#         ig_account_id: Instagram business account ID
#         message_text: DM text
#         link_url: Optional link URL
#         button_title: Optional button title
    
#     Returns:
#         True if successful, False otherwise
#     """
#     try:
#         # Get page access token
#         connection = db.instagram_connections.find_one(
#             {"instagram_id": ig_account_id, "is_active": True}
#         )
        
#         if not connection:
#             logger.error(f"No active Instagram connection found for {ig_account_id}")
#             return False
        
#         page_access_token = connection.get("page_access_token")
        
#         if not page_access_token:
#             logger.error("No page access token found")
#             return False
        
#         # Step 1: Get comment details to extract commenter ID
#         comment_url = f"https://graph.instagram.com/v22.0/{comment_id}"
#         comment_params = {
#             "fields": "from,text",
#             "access_token": page_access_token
#         }
        
#         async with httpx.AsyncClient(timeout=30.0) as client:
#             comment_response = await client.get(comment_url, params=comment_params)
            
#             if comment_response.status_code != 200:
#                 logger.error(f"Failed to fetch comment details: {comment_response.text}")
#                 return False
            
#             comment_data = comment_response.json()
#             commenter_id = comment_data.get("from", {}).get("id")
            
#             if not commenter_id:
#                 logger.error("Could not extract commenter ID from comment")
#                 return False
            
#             logger.info(f"Sending DM to commenter: {commenter_id}")
            
#             # Step 2: Send DM using Instagram Messaging API
#             message_url = f"https://graph.instagram.com/v22.0/me/messages"
            
#             # Build message payload
#             message_payload = {
#                 "recipient": {"id": commenter_id},
#                 "message": {},
#                 "access_token": page_access_token
#             }
            
#             # Add message content
#             if link_url:
#                 # Send with link button
#                 message_payload["message"] = {
#                     "attachment": {
#                         "type": "template",
#                         "payload": {
#                             "template_type": "generic",
#                             "elements": [{
#                                 "title": message_text[:80],  # Max 80 chars
#                                 "subtitle": message_text[80:] if len(message_text) > 80 else None,
#                                 "buttons": [{
#                                     "type": "web_url",
#                                     "url": link_url,
#                                     "title": button_title or "View Link"
#                                 }]
#                             }]
#                         }
#                     }
#                 }
#             else:
#                 # Send text-only message
#                 message_payload["message"] = {
#                     "text": message_text
#                 }
            
#             # Send DM
#             dm_response = await client.post(
#                 message_url,
#                 json=message_payload
#             )
            
#             if dm_response.status_code == 200:
#                 result = dm_response.json()
#                 logger.success(f"✅ DM sent successfully: {result.get('message_id')}")
#                 return True
#             else:
#                 error_data = dm_response.json()
#                 logger.error(f"❌ Failed to send DM: {error_data}")
#                 return False
    
#     except Exception as e:
#         logger.error(f"❌ Error sending private reply: {str(e)}")
#         return False


async def get_comment_details(comment_id: str, ig_account_id: str) -> dict:
    """
    Get details of an Instagram comment
    
    Args:
        comment_id: Instagram comment ID
        ig_account_id: Instagram business account ID
    
    Returns:
        Comment data dictionary
    """
    try:
        connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True}
        )
        
        if not connection:
            return {}
        
        page_access_token = connection.get("page_access_token")
        
        url = f"https://graph.instagram.com/v22.0/{comment_id}"
        params = {
            "fields": "id,text,from,timestamp,media",
            "access_token": page_access_token
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to fetch comment: {response.text}")
                return {}
    
    except Exception as e:
        logger.error(f"Error fetching comment details: {str(e)}")
        return {}



async def process_comments_events(value: dict, ig_account_id: str, background_tasks: Optional[BackgroundTasks] = None):
    """
    Process Instagram comment events from webhook
    """
    try:
        comment_id = value.get("id")
        comment_text = value.get("text", "")
        media_info = value.get("media", {})
        post_id = media_info.get("id")
        commenter_info = value.get("from", {})
        commenter_id = commenter_info.get("id")
        commenter_username = commenter_info.get("username")

        if str(commenter_id) == str(ig_account_id):
            logger.info("Ignoring echo comment (comment made by the business account).")
            return
        
        if not comment_id or not post_id:
            logger.warning("Missing comment_id or post_id in webhook payload")
            return
        
        if comment_id in services.processed_comment_ids:
            logger.info(f"Duplicate comment ID {comment_id}, skipping processing.")
            return
        
        # Record this comment ID to prevent duplicate processing on webhook retries
        current_time = datetime.now(timezone.utc).timestamp()
        services.processed_comment_ids[comment_id] = current_time

        # Cleanup expired processed comment IDs to keep the map from growing indefinitely
        expiry_seconds = 60 * 60  # 1 hour TTL
        expired_ids = [
            msg_id for msg_id, ts in list(services.processed_comment_ids.items())
            if current_time - ts > expiry_seconds
        ]
        for msg_id in expired_ids:
            try:
                del services.processed_comment_ids[msg_id]
            except KeyError:
                # Entry may have been removed concurrently; safe to ignore
                pass
        
        logger.info(f" Instagram Comment Received: @{commenter_username} on post {post_id}: '{comment_text}'")
        
        #  Get connection from instagram_connections 
        instagram_connection = db.instagram_connections.find_one(
            {"instagram_id": ig_account_id, "is_active": True},
            sort=[("last_updated", -1)]
        )
        
        if not instagram_connection:
            logger.error(f"No active Instagram connection found for {ig_account_id}")
            return
        
        #  Get org_id and page_access_token from connection
        org_id = instagram_connection.get("org_id")
        page_access_token = instagram_connection.get("page_access_token")
        
        if not org_id:
            logger.error(f"No org_id found for Instagram connection {ig_account_id}")
            return
        
        if not page_access_token:
            logger.warning(f"No page_access_token found for Instagram connection {ig_account_id}")
            # Continue anyway - automation might not need to reply
        
        # Prepare trigger data with ALL required fields
        trigger_data = {
            "platform": "instagram",
            "platform_id": ig_account_id,  
            "comment_id": comment_id,
            "post_id": post_id,  
            "commenter_id": commenter_id,
            "commenter_username": commenter_username,
            "customer_id": commenter_id,
            "customer_name": commenter_username,
            "customer_username": commenter_username,
            "message_text": comment_text,  
            "comment_text": comment_text,
            "media_type": media_info.get("media_product_type"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        #  Add tasks to background_tasks to trigger automation system
        if background_tasks:
            from services.automation_trigger_handler import check_and_trigger_automations
            
            background_tasks.add_task(
                check_and_trigger_automations,
                org_id=org_id,
                platform="instagram",
                event_type="post_comment",  # Maps to "instagram_comment" trigger
                trigger_data=trigger_data
            )
        else:
            logger.warning("No background_tasks provided, skipping automation trigger.")

        #  Legacy keyword matching logic (Supabase)
        media_info = media_data.get(str(post_id))
        if media_info and media_info['keyword'].lower() in comment_text.lower():
            # Initialize responded users for this media if needed
            if str(post_id) not in services.responded_users_by_media:
                services.responded_users_by_media[str(post_id)] = set()

            if str(commenter_id) not in services.responded_users_by_media[str(post_id)]:
                logger.success(f"✅ Keyword match found: '{media_info['keyword']}' for commenter {commenter_id}")
                reply_message = media_info['reply']
                link_url = media_info.get('link')
                button_title = media_info.get('button_title', 'View Offer')

                # Public Reply 
                await reply_to_comment(comment_id, reply_message, ig_account_id)

                # Private DM
                await send_private_reply(
                    comment_id=comment_id,
                    ig_account_id=ig_account_id,
                    message_text=reply_message,
                    link_url=link_url,
                    button_title=button_title
                )

                # Record that we responded to this user on this media
                services.responded_users_by_media[str(post_id)].add(str(commenter_id))
            else:
                logger.info(f"⏭ Already responded to user_id {commenter_id} for post {post_id}. Skipping keyword reply.")
        
        logger.success(f"✅ Processed Instagram comment {comment_id} successfully.")
        
    except Exception as e:
        logger.error(f"❌ Error processing Instagram comment event: {str(e)}", exc_info=True)

if __name__ == "__main__":
    import asyncio
    asyncio.run(send_ig_message("1241623887433864", "Hello from the test", "17841471805170358", "reply", "org_2zV2OOhodLocEnTybzR8t9MIYCE", temp_id="manual-test"))
