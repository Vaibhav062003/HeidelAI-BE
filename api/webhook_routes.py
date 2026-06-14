# Built-in Modules
import time, json, hmac, hashlib
from fastapi import APIRouter, HTTPException, Response
from datetime import datetime, timezone, timedelta
from fastapi import Request, Header, BackgroundTasks
from fastapi.responses import PlainTextResponse
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
import httpx

# Internal Modules 
from core.managers import manager
from services.automation_service import execute_whatsapp_flow
from services.pma_chatbot import get_vedic_maths_books_message, process_onboarding_if_needed, send_to_pma_chatbot
from services.cloudinary_service import upload_and_send_whatsapp_media_background
from services.contacts_service import store_contact_background
from services.payment_service import build_idempotency_key, process_cashfree_event, verify_cashfree_signature
from services.heidelai_bot import send_to_chatbot
from services.agent_service import send_to_agent
from services.wa_service import *
from services.ig_service import *
from services.utils import normalize_phone
from schemas.models import ActionButton, InteractiveUrlButtonMessage, ReplyButton, InteractiveReplyButtonMessage
from schemas.models import MessageRole
from database import get_mongo_db
from config.settings import (
    VERIFY_TOKEN, APP_SECRET, HEIDELAI_ORG_WA_ID, DEV_WEBHOOK_URL, TEST_WEBHOOK_URL, ENVIRONMENT, PARIJAT_ACADEMY_ORG_WA_ID, 
    LEE_DEALER_ORG_WA_ID, KARAN_ORG_WA_ID, INSTAGRAM_APP_SECRET, GIGA_ORG_WA_ID,
    EGENIE_WA_ID, EGENIE_IG_ID, SAHIL_WA_ID, SAHIL_IG_ID, KOMAL_ORG_WA_ID)
from core.services import services
from loguru import logger
from services.webhook_service import calculate_whatsapp_reply_window
from services.automation_trigger_handler import check_and_trigger_automations

router = APIRouter(prefix="/webhook", tags=["Webhooks"])
db = get_mongo_db()

