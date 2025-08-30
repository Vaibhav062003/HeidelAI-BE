# Built-in Modules
import time, json, hmac, hashlib
from fastapi import APIRouter, HTTPException
from datetime import datetime
from fastapi import Request, Header
from fastapi.responses import PlainTextResponse

# Internal Modules
from core.managers import manager
from services.wa_service import *
from services.ig_service import *
from schemas.models import MessageRole
from database import get_mongo_db
from config.settings import VERIFY_TOKEN, APP_SECRET
from core.services import services

router = APIRouter(prefix="/webhook", tags=["Webhooks"])
db = get_mongo_db()

# Middleware or decorator to validate signature for POST requests
async def signature_required(request: Request, x_hub_signature_256: str = Header(None)):
    # Get the request payload
    body = await request.body()
    payload = body.decode('utf-8')

    if not x_hub_signature_256:
        raise HTTPException(status_code=403, detail="Signature missing")

    if not validate_signature(payload, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

def validate_signature(payload, signature):
    # Hash the payload with the app secret
    expected_signature = hmac.new(
        bytes(APP_SECRET, 'latin-1'),
        msg=payload.encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()
    
    # Compare signatures to ensure they match
    return hmac.compare_digest(f"sha256={expected_signature}", signature)

# def validate_signature(payload, signature):
#     expected_signature = hmac.new(
#         bytes(APP_SECRET, 'latin-1'),
#         msg=payload.encode('utf-8'),
#         digestmod=hashlib.sha256
#     ).hexdigest()
    
#     computed_signature = f"sha256={expected_signature}"
#     print(f"Computed Signature: {computed_signature}")
#     print(f"Received Signature: {signature}")
    
#     return hmac.compare_digest(computed_signature, signature)

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
    if args.get("") == VERIFY_TOKEN:
        return PlainTextResponse(content=str(args.get("hub.challenge")))
    return {"status": "Verification failed"}

@router.post("")
async def handle_webhook(request: Request, x_hub_signature_256: str = Header(None)):
    """
    This weebhook accepts POST requests from 
    """
    global flag
    global search_term
    global websocket_product_data
    global websocket_product_data_ig

    # First, validate the signature
    body = await request.body()
    payload_str = body.decode('utf-8')
    
    # Validate signature
    if not x_hub_signature_256:
        print(f"NO Signature: {payload_str}")
        raise HTTPException(status_code=403, detail="Signature missing")
    
    if not validate_signature(payload_str, x_hub_signature_256):
        print(f"Validate Signature: {payload_str}")
        raise HTTPException(status_code=403, detail="Invalid signature")
    
    print("RECEIVED CORRECT SIGNATURE:", payload_str)

    # Now parse the payload
    payload = json.loads(payload_str)

    # Remove expired message IDs
    remove_expired_message_ids()

    print("Incoming webhook payload:", json.dumps(payload, indent=2))

    if payload["object"] == "whatsapp_business_account":
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                whatsapp_business_id = entry.get("id")
                changes = entry.get("changes", [])
                for change in changes:
                    value = change.get("value", {})
                    
                    
                    # Validate whatsapp_business_id exists
                    if not whatsapp_business_id:
                        print("Missing WhatsApp business ID in webhook payload")
                        continue
                    
                    # Find the connection to get user details
                    connection = db.whatsapp_connections.find_one(
                        {"business_account_id": whatsapp_business_id}
                    )
                    
                    if not connection:
                        print(f"No connection found for WhatsApp business ID: {whatsapp_business_id}")
                        continue
                    
                    # org_id = connection.get("org_id")

                    # Get sender name from contacts if available
                    sender_name2 = "Unknown Sender"
                    contacts = value.get("contacts", [])
                    if contacts:
                        for contact in contacts:
                            sender_name2 = contact.get("profile", {}).get("name", "Unknown Sender")
                            print(f"Sender Name: {sender_name2}")

                    # Process messages in the webhook
                    messages = value.get("messages", [])
                    if messages:
                        for message in messages:
                            # Skip processing if not a message
                            if "id" not in message:
                                continue
                                
                            # Check if message ID is already processed
                            message_id = message["id"]
                            if message_id in services.processed_message_ids:
                                print("Duplicate WhatsApp message, skipping processing.")
                                continue
                                
                            # Add message ID with the current timestamp
                            services.processed_message_ids[message_id] = time.time()
                            
                            # Extract essential data
                            sender_id = message.get("from")
                            message_type = message.get("type")
                            
                            # Create conversation ID
                            conversation_id = f"whatsapp_{sender_id}"
                            
                            # Extract message content based on message type
                            if message_type == "text":
                                content = message.get("text", {}).get("body", "")
                            elif message_type == "image":
                                content = "[Image]"
                            elif message_type == "video":
                                content = "[Video]"
                            elif message_type == "audio":
                                content = "[Audio]"
                            elif message_type == "document":
                                content = "[Document]"
                            elif message_type == "location":
                                content = "[Location]"
                            elif message_type == "button":
                                content = f"[Button: {message.get('button', {}).get('payload', 'Unknown')}]"
                            elif message_type == "order":
                                content = "[Order]"
                            elif message_type == "interactive":
                                content = "[Interactive Message]"
                            else:
                                content = f"[Unsupported message type: {message_type}]"
                                
                            # Get user-specific collections
                            messages_collection_name = f"messages_{org_id}"
                            conversations_collection_name = f"conversations_{org_id}"
                            
                            # Get or create collections
                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]

                            print(f"WhatsApp Message Received from {sender_name2}, Message: {content}")
                            
                            # Create or update conversation
                            conversation_id = await create_or_update_whatsapp_conversation(
                                recipient_id=whatsapp_business_id,
                                sender_id=sender_id,
                                customer_name=sender_name2,
                                last_message=content
                            )
                            
                            # Store in WhatsApp-specific collections
                            message_id = await store_whatsapp_message(
                                content=content,
                                conversation_id=conversation_id,
                                sender_id=sender_id,
                                sender_name=sender_name2,
                                recipient_id=whatsapp_business_id,
                                role=MessageRole.CUSTOMER,
                                message_id=message_id,  # Use the ID from the webhook for deduplication
                                message_type=message_type,
                                raw_data=message
                            )
                            
    
    if payload["object"] == "instagram": 
        if "entry" in payload and len(payload["entry"]) > 0:
            for entry in payload["entry"]:
                ig_account_id = entry.get("id")

                
                # Prithal's code testing
                for changes in entry.get('changes', []):
                    field = changes.get('field')
                    value = changes.get('value')

                    if field == 'comments':
                        await process_comments_events(value, ig_account_id)
                    return {"status": "success"}
                
                for message in entry.get("messaging", []):
                    # Check if this is a deletion event
                    if "message" in message and message["message"].get("is_deleted", False):
                        # Get the message ID that was deleted
                        deleted_message_id = message["message"]["mid"]
                        recipient_id = message["recipient"]["id"]  # Instagram business account ID
                        sender_id = message["sender"]["id"]        # Instagram user ID who deleted the message
                        
                        print(f"Message deletion event detected - mid: {deleted_message_id}")

                        # Find the corresponding Instagram account in our connections
                        instagram_connection = db.instagram_connections.find_one(
                            {"instagram_id": recipient_id, "is_active": True},
                            sort=[("last_updated", -1)]
                        )

                        org_id = instagram_connection.get("org_id")
                        print(f"Org_id: {org_id}")

                        # Get the Instagram-specific collection names
                        messages_collection_name = f"messages_{org_id}"
                        conversations_collection_name = f"conversations_{org_id}"  # Add this line

                        # Check if collection exists
                        if messages_collection_name in db.list_collection_names():

                            messages_collection = db[messages_collection_name]
                            conversations_collection = db[conversations_collection_name]  # Add this line
                            
                            # Find the message to be deleted
                            message_to_delete = messages_collection.find_one({"message_id": deleted_message_id})
                            
                            if message_to_delete:
                                conversation_id = message_to_delete.get("conversation_id")
                                
                                # Get the conversation
                                conversation = conversations_collection.find_one({"id": conversation_id})
                                
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
                                            {"id": conversation_id},
                                            {"$set": {
                                                "last_message": newest_message.get("content", ""),
                                                "last_message_timestamp": newest_message.get("timestamp")
                                            }}
                                        )
                                    else:
                                        # If no messages left, set default
                                        conversations_collection.update_one(
                                            {"id": conversation_id},
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
                                    
                                    # Broadcast both the deletion event and updated conversation
                                    if recipient_id in services.instagram_connections_map and conversation_id:
                                        await broadcast_instagram_message(recipient_id, "message_deleted", {
                                            "message_id": deleted_message_id,
                                            "conversation_id": conversation_id
                                        })
                                        
                                        await broadcast_instagram_message(recipient_id, "conversation_updated", {
                                            "conversation": conversation
                                        })
                        
                        # Return success early since we've handled the deletion event
                        return {"status": "success"}
                

                    # Check if the message is a read event
                    if "read" in message:
                        print("Message read event")
                        continue

                    ig_message_id = message["message"]["mid"]

                    if ig_message_id in services.processed_message_ids:
                        print("Duplicate message, skipping processing.")
                        continue
                    
                    # Add message ID with the current timestamp
                    services.processed_message_ids[ig_message_id] = time.time()

                    sender_id = message["sender"]["id"]
                    recipient_id = message["recipient"]["id"]
                    message_text = message["message"]["text"]

                    # Find the corresponding Instagram account in our connections
                    instagram_connection = db.instagram_connections.find_one(
                        {"instagram_id": recipient_id, "is_active": True},
                        sort=[("last_updated", -1)]
                    )

                    if not instagram_connection:
                        print(f"No active Instagram connection found for recipient ID: {recipient_id}")
                        return {"status": "error", "message": "Instagram account not found"}
                    
                    org_collection = db.organizations.find_one({"ig_id": recipient_id})

                    if not org_collection:
                        print(f"No active organization found for Instagram ID: {recipient_id}")
                        return {"status": "error", "message": "Organization not found"}

                    org_id = org_collection.get("org_id")

                    # get the page access token
                    page_access_token = instagram_connection.get("page_access_token")
                    if not page_access_token:
                        print(f"Page access token not found for recipient ID: {recipient_id}")
                        return {"status": "error", "message": "Page access token not found"}

                    # Get user-specific collection names
                    messages_collection_name = f"messages_{org_id}"
                    conversations_collection_name = f"conversations_{org_id}"
                    
                    # Get or create collections
                    messages_collection = db[messages_collection_name]
                    conversations_collection = db[conversations_collection_name]

                    # Get user details
                    user_details = await get_ig_username(sender_id, page_access_token)
                    username = user_details.get('username')
                    name = user_details.get('name')
                    user = name + " - @" + username

                    print(f"Message Received from {user}, Message: {message_text}")

                    print("Calling function create_or_update_instagram_conversation")

                    # Create or update conversation
                    conversation_id = await create_or_update_instagram_conversation(
                        org_id=org_id,
                        recipient_id=recipient_id,
                        sender_id=sender_id,
                        customer_name=name,
                        username=username,
                        last_message=message_text
                    )
                    
                    # Store in Instagram-specific collections
                    message_id = await store_instagram_message(
                        org_id=org_id,
                        content=message_text,
                        conversation_id=conversation_id,
                        sender_id=sender_id,
                        sender_name=name,
                        sender_username=username,
                        recipient_id=recipient_id,
                        role=MessageRole.CUSTOMER,
                        message_id=ig_message_id  # Use the ID from the webhook for deduplication
                    )
                    
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
                                            "timestamp": datetime.now().isoformat()
                                        }
                                    })
                            except Exception as e:
                                print(f"Error notifying connection {connection_key}: {e}")
                    
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
                    
                    # Broadcast the new message to Instagram-specific clients
                    await broadcast_instagram_message(recipient_id, "new_message", {
                        "message": {
                            "conversation_id": conversation_id,
                            "sender_id": username,
                            "content": message_text,
                            "timestamp": datetime.now().isoformat()
                        }
                    })

    return {"status": "ok"}