async def forward_webhook_to_dev(body: bytes, headers: dict):
    """
    Forwards the raw webhook body and necessary headers to the dev environment.
    """
    async with httpx.AsyncClient() as client:
        try:
            # We use `content=body` to send the exact raw bytes, 
            # preserving the integrity of the payload for signature validation.
            response = await client.post(DEV_WEBHOOK_URL, content=body, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully replicated webhook to Dev. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to forward webhook to Dev: {e}")
            
async def forward_webhook_to_test(body: bytes, headers: dict):
    """
    Forwards the raw webhook body and necessary headers to the test environment.
    """
    async with httpx.AsyncClient() as client:
        try:
            # We use `content=body` to send the exact raw bytes, 
            # preserving the integrity of the payload for signature validation.
            response = await client.post(TEST_WEBHOOK_URL, content=body, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully replicated webhook to Test. Status: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to forward webhook to Test: {e}")

def validate_signature(raw_body: bytes, signature: str) -> bool:
    try:
        payload = json.loads(raw_body)
        platform = payload.get("object", "")
    except Exception:
        return False

    if platform == "instagram":
        secret = INSTAGRAM_APP_SECRET
    else:
        secret = APP_SECRET  # whatsapp + anything else

    expected = hmac.new(
        key=secret.encode('utf-8'),
        msg=raw_body,
        digestmod=hashlib.sha256
    ).hexdigest()

    our_sig = f"sha256={expected}"

    if not hmac.compare_digest(our_sig, signature):
        logger.error("🚨 SIGNATURE MISMATCH DETECTED 🚨")
        logger.error(f"1. Platform:          {platform}")
        logger.error(f"2. Secret Prefix:     {secret[:4]}*** (Length: {len(secret)})")
        logger.error(f"3. Meta's Signature:  {signature}")
        logger.error(f"4. Our Signature:     {our_sig}")
        logger.error(f"5. Raw Bytes Length:  {len(raw_body)}")
        return False

    return True


# Middleware or decorator to validate signature for POST requests
async def signature_required(request: Request, x_hub_signature_256: str = Header(None)):
    # Get the raw request payload bytes.
    body = await request.body() 

    if not x_hub_signature_256:
        raise HTTPException(status_code=403, detail="Signature missing")

    # Pass the raw `body` directly into the validator
    if not validate_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    return True

def remove_expired_message_ids():
    current_time = time.time()
    expired_ids = []

    # Iterate through the dictionary to find expired message IDs
    for message_id, timestamp in services.processed_message_ids.items():
        if current_time - timestamp > services.EXPIRY_TIME_SECONDS:
            expired_ids.append(message_id)

    # Remove the expired message IDs
    for message_id in expired_ids:
        del services.processed_message_ids[message_id]

@router.get("")
async def verify_webhook(request: Request):
    # This is for the webhook verification (GET request)
    args = request.query_params
    if args.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=str(args.get("hub.challenge")))
    return {"status": "Verification failed"}

# TODO: add caching at relevant spots in webhook
@router.post("")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None), background_tasks: BackgroundTasks = None):
    """
    This weebhook accepts POST requests from META with a valid signature only
    """
    await signature_required(request, x_hub_signature_256)

    logger.success("Signature validated successfully.")

    # First, validate the signature
    body = await request.body()

    # ── Extract account ID and object type ──────────────────────────────────
    if ENVIRONMENT == "production":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Webhook received non-JSON body")
            return Response(status_code=400)

        object_type: str = payload.get("object", "")
        account_id: str | None = None

        try:
            account_id = payload["entry"][0]["id"]
        except (KeyError, IndexError):
            logger.warning("Could not extract entry[0].id from webhook payload")
    
        if (object_type == "whatsapp_business_account" and (account_id == SAHIL_WA_ID or account_id == EGENIE_WA_ID )) or (object_type == "instagram" and (account_id == SAHIL_IG_ID or account_id == EGENIE_IG_ID)):
            logger.info(f"WhatsApp/Instagram webhook matched allowed account {account_id}. Forwarding.")
            forward_headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() in {"content-type", "x-hub-signature", "x-hub-signature-256"}
            }
            background_tasks.add_task(forward_webhook_to_dev, body=body, headers=forward_headers)
            return Response(status_code=200)

    payload_str = body.decode('utf-8')

    # Now parse the payload
    payload = json.loads(payload_str)

    # Remove expired message IDs
    remove_expired_message_ids()

    logger.info(f"Incoming webhook payload:\n{json.dumps(payload, indent=2)}")

    if payload["object"] == "whatsapp_business_account":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                whatsapp_business_id = entry.get("id")
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    
                    # Validate whatsapp_business_id exists
                    if not whatsapp_business_id:
                        logger.warning("Missing WhatsApp business ID in webhook payload")
                        continue
                    
                    # Find the connection to get user details
                    connection = db.whatsapp_connections.find_one(
                        {"wa_id": whatsapp_business_id}
                    )
                    
                    if not connection:
                        logger.error(f"No connection found for WhatsApp business ID: {whatsapp_business_id}")
                        return {"status": "ok"}

                    # ============================================================================
                    # Handle status updates (message delivery/read receipts)
                    # ============================================================================
                    statuses = value.get("statuses", [])
                    for status in statuses:
                        message_id = status.get("id")
                        status_type = status.get("status")
                        
                        if not message_id or not status_type:
                            continue
                        
                        logger.info(f"WhatsApp Status Update Received - Message ID: {message_id}, Status: {status_type}")
                        
                        # Update the message status in the database
                        broadcast_message = await update_whatsapp_message_status(
                            org_id=connection.get("org_id"),
                            message_id=message_id,
                            status=status_type,
                            # status_timestamp=status.get("timestamp")
                        )

                        # Propagate delivery status to Google Sheet automation tracking
                        # (no-op if message_id doesn't exist in the sheet sent collection)
                        try:
                            from services.google_sheet_automation import update_sheet_sent_status
                            await update_sheet_sent_status(
                                org_id=connection.get("org_id"),
                                message_id=message_id,
                                status=status_type,
                            )
                        except Exception as e:
                            logger.debug(f"Sheet status propagation skipped for {message_id}: {e}")

                        if not broadcast_message:
                            logger.warning(f"No message found with ID {message_id} to update status.")
                            continue

                        if broadcast_message:
                            if "_id" in broadcast_message:
                                broadcast_message["_id"] = str(broadcast_message["_id"])

                            await broadcast_main_ws(
                                platform_id=whatsapp_business_id,
                                platform_type="whatsapp",
                                event_type="message_status_update",
                                payload={"message": broadcast_message}
                            )

                        return {"status": "ok"}
                    # ============================================================================
                    
                    org_id = connection.get("org_id") 
                    access_token = connection.get("access_token")

                    # Get organization's automation flow collection
                    flow_collection_name = f"wa_bot_flows_{org_id}"
                    flow_collection = db[flow_collection_name]

                    # Get sender name from contacts if available
                    sender_name2 = "Unknown Sender"
                    contacts = value.get("contacts", [])
                    if contacts:
                        for contact in contacts:
                            sender_name2 = contact.get("profile", {}).get("name", "Unknown Sender")

                    # Process messages in the webhook
                    messages = value.get("messages", [])
                    if messages:
                        for message in messages:

                            # REACTION TYPE MESSAGES #######################################################################
                            if (message.get("reaction") is not None):

                                # message_timestamp = message.get("timestamp")
                                emoji = message.get("reaction").get("emoji")
                                message_id = message.get("reaction").get("message_id")
                                action = "react" if emoji else "unreact"
                                timestamp = datetime.now()

                                key = f"{message_id}_{emoji}_{action}"

                                # Check for duplicate messages reactions / if the message ID is already processed
                                if key in services.processed_message_reaction_ids:
                                    logger.info("Duplicate message, skipping processing.")
                                    continue
                                
                                # Add message ID with the current timestamp
                                services.processed_message_reaction_ids[key] = timestamp

                                # Extract essential data
                                customer_phone_no = normalize_phone(message.get("from"), is_canonical=True)
                                customer_name = sender_name2
                                message_type = message.get("type")

                                # Get user-specific collections
                                messages_collection_name = f"messages_{org_id}"
                                conversations_collection_name = f"conversations_{org_id}"
                                
                                # Get or create collections
                                messages_collection = db[messages_collection_name]
                                conversations_collection = db[conversations_collection_name]
                                
                                # Create conversation ID
                                conversation_id = f"whatsapp_{customer_phone_no}"

                                logger.info(f"Reaction Event: {customer_name} {action} to message ID {message_id} {f'with emoji: {emoji}' if action == 'react' else ''} ")

                                if (action == "react"):
                                    # Update the message with the reaction info in  database and it for broadcasting
                                    try:
                                        # Update the message with the reaction info
                                        broadcast_message = messages_collection.find_one_and_update(
                                            {"message_id": message_id},
                                            {"$set": {
                                                "reaction": {
                                                    "emoji": emoji,
                                                    "username": customer_phone_no,
                                                    "full_name": customer_name,
                                                    # "profile_picture_url": user_details.get("profile_pic"),
                                                    "timestamp": timestamp
                                                }
                                            }}, 
                                            return_document=ReturnDocument.AFTER
                                        )
                                        
                                        if not broadcast_message:
                                            logger.warning(f"Message with ID {message_id} not found for reaction update")
                                            return {"status": "ok"}
                                            
                                    except Exception as e:
                                        logger.error(f"Error updating message with reaction: {e}")
                                        return {"status": "error", "message": str(e)}
                                    
                                elif (action == "unreact"):
                                    # Update the message with the reaction info in  database and it for broadcasting
                                    try:
                                        # Update the message with the reaction info
                                        broadcast_message = messages_collection.find_one_and_update(
                                            {"message_id": message_id},
                                            {"$unset": {"reaction": 1}}, 
                                            return_document=ReturnDocument.AFTER
                                        )
                                        
                                        if not broadcast_message:
                                            logger.warning(f"Message with ID {message_id} not found for reaction update")
                                            return {"status": "ok"}
                                            
                                    except Exception as e:
                                        logger.error(f"Error updating message with reaction: {e}")
                                        return {"status": "error", "message": str(e)}
                                


                                if "_id" in broadcast_message:
                                    broadcast_message["_id"] = str(broadcast_message["_id"])

                                await broadcast_main_ws(
                                    platform_id=whatsapp_business_id,
                                    platform_type="whatsapp",
                                    event_type="message_reaction",
                                    payload={"message": broadcast_message}
                                    )

                                # Broadcast the reaction update to connected clients
                                if whatsapp_business_id in services.whatsapp_connections_map:
                                    logger.success(f"Broadcasting message_reaction event to Frontend for {whatsapp_business_id}")
                                    await broadcast_whatsapp_message(whatsapp_business_id, "message_reaction", {
                                        "message": broadcast_message
                                    })

                                return {"status": "ok"}

                            # Skip processing if not a message
                            if "id" not in message:
                                continue
                                
                            # Check if message ID is already processed
                            message_id = message["id"]
                            if message_id in services.processed_message_ids:
                                logger.info("Duplicate WhatsApp message, skipping processing.")
                                continue
                                
                            # Add message ID with the current timestamp
                            services.processed_message_ids[message_id] = time.time()
                            
                            # Extract essential data
                            customer_phone_no = normalize_phone(message.get("from"), is_canonical=True)
                            message_type = message.get("type")
                            message_context = message.get("context")

                            # Get user-specific collections
                            messages_collection_name = f"messages_{org_id}"
                            conversations_collection_name = f"conversations_{org_id}"
                            
                            # Get or create collections
                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]
                            
                            # Create conversation ID
                            conversation_id = f"whatsapp_{customer_phone_no}"

                            context_type = None
                            context = None

                            try:
                                reply_window_info = calculate_whatsapp_reply_window(message)
                                reply_window_expiry_time_utc = reply_window_info["expiry_time_utc"]
                                is_ad_referral = reply_window_info["is_ad_referral"]
                            except ValueError as exc:
                                logger.warning(
                                    f"Failed to calculate WhatsApp reply window due to invalid/missing timestamp; "
                                    f"using default 24h window. Error: {exc}"
                                )
                                reply_window_expiry_time_utc = datetime.now(timezone.utc) + timedelta(hours=24)
                                is_ad_referral = False

                            if message_context:
                                replied_message_id = message_context.get("id")

                                if replied_message_id:
                                    logger.info(f"Message is a reply to message ID: {replied_message_id}")

                                    context_type = "message_reply"
                                    context = {
                                        "mid": replied_message_id
                                    }
                                    
                            # Get conversation doc to check if AI is enabled
                            convo_doc = conversations_collection.find_one({"conversation_id": conversation_id})
                            is_conv_ai_enabled = convo_doc.get("is_ai_enabled", False) if convo_doc else True
                            stage = convo_doc.get("onboarding_stage", "welcome") if convo_doc else "welcome"
                            
                            org_doc = db.organizations.find_one({"org_id": org_id},{"is_ai_enabled": 1})
                            is_org_ai_enabled = org_doc.get("is_ai_enabled", False) if org_doc else False
                            
                            is_heidelai = whatsapp_business_id == HEIDELAI_ORG_WA_ID
                            is_pma_wa = whatsapp_business_id == PARIJAT_ACADEMY_ORG_WA_ID
                            is_lee_dealer_wa = whatsapp_business_id == LEE_DEALER_ORG_WA_ID
                            is_karan_wa = whatsapp_business_id == KARAN_ORG_WA_ID
                            is_technet_wa = whatsapp_business_id == TECHNET_ORG_WA_ID
                            is_production = ENVIRONMENT == "production"
                            is_dev = ENVIRONMENT == "development"
                            is_test = ENVIRONMENT == "test"
                            # fetch org + convo flags
                            ai_enabled = is_org_ai_enabled and is_conv_ai_enabled

                            logger.info(f"is_heidelai: {is_heidelai}")
                            logger.info(f"is_pma_wa: {is_pma_wa}")
                            logger.info(f"is_lee_dealer_wa: {is_lee_dealer_wa}")
                            logger.info(f"is_technet_wa: {is_technet_wa}")
                            logger.info(f"is_production: {is_production}")
                            logger.info(f"is_dev: {is_dev}")
                            logger.info(f"is_test: {is_test}")
                            logger.info(f"ai_enabled: {ai_enabled}") 

                            whatsapp_flow = flow_collection.find_one({"trigger_from": "webhook", "trigger_event": "message_received", "status": "active"})
                            logger.info(
                                f"[{org_id}] WhatsApp automation flow: "
                                f"{'found' if whatsapp_flow else 'no active flow found'}"
                            )

                            # Extract message content based on message type
                            if message_type == "text":
                                content = message.get("text", {}).get("body", "")

                                # Storing messages from customers in the DB
                                # Create or update conversation
                                conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=content,
                                        last_sender="customer",
                                        reply_window_expiry=reply_window_expiry_time_utc,
                                        is_ad_referral=is_ad_referral,
                                        mode="reply"
                                    )
                                    
                                # Store in WhatsApp-specific collections
                                message_id = await store_whatsapp_message(
                                        org_id=org_id,
                                        content=content,
                                        conversation_id=conversation_id,
                                        customer_phone_no=customer_phone_no,
                                        type=message_type,
                                        payload=message.get("payload"),
                                        sender_name=sender_name2,
                                        recipient_id=whatsapp_business_id,
                                        role=MessageRole.CUSTOMER,
                                        message_id=message_id,  # Use the ID from the webhook for deduplication
                                        raw_data=message,
                                        mode="reply",
                                        context_type=context_type,
                                        context=context
                                    )

                                contact_data = {
                                    "whatsapp_id": whatsapp_business_id,
                                    "full_name": sender_name2,
                                    "phone_number": customer_phone_no,
                                    "country": None,
                                    "profile_url": None,
                                    "conversation_id": f"whatsapp_{customer_phone_no}",
                                    "email": None,
                                    "categories": []
                                }
                                store_contact_background(background_tasks, org_id, "whatsapp", contact_data)



                                #  Don't execute for heidel_ai and pma as they are not on whatsapp flow yet but have AI
                                if ai_enabled and is_dev and not is_heidelai and not is_pma_wa:
                                    await read_message_and_typing_indicator(whatsapp_id=whatsapp_business_id, message_id=message_id)

                                    # Execute the WhatsApp Automation Flow first (if it exists)
                                    if whatsapp_flow:
                                        automation_response = await execute_whatsapp_flow(
                                            flow=whatsapp_flow,
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            stage=stage,
                                            trigger_event="message_received",
                                            trigger_messages=whatsapp_flow.get("trigger_messages"),
                                            message_content=content,
                                            customer_name=sender_name2,
                                            whatsapp_business_id=whatsapp_business_id,
                                            message_type=message_type,
                                        )

                                        # If the flow handled the message successfully, exit early.
                                        if automation_response:
                                            return {"status": "ok"}

                                    if not convo_doc:
                                        convo_doc = conversations_collection.find_one({"conversation_id": conversation_id})

                                    # Fallback to Agent (executes if no flow exists OR flow didn't return a response)
                                    chatbot_response = await send_to_agent(
                                        conversation_id=conversation_id,
                                        user_input=content,
                                        persona=convo_doc.get("persona", "fallback"),
                                        location=convo_doc.get("location", "india"),
                                        customer_name=sender_name2,
                                        org_id=org_id,
                                        source="webhook",
                                    )

                                    final_response = chatbot_response["final_response"]

                                    # Update the database with the agent's response
                                    conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=final_response,
                                        last_sender="ai",
                                        mode="ai",
                                    )

                                    # Send the WhatsApp message
                                    res = await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": final_response,
                                        "sender_id": whatsapp_business_id,
                                    }, org_id, "ai")

                                    return {"status": "ok"}

                                # Handling messages received on HeidelAI's whatsapp number
                                if is_heidelai and ai_enabled and is_production:

                                    chatbot_response = await send_to_chatbot(
                                        customer_phone_no=customer_phone_no,
                                        message_content=content
                                    )
                                
                                    # Storing chatbot response in the DB
                                    # Create or update conversation
                                    conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=chatbot_response,
                                        last_sender="ai",
                                        mode="reply"
                                    )
                                    
                                    # Storing in WhatsApp messages collection is handled by send_whatsapp_message()
                                    res = await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": chatbot_response,
                                        "sender_id": HEIDELAI_ORG_WA_ID,
                                    }, org_id, "ai")

                                    return {"status": "ok"}

                                elif is_pma_wa and ai_enabled and is_production:

                                    flow_response = await process_onboarding_if_needed(user_input=content, conversation_id=conversation_id, org_id=org_id, stage=stage, whatsapp_business_id=whatsapp_business_id, sender_name=sender_name2)
                                        
                                    if flow_response:
                                        await send_interactive_whatsapp_message(
                                            org_id=org_id,
                                            conversation_id=conversation_id,
                                            sender_id=whatsapp_business_id,
                                            interactive_message=flow_response
                                        )
                                    
                                        return {"status": "ok"}

                                    chatbot_response = await send_to_pma_chatbot(
                                            conversation_id=conversation_id,
                                            user_input=content,
                                            persona=convo_doc.get("persona", "fallback"),
                                            location=convo_doc.get("location", "india"),
                                            customer_name=sender_name2,
                                            org_id=org_id,
                                            source="webhook",
                                    )

                                    final_response = chatbot_response["final_response"]

                                    # Storing chatbot response in the DB
                                    # Create or update conversation
                                    conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=final_response,
                                        last_sender="ai",
                                        mode="ai",
                                    )

                                    # Storing in WhatsApp messages collection is handled by send_whatsapp_message()
                                    res = await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": final_response,
                                        "sender_id": PARIJAT_ACADEMY_ORG_WA_ID,
                                        }, org_id, "ai")

                                elif ai_enabled and is_production and is_lee_dealer_wa:
                                    # Fallback to Agent (executes if no flow exists OR flow didn't return a response)

                                    print("*" * 80 )
                                    print("Calling agent for Lee Dealer WhatsApp")
                                    chatbot_response = await send_to_agent(
                                        conversation_id=conversation_id,
                                        user_input=content,
                                        persona="dealers",
                                        location=convo_doc.get("location", "india"),
                                        customer_name=sender_name2,
                                        org_id=org_id,
                                        source="webhook",
                                    )

                                    final_response = chatbot_response["final_response"]

                                    # Update the database with the agent's response
                                    conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=final_response,
                                        last_sender="ai",
                                        mode="ai",
                                    )

                                    # Send the WhatsApp message
                                    res = await send_whatsapp_message({
                                        "conversation_id": conversation_id,
                                        "content": final_response,
                                        "sender_id": whatsapp_business_id,
                                    }, org_id, "ai")

                                    return {"status": "ok"}
                                
                                return {"status": "ok"}

                            elif message_type in ["image", "video", "document"]:
                                media_data = message[message_type]
                                media_url = media_data["url"]
                                media_id = media_data["id"]
                                mime_type = media_data["mime_type"]
                                media_name = media_data.get("filename", f"{message_type}_{media_id}")

                                if message_type == "image":
                                    content = media_data.get("caption", "Shared an image")
                                elif message_type == "video":
                                    content = media_data.get("caption", "Shared a video")
                                elif message_type == "document":
                                    content = media_data.get("caption", "Shared a document")

                                background_tasks.add_task(
                                    upload_and_send_whatsapp_media_background,
                                    media_url=media_url,
                                    media_id=media_id,
                                    org_id=org_id,
                                    mime_type=mime_type,
                                    access_token=access_token,
                                    content=content,
                                    message_type=message_type,
                                    whatsapp_business_id=whatsapp_business_id,
                                    customer_phone_no=customer_phone_no,
                                    sender_name2=sender_name2,
                                    message_id=message_id,
                                    conversation_id=conversation_id,
                                    reply_window_expiry_time_utc=reply_window_expiry_time_utc,
                                    is_ad_referral=is_ad_referral,
                                    media_name=media_name,
                                    caption=media_data.get("caption", None)
                                )

                                contact_data = {
                                "whatsapp_id": whatsapp_business_id,
                                "full_name": sender_name2,
                                "phone_number": customer_phone_no,
                                "country": None,
                                "profile_url": None,
                                "conversation_id": f"whatsapp_{customer_phone_no}",
                                "email": None,
                                "categories": []
                                }

                                store_contact_background(background_tasks, org_id, "whatsapp", contact_data)

                                return {"status": "ok"}
                            elif message_type == "audio":
                                content = "Shared an audio"
                            elif message_type == "location":
                                content = "Shared a location"
                            elif message_type == "button":
                                content = f"[Button: {message.get('button', {}).get('payload', 'Unknown')}]"
                            elif message_type == "order":
                                content = "Shared an order"
                            elif message_type == "interactive":
                                interactive_data = message.get("interactive", {})
                                
                                # Determine if it's a list or button reply
                                interaction_type = interactive_data.get("type")
                    
                                # Larger scope variables to access later below in the block
                                selection_id = None
                                selection_title = None
                    
                                # Extract the specific payload/ID and title into larger scope variables
                                if interaction_type == "list_reply":
                                    reply = interactive_data.get("list_reply", {})
                                    logger.info("Interaction type is list")

                                    selection_id = reply.get("id")      # Predefined Row ID
                                    selection_title = reply.get("title")
                                        
                                elif interaction_type == "button_reply":
                                    reply = interactive_data.get("button_reply", {})
                                    logger.info("Interaction type is button")

                                    selection_id = reply.get("id")      # Predefined Button ID
                                    selection_title = reply.get("title")
                
                                content = selection_title
                                logger.info(f"Extracted content from interactive message: {content}, selection_id: {selection_id}")
                                # Storing messages from customers in the DB
                                # Create or update conversation
                                conversation_id = await create_or_update_whatsapp_conversation(
                                        org_id=org_id,
                                        recipient_id=whatsapp_business_id,
                                        customer_phone_no=customer_phone_no,
                                        customer_name=sender_name2,
                                        last_message=content,
                                        last_sender="customer",
                                        reply_window_expiry=reply_window_expiry_time_utc,
                                        is_ad_referral=is_ad_referral,
                                        mode="reply"
                                    )
                                    
                                # Store in WhatsApp-specific collections
                                message_id = await store_whatsapp_message(
                                        org_id=org_id,
                                        content=content,
                                        conversation_id=conversation_id,
                                        customer_phone_no=customer_phone_no,
                                        type=message_type,
                                        payload=interactive_data,
                                        sender_name=sender_name2,
                                        recipient_id=whatsapp_business_id,
                                        role=MessageRole.CUSTOMER,
                                        message_id=message_id,  # Use the ID from the webhook for deduplication
                                        raw_data=message,
                                        mode="reply",
                                        context_type=context_type,
                                        context=context
                                    )

                                if whatsapp_flow and ai_enabled and is_dev:
                                    automation_response = await execute_whatsapp_flow(
                                        flow=whatsapp_flow,
                                        org_id=org_id,
                                        conversation_id=conversation_id,
                                        stage=stage,
                                        trigger_event="message_received",
                                        trigger_messages=whatsapp_flow.get("trigger_messages") if isinstance(whatsapp_flow, dict) else None,
                                        message_content=selection_id if selection_id else None,
                                        customer_name=sender_name2,
                                        whatsapp_business_id=whatsapp_business_id,
                                        message_type=message_type,
                                    )

                                    if automation_response:
                                        return {"status": "ok"}
                                
                                if is_pma_wa and ai_enabled and is_production:
                                    if selection_id:
                                        match selection_id:
                                            case "india" | "abroad":
                                                flow_response = await process_onboarding_if_needed(user_input=selection_id, conversation_id=conversation_id, org_id=org_id, stage="awaiting_location", whatsapp_business_id=whatsapp_business_id, sender_name=sender_name2)
                                                if flow_response:
                                                    await send_interactive_whatsapp_message(
                                                        org_id=org_id,
                                                        conversation_id=conversation_id,
                                                        sender_id=whatsapp_business_id,
                                                        interactive_message=flow_response
                                                    )
                                                    return {"status": "ok"}

                                            case "free_master_class":
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=InteractiveUrlButtonMessage(
                                                        type="cta_url",
                                                        header={
                                                            "type": "image",
                                                            "image": {
                                                                "link": "https://res.cloudinary.com/djpc5zfgz/image/upload/v1772559110/intro_image_pma_p0tae1.jpg"
                                                            }
                                                        },
                                                        body_text=(
                                                            "🌟 FREE VEDIC MATHS MASTERCLASS 🌟\n\n"
                                                            "Does your child feel Exam Stress in Maths? 😟\n"
                                                            "✨ Now let the Exam Stress Just Go Away! ✨\n"
                                                            "Give your child the power of Vedic Maths and watch confidence grow! 🚀\n\n"
                                                            "🎯 In This FREE Masterclass Students Will Learn:\n"
                                                            "✅ Faster Calculations\n"
                                                            "✅ Better Accuracy\n"
                                                            "✅ Improved Concentration\n"
                                                            "✅ More Confidence in Exams\n"
                                                            "✅ Fear of Maths will disappear\n\n"
                                                            "🎓 100% FREE Session\n"
                                                            "🚀 Limited Seats, Register Now!\n\n" 
                                                            "🌐 www.paarijatacademy.in\n"
                                                            "📞 Call / WhatsApp:\n"
                                                            "8055579965 / 9503498556"
                                                        ),
                                                        button_text="Register Now",
                                                        url="https://forms.gle/xWnKQCt8CVepAbYb9"
                                                    )
                                                )

                                                return {"status": "ok"}

                                            case "abacus_students_5_to_9":
                                                # Send info about the Abacus course
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "🔢 *Abacus Program*\n\n"
                                                        
                                                        "Age: *5-9 years*\n\n"
                                                        
                                                        "Course Structure:\n"
                                                        "- *8 levels*\n"
                                                        "- Classes: *2 days per week*"

                                                        "\n\n*Benefits*\n"
                                                        "✅ Improves memory\n"
                                                        "✅ Strong number sense\n"
                                                        "✅ Faster mental calculations\n"
                                                        "✅ Better concentration\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}
                                               
                                            case "vedic_maths_regular_9_plus":
                                                # Send info about Vedic Maths for Students
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=InteractiveReplyButtonMessage(
                                                        type="button",
                                                        header={
                                                            "type": "text",
                                                            "text": "🧠 Vedic Maths Program"
                                                        },
                                                        body_text=(
                                                            "Age: *9 years and above*\n\n"
                                                            
                                                            "Course Structure:\n"
                                                            "- *Five Structured Volumes*\n"
                                                            "- Classes: *2 days per week*"

                                                            "\n\n*Benefits*\n"
                                                            "✅ Faster calculations\n"
                                                            "✅ Improves concentration\n"
                                                            "✅ Boosts confidence in Maths\n"
                                                            "✅ Helpful for school & competitive exams\n\n"
                                                        ),
                                                        buttons=[
                                                            ActionButton(
                                                                type="reply",
                                                                reply=ReplyButton(
                                                                    id="contact_details_for_vedic_maths_regular", 
                                                                    title="Contact Details"
                                                                )
                                                            )
                                                        ],
                                                    )
                                                )

                                                return {"status": "ok"}                                

                                            case "vedic_maths_books":
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=get_vedic_maths_books_message()
                                                )
                                                
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id": whatsapp_business_id,
                                                        "conversation_id": conversation_id,
                                                        "content": (
                                                            "https://youtube.com/watch?v=ztvJiASRm0w&si=t2tiBQN9ZqO92Ua3"
                                                        ),
                                                        "preview_url": True,
                                                    }
                                                )

                                                return {"status": "ok"}

                                            case "vedic_maths_teacher":
                                                # Send info about Teacher Training
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "🔢 *Vedic Maths Teacher Certification*\n\n"
                                                        
                                                        "Duration: *40 hours*\n"
                                                        "Schedule: *3 days per week*\n\n"

                                                        "*Benefits*\n"
                                                        "✅ Become a Certified Vedic Maths Trainer\n"
                                                        "✅ Start your own classes\n"
                                                        "✅ Teach online or offline\n"
                                                        "✅ Earn additional income\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}

                                            case "contact_details_for_vedic_maths_regular":
                                                await send_interactive_whatsapp_message(
                                                    org_id=org_id,
                                                    conversation_id=conversation_id,
                                                    sender_id=whatsapp_business_id,
                                                    interactive_message=InteractiveUrlButtonMessage(
                                                        type="cta_url",
                                                        body_text=(
                                                            "Thanks for showing interest in our *Vedic Maths Program*\n\n"

                                                            "Please click on the below button for contacting us."
                                                        ),
                                                        url="https://paarijatacademy.in/courses/",
                                                        button_text="Details & Contact"
                                                    )
                                                )

                                                return {"status": "ok"}
                                        
                                        
                                            case "order_books":
                                                await send_whatsapp_message(
                                                    org_id=org_id,
                                                    mode="reply",
                                                    message={
                                                        "sender_id" : whatsapp_business_id,
                                                        "conversation_id" : conversation_id,
                                                        "content": (
                                                        "*Please contact below numbers to place order*\n\n"
                                                        
                                                        "📱 9503498556\n"
                                                        "📱 8055579965\n\n"
                                                    )
                                                    }
                                                )

                                                return {"status": "ok"}

                                            # --- Fallback for unknown IDs ---
                                            case _:
                                                chatbot_response = await send_to_pma_chatbot(
                                                    conversation_id=conversation_id,
                                                    user_input=content,
                                                    persona="fallback",
                                                    location="india",
                                                    customer_name=sender_name2,
                                                    org_id=org_id,
                                                    source="webhook",
                                                )

                                                final_response = chatbot_response["final_response"]

                                                # Storing chatbot response in the DB
                                                # Create or update conversation
                                                conversation_id = await create_or_update_whatsapp_conversation(
                                                    org_id=org_id,
                                                    recipient_id=whatsapp_business_id,
                                                    customer_phone_no=customer_phone_no,
                                                    customer_name=sender_name2,
                                                    last_message=final_response,
                                                    last_sender="ai",
                                                    mode="ai",
                                                )

                                                # Storing in WhatsApp messages collection is handled by send_whatsapp_message()
                                                res = await send_whatsapp_message({
                                                    "conversation_id": conversation_id,
                                                    "content": final_response,
                                                    "sender_id": PARIJAT_ACADEMY_ORG_WA_ID,
                                                    }, org_id, "ai")

                                return {"status":"ok"}
                                
                            elif message_type == "unsupported":
                                content = "[Unsupported message type]"
                            elif message_type == "reaction":
                                continue  # Handled above
                            else:
                                content = f"[Unsupported message type: {message_type}]"
                           
                            logger.info(f"WhatsApp Message Received from {sender_name2}, Message: {content}")
                            
                            # Create or update conversation
                            conversation_id = await create_or_update_whatsapp_conversation(
                                org_id=org_id,
                                recipient_id=whatsapp_business_id,
                                customer_phone_no=customer_phone_no,
                                customer_name=sender_name2,
                                last_message=content,
                                last_sender="customer",
                                reply_window_expiry=reply_window_expiry_time_utc,
                                is_ad_referral=is_ad_referral,
                                mode="reply",
                            )
                            
                            # Store in WhatsApp-specific collections
                            message_id = await store_whatsapp_message(
                                org_id=org_id,
                                content=content,
                                conversation_id=conversation_id,
                                customer_phone_no=customer_phone_no,
                                type=message_type,
                                payload=message.get("payload"),
                                sender_name=sender_name2,
                                recipient_id=whatsapp_business_id,
                                role=MessageRole.CUSTOMER,
                                message_id=message_id,  # Use the ID from the webhook for deduplication
                                raw_data=message,
                                mode="reply",
                                context_type=context_type,
                                context=context
                            )

                            # Check if conversation exists
                            existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
                            conversation = conversations_collection.find_one({"conversation_id": conversation_id})

                            if conversation:
                                # Convert ObjectId to string for JSON serialization
                                conversation["_id"] = str(conversation["_id"])
                                # Convert datetime to ISO string format
                                if "timestamp" in conversation:
                                    conversation["timestamp"] = conversation["timestamp"].isoformat()
                                if "last_message_timestamp" in conversation:
                                    conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()

                            # Broadcast WhatsApp-specific notifications first
                            if not existing_conversation:
                                await broadcast_whatsapp_message(whatsapp_business_id, "new_conversation", {
                                    "conversation": conversation
                                })
                            else:
                                await broadcast_whatsapp_message(whatsapp_business_id, "conversation_updated", {
                                    "conversation": conversation
                                })

                            contact_data = {
                                "whatsapp_id": whatsapp_business_id,
                                "full_name": sender_name2,
                                "phone_number": customer_phone_no,
                                "country": None,
                                "profile_url": None,
                                "conversation_id": f"whatsapp_{customer_phone_no}",
                                "email": None,
                                "categories": []
                                }

                            store_contact_background(background_tasks, org_id, "whatsapp", contact_data)

                            return {"status": "ok"}
    
    if payload["object"] == "instagram":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                ig_account_id = entry.get("id")

                
                # Prithal's code testing
                for changes in entry.get('changes', []):
                    field = changes.get('field')
                    value = changes.get('value')

                    if field == 'comments':
                        await process_comments_events(value, ig_account_id, background_tasks)
                
                for message in entry.get("messaging", []):
                    # Check if this is a deletion event
                    if "message" in message and message["message"].get("is_deleted", False):
                        # Get the message ID that was deleted
                        deleted_message_id = message["message"]["mid"]
                        recipient_id = message["recipient"]["id"]  # Instagram business account ID
                        sender_id = message["sender"]["id"]        # Instagram user ID who deleted the message
                        
                        logger.info(f"Message deletion event detected - mid: {deleted_message_id}")

                        # Find the corresponding Instagram account in our connections
                        instagram_connection = db.instagram_connections.find_one(
                            {"instagram_id": recipient_id, "is_active": True},
                            sort=[("last_updated", -1)]
                        )
                        
                        logger.info(f"Instagram Connection: {instagram_connection}")
                        org_id = instagram_connection.get("org_id")
                        logger.info(f"Org_id: {org_id}")

                        # Get the Instagram-specific collection names
                        messages_collection_name = f"messages_{org_id}"
                        conversations_collection_name = f"conversations_{org_id}" 

                        # Check if collection exists
                        if messages_collection_name in db.list_collection_names():

                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]  
                            
                            # Find the message to be deleted
                            message_to_delete = messages_collection.find_one({"message_id": deleted_message_id})
                            
                            if message_to_delete:
                                conversation_id = message_to_delete.get("conversation_id")
                                
                                # Get the conversation
                                conversation = conversations_collection.find_one({"conversation_id": conversation_id})
                                
                                # Check if this was the last message in the conversation
                                was_last_message = False
                                if conversation and conversation.get("last_message_timestamp"):
                                    # Compare timestamps to see if this was the most recent message
                                    msg_time = message_to_delete.get("timestamp")
                                    conv_time = conversation.get("last_message_timestamp")
                                    
                                    # If the deleted message time matches the conversation's last_message_timestamp
                                    if msg_time and abs((msg_time - conv_time).total_seconds()) < 1:  # Within 1 second
                                        was_last_message = True
                                
                                # Delete the message
                                delete_result = messages_collection.delete_one({"message_id": deleted_message_id})
                                
                                # If this was the last message, update the conversation with previous message
                                if was_last_message:
                                    # Find the new most recent message
                                    newest_message = messages_collection.find_one(
                                        {"conversation_id": conversation_id},
                                        sort=[("timestamp", -1)]  # Sort by timestamp descending
                                    )
                                    
                                    if newest_message:
                                        # Update conversation with new last message and timestamp
                                        conversations_collection.update_one(
                                            {"conversation_id": conversation_id},
                                            {"$set": {
                                                "last_message": newest_message.get("content", ""),
                                                "last_message_timestamp": newest_message.get("timestamp")
                                            }}
                                        )
                                    else:
                                        # If no messages left, set default
                                        conversations_collection.update_one(
                                            {"conversation_id": conversation_id},
                                            {"$set": {
                                                "last_message": "(No messages)",
                                                "last_message_timestamp": datetime.now()
                                            }}
                                        )
                                
                                # Get updated conversation for broadcasting
                                conversation = conversations_collection.find_one({"id": conversation_id})
                                
                                # Format for broadcasting
                                if conversation:
                                    # Convert ObjectId to string for JSON serialization
                                    conversation["_id"] = str(conversation["_id"])
                                    # Convert datetime to ISO string format
                                    if "timestamp" in conversation:
                                        conversation["timestamp"] = conversation["timestamp"].isoformat()
                                    if "last_message_timestamp" in conversation:
                                        conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()

                                    await broadcast_main_ws(
                                        platform_id=recipient_id,
                                        platform_type="instagram",
                                        event_type="message_deleted",
                                        payload={
                                            "message_id": deleted_message_id,
                                            "conversation_id": conversation_id
                                        }
                                    )

                                    await broadcast_main_ws(
                                        platform_id=recipient_id,
                                        platform_type="instagram",
                                        event_type="conversation_updated",
                                        payload={
                                            "conversation": conversation
                                        }
                                    )

                                    # Broadcast both the deletion event and updated conversation
                                    if recipient_id in services.instagram_connections_map and conversation_id:
                                        await broadcast_instagram_message(recipient_id, "message_deleted", {
                                            "message_id": deleted_message_id,
                                            "conversation_id": conversation_id
                                        })

                                    # Update dashboard stats live after deletion
                                    # await broadcast_on_stats_update(org_id)
                                        
                                    await broadcast_instagram_message(recipient_id, "conversation_updated", {
                                        "conversation": conversation
                                    })
                        
                        # Return success early since we've handled the deletion event
                        return {"status": "ok"}
                

                    # Check if the message is a read event
                    if "read" in message:
                        logger.info("Message read event")
                        continue

                    sender_id = message["sender"]["id"]
                    recipient_id = message["recipient"]["id"]
                    timestamp = message["timestamp"]

                    # REACTION TYPE MESSAGES #######################################################################
                    if (message.get("reaction") is not None):

                        message_timestamp = message.get("timestamp")
                        emoji = message.get("reaction").get("emoji")
                        mid = message.get("reaction").get("mid")
                        action = message.get("reaction").get("action")
                        timestamp = datetime.now()

                        key = f"{mid}_{emoji}_{action}"

                        # Check for duplicate messages reactions / if the message ID is already processed
                        if key in services.processed_message_reaction_ids:
                            logger.info("Duplicate message, skipping processing.")
                            continue
                        
                        # Add message ID with the current timestamp
                        services.processed_message_reaction_ids[key] = timestamp

                        # Find the corresponding Instagram account in our connections
                        instagram_connection = db.instagram_connections.find_one(
                            {"instagram_id": recipient_id, "is_active": True},
                            sort=[("last_updated", -1)]
                        )

                        if not instagram_connection:
                            logger.error(f"No active Instagram connection found for recipient ID: {recipient_id}")
                            return {"status": "ok"}
                        
                        org_collection = db.organizations.find_one({"ig_id": recipient_id})

                        if not org_collection:
                            logger.error(f"No active organization found for Instagram ID: {recipient_id}")
                            return {"status": "ok"}

                        org_id = org_collection.get("org_id")

                        if (not org_id):
                            logger.error(f"No active organization ID found for Instagram ID: {recipient_id}")
                            return {"status": "ok"}

                        # get the page access token
                        page_access_token = instagram_connection.get("page_access_token")
                        if not page_access_token:
                            logger.error(f"Page access token not found for recipient ID: {recipient_id}")
                            return {"status": "error", "message": "Page access token not found"}
                        
                        # Get user-specific collection names
                        messages_collection_name = f"messages_{org_id}"
                        conversations_collection_name = f"conversations_{org_id}"
                        
                        # Get or create collections
                        messages_collection = db[messages_collection_name]
                        conversations_collection = db[conversations_collection_name]
                        
                        user_details = await get_ig_username(sender_id, page_access_token)
                        username = user_details.get('username')
                        name = user_details.get('name', username)
                        user = name + " - @" + username

                        logger.info(f"Reaction Event: {user} reacted to message ID {mid} with emoji: {emoji}")

                        if (action == "react"):
                            # Update the message with the reaction info in  database and it for broadcasting
                            try:
                                # Update the message with the reaction info
                                broadcast_message = messages_collection.find_one_and_update(
                                    {"message_id": mid},
                                    {"$set": {
                                        "reaction": {
                                            "emoji": emoji,
                                            "username": username,
                                            "full_name": name,
                                            "profile_picture_url": user_details.get("profile_pic"),
                                            "timestamp": timestamp
                                        }
                                    }}, 
                                    return_document=ReturnDocument.AFTER
                                )
                                
                                if not broadcast_message:
                                    logger.warning(f"Message with ID {mid} not found for reaction update")
                                    return {"status": "ok"}
                                    
                            except Exception as e:
                                logger.error(f"Error updating message with reaction: {e}")
                                return {"status": "error", "message": str(e)}
                            
                        elif (action == "unreact"):
                            # Update the message with the reaction info in  database and it for broadcasting
                            try:
                                # Update the message with the reaction info
                                broadcast_message = messages_collection.find_one_and_update(
                                    {"message_id": mid},
                                    {"$unset": {"reaction": 1}}, 
                                    return_document=ReturnDocument.AFTER
                                )
                                
                                if not broadcast_message:
                                    logger.warning(f"Message with ID {mid} not found for reaction update")
                                    return {"status": "ok"}
                                    
                            except Exception as e:
                                logger.error(f"Error updating message with reaction: {e}")
                                return {"status": "error", "message": str(e)}
                        


                        if "_id" in broadcast_message:
                            broadcast_message["_id"] = str(broadcast_message["_id"])

                        await broadcast_main_ws(
                            platform_id=recipient_id,
                            platform_type="instagram",
                            event_type="message_reaction",
                            payload={"message": broadcast_message}
                            )

                        # Broadcast the reaction update to connected clients
                        if recipient_id in services.instagram_connections_map:
                            logger.success(f"Broadcasting message_reaction event to Frontend for {recipient_id}")
                            await broadcast_instagram_message(recipient_id, "message_reaction", {
                                "message": broadcast_message
                            })

                        return {"status": "ok"}


                    message = message.get("message")
                    
                    if (message is None):
                        logger.warning("No message content, skipping.")
                        return {"status": "ok"}

                    # if no text content in the message, skip processing it
                    if (message.get("text") is None and message.get("attachments") is None):
                        logger.warning("Event can't be handled (not supported), skipping processing.")
                        return {"status": "ok"}
                    
                    # Check for duplicate messages / if the message ID is already processed
                    ig_message_id = message["mid"]

                    if ig_message_id in services.processed_message_ids:
                        logger.warning("Duplicate message, skipping processing.")
                        continue
                    
                    # Add message ID with the current timestamp
                    services.processed_message_ids[ig_message_id] = time.time()

                    message_echo = message.get("is_echo", False)
                    
                    if message_echo:
                        recipient_id, sender_id = sender_id, recipient_id

                    # Find the corresponding Instagram account in our connections
                    instagram_connection = db.instagram_connections.find_one(
                        {"instagram_id": recipient_id, "is_active": True},
                        sort=[("last_updated", -1)]
                    )

                    if not instagram_connection:
                        logger.error(f"No active Instagram connection found for recipient ID: {recipient_id}")
                        return {"status": "ok"}
                    
                    org_collection = db.organizations.find_one({"ig_id": recipient_id})

                    if not org_collection:
                        logger.error(f"No active organization found for Instagram ID: {recipient_id}")
                        return {"status": "ok"}

                    org_id = org_collection.get("org_id")

                    if (not org_id):
                        logger.error(f"No active organization ID found for Instagram ID: {recipient_id}")
                        return {"status": "ok"}

                    # get the page access token
                    page_access_token = instagram_connection.get("page_access_token")
                    if not page_access_token:
                        logger.error(f"Page access token not found for recipient ID: {recipient_id}")
                        return {"status": "error", "message": "Page access token not found"}
                    
                    # ATTACHMENT TYPE MESSAGES #######################################################################
                    attachment_type = "text"
                    attachment_payload = None

                    context_type = None
                    context = None

                    if (message.get("attachments") is not None):
                        logger.info("Message has attachments")
                        for attachment in message.get("attachments"):
                            attachment_type = str(attachment.get("type"))
                            attachment_payload = attachment.get("payload")

                    # Handle reply type messages 
                    if(message.get("reply_to") is not None):
                        # Handle message replies
                        if message.get("reply_to").get("mid") is not None:
                            logger.info(f"Message is a reply to message ID: {message.get('reply_to').get('mid')}")
                            context_type = "message_reply"
                            context = {
                                "mid": message.get("reply_to").get("mid")
                            }
                            
                        # Handle story replies
                        if message.get("reply_to").get("story") is not None:
                            story_id = message.get("reply_to").get("story").get("id")
                            story_url = message.get("reply_to").get("story").get("url")
                            story_owner = await get_ig_media_owner(story_id, page_access_token)
                            
                            logger.info(f"Message is a reply to story ID: {story_id}")

                            attachment_type = "story_reply"
                            attachment_payload = {
                                "story_media_id": story_id,
                                "story_media_url": story_url,
                                "owner_username": story_owner.get("username"),
                                "owner_profile_picture_url" : story_owner.get("profile_picture_url")
                            }

                    message_text = message.get("text") or {
                        "share": "Shared a post",
                        "ig_story": "Shared a story",
                        "ig_reel": "Shared a reel",
                        "story_mention": "Mentioned you in their story",
                        "image": "Shared an image",
                        "video": "Shared a video",
                    }.get(attachment_type, f"Shared a {attachment_type}")

                    # Get user-specific collection names
                    messages_collection_name = f"messages_{org_id}"
                    conversations_collection_name = f"conversations_{org_id}"
                    
                    # Get or create collections
                    messages_collection = db[messages_collection_name]
                    conversations_collection = db[conversations_collection_name]

                    # Get user details
                    user_details = await get_ig_username(sender_id, page_access_token)
                    username = user_details.get('username')
                    name = user_details.get('name', username)
                    user = ""
                    if name is not None:
                        user = name
                    if username is not None:
                        user = user + " - @" + username

                    logger.info(f"Message Received from {user}, Message: {message_text}")

                    


                    logger.success("Calling function create_or_update_instagram_conversation")

                    # Create or update conversation
                    conversation_id = await create_or_update_instagram_conversation(
                        org_id=org_id,
                        recipient_id=recipient_id,
                        sender_id=sender_id,
                        customer_name=name,
                        username=username,
                        last_message=message_echo and f"You: {message_text}" or message_text,
                        last_sender= message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                    )
                    
                    # Store in Instagram-specific collections
                    message_id = await store_instagram_message(
                        org_id=org_id,
                        content=message_text,
                        type=attachment_type,
                        payload=attachment_payload,
                        conversation_id=conversation_id,
                        sender_id=sender_id,
                        sender_name=name,
                        sender_username=username,
                        recipient_id=recipient_id,
                        role= message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                        message_id=ig_message_id,  # Use the ID from the webhook for deduplication
                        context_type=context_type,
                        context=context,
                    )
                    
                    if not message_echo:
                        if background_tasks:
                            background_tasks.add_task(
                                check_and_trigger_automations,
                                org_id=org_id,
                                platform="instagram",
                                event_type="message_received",
                                trigger_data={
                                    "platform": "instagram",    
                                    "platform_id": recipient_id,
                                    "conversation_id": conversation_id,
                                    "customer_id": sender_id,
                                    "customer_name": name,
                                    "customer_username": username,
                                    "message_text": message_text,
                                    "message_type": attachment_type,
                                    "message_id": ig_message_id
                                }
                            )
                        else:
                            logger.warning("No background_tasks provided, skipping automation trigger for message_received.")

                    # Check if conversation exists
                    existing_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
                    conversation = conversations_collection.find_one({"conversation_id": conversation_id})

                    if recipient_id in services.instagram_connections_map:
                        for connection_key in services.instagram_connections_map[recipient_id]:
                            # Notify each connected client for this Instagram ID
                            try:
                                if connection_key in manager.active_connections:
                                    await manager.active_connections[connection_key].send_json({
                                        "type": "new_instagram_message",
                                        "message": {
                                            "conversation_id": conversation_id,
                                            "sender_id": username,
                                            "content": message_text,
                                            "type": attachment_type,
                                            "payload": attachment_payload,
                                            "timestamp": datetime.now().isoformat(),
                                            "role": message_echo and MessageRole.AGENT or MessageRole.CUSTOMER,
                                        }
                                    })
                            except Exception as e:
                                logger.error(f"Error notifying connection {connection_key}: {e}")
                    
                    if conversation:
                    # Convert ObjectId to string for JSON serialization
                        conversation["_id"] = str(conversation["_id"])
                        # Convert datetime to ISO string format
                        if "timestamp" in conversation:
                            conversation["timestamp"] = conversation["timestamp"].isoformat()
                        if "last_message_timestamp" in conversation:
                            conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()
                    
                    # Broadcast Instagram-specific notifications first
                    if not existing_conversation:
                        await broadcast_instagram_message(recipient_id, "new_conversation", {
                            "conversation": conversation
                        })
                    else:
                        await broadcast_instagram_message(recipient_id, "conversation_updated", {
                            "conversation": conversation
                        })

                    if not message_echo:
                        contact_data = {
                            "instagram_id": sender_id,
                            "full_name": name,
                            "username": username,
                            "country": None,
                            "profile_url": user_details.get("profile_pic"),
                            "conversation_id": f"instagram_{username}",
                            "email": None,
                            "categories": []
                        }

                        store_contact_background(background_tasks, org_id, "instagram", contact_data)
                        
                    return {"status": "ok"}
                    # Broadcast the new message to Instagram-specific clients
                    # await broadcast_instagram_message(recipient_id, "new_message", {
                    #     "message": {
                    #         "role": "customer",
                    #         "conversation_id": conversation_id,
                    #         "sender_id": username,
                    #         "content": message_text,
                    #         "timestamp": datetime.now().isoformat()
                    #     }
                    # })

    return {"status": "ok"}
        
@router.post("/cashfree")
async def cashfree_webhook(request: Request):

    await verify_cashfree_signature(request)

    raw_body = await request.body()
    payload = json.loads(raw_body)
    logger.info(f"Cashfree Webhook Received:\n{json.dumps(payload, indent=2)}")


    event_type = payload["type"]
    data = payload["data"]

    idempotency_key = await build_idempotency_key(event_type, data)

    try:
        db.cashfree_webhook_events.insert_one({
            "idempotency_key": idempotency_key,
            "gateway": "CASHFREE",
            "event_type": event_type,
            "reference_id": data.get("cf_payment_id")
                or data.get("cf_subscription_id"),
            "processed": False,
            "payload": payload,
            "created_at": datetime.utcnow()
        })
    except DuplicateKeyError:
        # webhook retry or duplicate
        return {"status": "duplicate_ignored"}

    try:
        logger.info(f"Processing Cashfree event: {event_type}")
        await process_cashfree_event(event_type, payload)

        db.cashfree_webhook_events.update_one(
            {"idempotency_key": idempotency_key},
            {"$set": {"processed": True}}
        )

    except Exception as e:
        # DO NOT fail the webhook
        logger.error(f"Webhook processing failed : {e}")

    return {"status": "processed"}

