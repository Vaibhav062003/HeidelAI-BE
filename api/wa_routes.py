# Built-in modules
import uuid
import httpx
import requests
from typing import Optional
from datetime import datetime

# Third-party modules
import cloudinary
from fastapi import (
    APIRouter, 
    HTTPException,
    Form,
    File,
    UploadFile,
    Depends
)

# Internal modules
from core.services import services
from services.utils import serialize_mongo
from schemas.models import WhatsappAuthRequest, MessageRole, PlatformDisconnectRequest
from services.wa_service import broadcast_whatsapp_message, store_whatsapp_message, send_whatsapp_message, get_templates
from services.platforms.whatsapp_service import WhatsAppService
from services.cloudinary_service import check_cloudinary_storage_limit
from database import get_mongo_db
from auth.dependencies import CurrentUser
from auth.auth_utils import admin_required, require_auth
from config.settings import (
    APP_ID,
    APP_SECRET,
    ACCESS_TOKEN,
    BUSINESS_ID,
    VERSION,
    CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_API_KEY,
    CLOUDINARY_API_SECRET
)
from core.logger import get_logger



logger = get_logger(__name__)

cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

whatsapp_service = WhatsAppService()

db = get_mongo_db()

router = APIRouter(prefix="/api/whatsapp", tags=["WhatsApp"])

@router.post("/auth")
async def whatsapp_auth_exchange(request: WhatsappAuthRequest, user: CurrentUser):
    try:
        # Extract authorization code from request
        code = request.code
        # redirect_uri = request.redirect_uri
        org_id = user.org_id
        whatsapp_business_id = request.waba_id
        
        if not code:
            raise HTTPException(status_code=400, detail="Authorization code is required")

        # Exchange code for access token with WhatsApp/Facebook
        token_url = "https://graph.facebook.com/v22.0/oauth/access_token"
        token_data = {
            "client_id": APP_ID,
            "client_secret": APP_SECRET, 
            # "redirect_uri": redirect_uri, Not needed now as using FB SDK
            "grant_type": "authorization_code",
            "code": code
        }
        
        # Make request to Facebook Graph API
        token_response = requests.post(token_url, json=token_data)
        
        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail=f"Failed to exchange authorization code: {token_response.text}"
            )
        
        token_info = token_response.json()
        logger.info(f"WhatsApp exchange auth code for token response: {token_info}")
        access_token = token_info.get("access_token")
        
        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to get valid access token")
        
        # Get WhatsApp Business Account details
        # request_data = {
        # "fields": "id,name,currency,owner_business_info",
        # "limit": 20,
        # "access_token": ACCESS_TOKEN #our system access token, not the user access token
        # }
        # url = f"https://graph.facebook.com/v22.0/{BUSINESS_ID}/client_whatsapp_business_accounts"
        # account_response = requests.get(url, params=request_data)
        # print(f"Account response : {account_response.json()}")

        # if account_response.status_code != 200:
        #     print(f"Get whatsapp account details err: {account_response.text}")
        #     raise HTTPException(
        #         status_code=400, 
        #         detail="Failed to get WhatsApp Business Account information"
        #     )
        
        # account_data = account_response.json().get("data", [{}])[0]
        # whatsapp_business_id = account_data.get("id")
        
        # Get user phone number details
        params = {
            "fields": "id,cc,country_dial_code,display_phone_number,verified_name,status,quality_rating,search_visibility,platform_type,code_verification_status",
            "access_token": access_token # User's access token
        }
        phone_url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}/phone_numbers"
        phone_response = requests.get(phone_url, params=params)
        logger.info(f"Phone response: {phone_response.json()}")
        
        if phone_response.status_code != 200:
            logger.error(f"get phone number details err: {phone_response.text}")
            raise HTTPException(
                status_code=400, 
                detail="Failed to get phone number information"
            )
        
        phones_data = phone_response.json().get("data", [{}])[0]
        phone = phones_data.get("display_phone_number")
        phone_verified = phones_data.get("code_verification_status")
        phone_id = phones_data.get("id")
        

        # subscribe user to our webhook
        webhook_params = {
            "access_token": access_token
        }
        webhook_url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}/subscribed_apps"
        webhook_response = requests.post(webhook_url, json=webhook_params)

        if webhook_response.status_code != 200:
            logger.error(f"Subscribe to webhook err: {webhook_response.text}")
            raise HTTPException(
                status_code=400, 
                detail="Failed to subscribe WhatsApp account to webhook"
            )
        
        # Check if phone number is verified
        if phone_verified != "VERIFIED":
            logger.warning(f"Phone number {phone} is currently {phone_verified}. Attempting registration to fix this...")
        
        # Register phone on WhatsApp Cloud API
        phone_register_params = {
            "messaging_product": "whatsapp",
            "pin": "000000",
            "access_token": access_token
        }
        phone_register_url = f"https://graph.facebook.com/v22.0/{phone_id}/register"
        phone_register_response = requests.post(phone_register_url, json=phone_register_params)
        logger.info(f"Phone register response: {phone_register_response.json()}")

        if phone_register_response.status_code != 200:
            logger.error(f"Register phone number error: {phone_register_response.text}")
            raise HTTPException(
                status_code=400, 
                detail="Failed to register phone number. Please ensure the phone number is valid/verified and try again."
            )
        
        # Generate a unique user_id for this WhatsApp connection
        user_id = str(uuid.uuid4())

        # Create collections for messages and conversations
        messages_collection = db[f"messages_{org_id}"]
        conversations_collection = db[f"conversations_{org_id}"]

        # Ensure indexes for efficient querying
        messages_collection.create_index("message_id", unique=True)
        conversations_collection.create_index("conversation_id", unique=True)

        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"wa_id": str(whatsapp_business_id)}}
        )
        
        # Store connection in database
        whatsapp_connection = {
            "user_id": user_id,
            "org_id": org_id,
            "wa_id": whatsapp_business_id,
            "phone": phone,
            "phone_id": phone_id,
            "name": phones_data.get("verified_name", "WhatsApp Business"),
            "access_token": access_token,
            "connected_at": datetime.now().isoformat(),
            "business_info": {
                "status": "Active"
            }
        }
        
        # Store in your existing MongoDB database
        db.whatsapp_connections.update_one(
            {"wa_id": whatsapp_business_id, "user_id": user_id},
            {"$set": whatsapp_connection},
            upsert=True
        )
        
        return {
            "status": "success",
            "org_id": org_id,
            "user_id": user_id,
            "name": phones_data.get("verified_name", "WhatsApp Business"),
            "phone": phone,
            "business_info": {
                "account_type": "WhatsApp Business",
                "status": "Active"
            }
        }
        
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"WhatsApp authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"WhatsApp authentication error: {str(e)}")
    
@router.post("/disconnect")
async def whatsapp_disconnect(request: PlatformDisconnectRequest, user: CurrentUser):
    """Disconnect a WhatsApp Account from the system"""
    try:
        org_id = user.org_id
        connection_id = request.user_id

        removal_result = await whatsapp_service.disconnect(org_id, connection_id)

        return {
            "message": (
                f"WhatsApp disconnected, archived data, "
                f"removed {removal_result['deleted_connections']} connections, "
                f"and cleared org WhatsApp reference."
            )
        }
       
    except Exception as e:
        logger.error(f"Error disconnecting WhatsApp: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect WhatsApp: {str(e)}")

@router.get("/{whatsapp_id}/details")
async def get_whatsapp_details(whatsapp_id: str, _: None = Depends(require_auth)):
    """
    Fetch detailed WhatsApp Business Account information from Meta Graph API
    
    Args:
        whatsapp_id: The WhatsApp user ID from your application
        
    Returns:
        WhatsApp Business Account details including phone numbers, status, etc.
    """
    try:
        # Find the WhatsApp connection to get the access token and business account ID
        connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "access_token": 1, "phone_id": 1, "_id": 0}
        )
        
        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        whatsapp_business_id = connection["wa_id"]
        access_token = connection["access_token"]
        phone_id = connection.get("phone_id")
        
        # Set up date ranges for analytics
        end_date = int(datetime.now().timestamp())
        start_date = end_date - (30 * 24 * 60 * 60)  # 30 days ago
        
        # Build analytics field query
        analytics_field = f"analytics.start({start_date}).end({end_date}).granularity(DAY)"
        
        # Fetch WhatsApp Business Account details
        account_url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}"
        account_params = {
            "access_token": access_token,
            "fields": f"id,name,currency,owner_business_info,message_template_namespace,timezone_id,{analytics_field}"
        }
        
        account_response = requests.get(account_url, params=account_params)
        
        if account_response.status_code != 200:
            logger.error(f"WhatsApp API Error: {account_response.text}")
            raise HTTPException(
                status_code=account_response.status_code,
                detail=f"Failed to fetch WhatsApp account details: {account_response.json().get('error', {}).get('message')}"
            )
        
        account_data = account_response.json()
        
        # If we have a phone_id, fetch phone number details too
        phone_data = {}
        if phone_id:
            phone_url = f"https://graph.facebook.com/v22.0/{phone_id}"
            phone_params = {
                "access_token": access_token,
                "fields": "display_phone_number,verified_name,code_verification_status,quality_rating,status,certificate,platform_type"
            }
            
            phone_response = requests.get(phone_url, params=phone_params)
            
            if phone_response.status_code == 200:
                phone_data = phone_response.json()
        
        # Fetch template information
        templates_url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}/message_templates"
        templates_params = {
            "access_token": access_token,
            "limit": 10  # Limit to 10 recent templates
        }
        
        templates_response = requests.get(templates_url, params=templates_params)
        templates_data = {}
        
        if templates_response.status_code == 200:
            templates_data = templates_response.json()
        
        # Combine all the data
        result = {
            "status": "success",
            "account_info": account_data,
            "phone_details": phone_data,
            "templates_summary": {
                "count": len(templates_data.get("data", [])),
                "recent_templates": templates_data.get("data", [])
            },
            "analytics_period": {
                "start": datetime.fromtimestamp(start_date).isoformat(),
                "end": datetime.fromtimestamp(end_date).isoformat(),
                "granularity": "DAY"
            },
            "connection_details": {
                "user_id": whatsapp_id,
                "wa_id": whatsapp_business_id,
                "phone_id": phone_id
            }
        }
        
        return result
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching WhatsApp details: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch WhatsApp details: {str(e)}")

@router.get("/{whatsapp_id}/analytics/conversations")
async def get_whatsapp_conversation_analytics(
    whatsapp_id: str,
    start_date: Optional[int] = None,
    end_date: Optional[int] = None,
    granularity: Optional[str] = "DAILY",
    phone_number: Optional[str] = None, 
    _: None = Depends(require_auth)
):
    """
    Fetch conversation analytics for a WhatsApp Business Account
    
    Args:
        whatsapp_id: The WhatsApp user ID from your application
        start_date: Start timestamp for analytics period (Unix timestamp)
        end_date: End timestamp for analytics period (Unix timestamp)
        granularity: Data granularity (DAILY, MONTHLY, or HALF_HOUR)
        phone_number: Optional specific phone number to filter analytics
        
    Returns:
        Conversation analytics data for the WhatsApp Business Account
    """
    try:
        # Find the WhatsApp connection to get the access token and business account ID
        connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "access_token": 1, "phone_id": 1, "_id": 0}
        )
        
        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        whatsapp_business_id = connection["wa_id"]
        access_token = connection["access_token"]
        
        # Set default date range if not provided (last 30 days)
        if not end_date:
            end_date = int(datetime.now().timestamp())
        
        if not start_date:
            # Default to 30 days before end_date
            start_date = end_date - (30 * 24 * 60 * 60)
        
        # Validate granularity
        valid_granularities = ["DAILY", "MONTHLY", "HALF_HOUR"]
        if granularity not in valid_granularities:
            granularity = "DAILY"
            
        # Define dimensions for analysis
        dimensions = ["PHONE", "COUNTRY", "CONVERSATION_TYPE", "CONVERSATION_DIRECTION"]
        
        # Build analytics field query including dimensions
        dimensions_param = ",".join(dimensions)
        conversation_analytics_field = (
            f"conversation_analytics.start({start_date}).end({end_date})."
            f"granularity({granularity}).dimensions({dimensions_param})."
            f"metric_types(CONVERSATION,COST)"
        )
        
        # Add phone number filter if provided
        if phone_number:
            conversation_analytics_field += f".phone_numbers({phone_number})"
            
        # Fetch WhatsApp Business Account analytics
        url = f"https://graph.facebook.com/v22.0/{whatsapp_business_id}"
        params = {
            "access_token": access_token,
            "fields": conversation_analytics_field
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"WhatsApp API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch WhatsApp conversation analytics: {response.json().get('error', {}).get('message')}"
            )
        
        analytics_data = response.json()
        
        # Process analytics data - calculate totals and format results
        conversation_data = analytics_data.get('conversation_analytics', {}).get('data', [])
        
        # Calculate summary statistics
        total_conversations = 0
        total_cost = 0
        business_initiated = 0
        user_initiated = 0
        country_stats = {}
        
        for data_group in conversation_data:
            for data_point in data_group.get('data_points', []):
                conv_count = data_point.get('conversation', 0)
                cost = data_point.get('cost', 0)
                direction = data_point.get('conversation_direction')
                country = data_point.get('country')
                
                total_conversations += conv_count
                total_cost += cost
                
                if direction == 'BUSINESS_INITIATED':
                    business_initiated += conv_count
                elif direction == 'USER_INITIATED':
                    user_initiated += conv_count
                
                if country:
                    if country not in country_stats:
                        country_stats[country] = {
                            'conversations': 0,
                            'cost': 0
                        }
                    country_stats[country]['conversations'] += conv_count
                    country_stats[country]['cost'] += cost
        
        # Extract and format the analytics data
        result = {
            "status": "success",
            "wa_id": whatsapp_business_id,
            "analytics_period": {
                "start": datetime.fromtimestamp(start_date).isoformat(),
                "end": datetime.fromtimestamp(end_date).isoformat(),
                "granularity": granularity
            },
            "summary": {
                "total_conversations": total_conversations,
                "total_cost": total_cost,
                "business_initiated": business_initiated,
                "user_initiated": user_initiated
            },
            "country_distribution": country_stats,
            "raw_data": analytics_data.get("conversation_analytics", {})
        }
        
        return result
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching WhatsApp conversation analytics: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp conversation analytics: {str(e)}"
        )

@router.get("/conversations")
async def get_whatsapp_conversations(user: CurrentUser):
    """
    Fetch all conversations for a specific WhatsApp business account ID
    Returns them in the standard conversation format for compatibility with the frontend
    """
    try:
        org_id = user.org_id 

        # Get the WhatsApp-specific collection name
        conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if conversations_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        conversations_collection = db[conversations_collection_name]
        
        # Fetch conversations and sort by last_message_timestamp
        raw_conversations = list(conversations_collection.find(
            {}, 
            {"_id": 0}  # Exclude _id field
        ).sort("last_message_timestamp", -1))
        
        # Format conversations to match the required structure
        formatted_conversations = []
        for conv in raw_conversations:
            # Format timestamps
            timestamp = conv.get("last_message_timestamp") or conv.get("timestamp")
            if timestamp and isinstance(timestamp, datetime):
                timestamp = timestamp.isoformat()
            
            # Format the conversation object to match expected structure
            formatted_conversation = {
                'id': conv.get('conversation_id', conv.get('id')),
                'platform': 'whatsapp',
                'customer_id': conv.get('customer_id'),
                'customer_name': f"{conv.get('customer_name', '')}",
                'last_message': conv.get('last_message', ''),
                'timestamp': timestamp,
                'unread_count': conv.get('unread_count', 0),
                'is_ai_enabled': conv.get('is_ai_enabled', True),
                'assigned_agent_id': conv.get('assigned_agent_id'),
                'priority': conv.get('priority', 'medium'),  # Default to medium
                'sentiment': conv.get('sentiment', 'neutral')  # Default to neutral
            }
            
            formatted_conversations.append(formatted_conversation)
        
        return formatted_conversations
    
    except Exception as e:
        logger.error(f"Error fetching WhatsApp conversations for org {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp conversations: {str(e)}"
        )

@router.get("/conversations/{conversation_id}")
async def get_whatsapp_conversation(conversation_id: str, user: CurrentUser):
    """
    Fetch a specific conversation for an Whatsapp business account ID
    Returns the conversation in the standard format for compatibility with the frontend
    """
    try:
        org_id = user.org_id 

        conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if conversations_collection_name not in db.list_collection_names():
            return {"message": "No conversation found"}
        
        # Get the collection
        conversations_collection = db[conversations_collection_name]
        
        # Find the specific conversation
        conversation = conversations_collection.find_one(
            {"conversation_id": conversation_id},
            {"_id": 0}  # Exclude _id field
        )
        
        if not conversation:
            return {"message": "No conversation found"}
        
        # Format timestamps for JSON serialization
        if "last_message_timestamp" in conversation and isinstance(conversation["last_message_timestamp"], datetime):
            conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()
        if "timestamp" in conversation and isinstance(conversation["timestamp"], datetime):
            conversation["timestamp"] = conversation["timestamp"].isoformat()
        
        return conversation
    
    except Exception as e:
        logger.error(f"Error fetching Instagram conversation {conversation_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch Instagram conversation: {str(e)}"
        )

@router.get("/conversations/{conversation_id}/messages")
async def get_whatsapp_messages(conversation_id: str, user: CurrentUser):
    """Fetch messages for a specific WhatsApp conversation"""
    try:
        org_id = user.org_id 

        # Get the WhatsApp-specific collection name
        messages_collection_name = f"messages_{org_id}"
        
        # Check if collection exists
        if messages_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        messages_collection = db[messages_collection_name]
        
        # Fetch messages
        messages = list(messages_collection.find(
            {"conversation_id": conversation_id},
            {"_id": 0, "raw_data": 0}  # Exclude _id field and raw_data to reduce payload size
        ).sort("timestamp", 1))  # Sort by timestamp ascending
        
        # Format timestamps for JSON serialization
        for msg in messages:
            if "timestamp" in msg and isinstance(msg["timestamp"], datetime):
                msg["timestamp"] = msg["timestamp"].isoformat()
        
        return messages
    
    except Exception as e:
        logger.error(f"Error fetching WhatsApp messages for {conversation_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch WhatsApp messages: {str(e)}"
        )

# @router.get("/conversations/{conversation_id}/read")
# async def update_whatsapp_unread_count(conversation_id: str, user: CurrentUser):
#     """Reset unread count for a specific WhatsApp conversation"""

#     try:
#         org_id = user.org_id 

#         # Get the WhatsApp-specific collection
#         conversations_collection = db[f"conversations_{org_id}"]
        
#         # Update the conversation
#         result = conversations_collection.update_one(
#             {"conversation_id": conversation_id},
#             {"$set": {"unread_count": 0}}
#         )
        
#         if result.matched_count == 0:
#             return {"status": "error", "message": "No conversation found"}
        
#         return {"status": "success", "message": "Unread count reset to 0"}
    
#     except Exception as e:
#         logger.error(f"Error resetting WhatsApp unread count: {e}")
#         raise HTTPException(
#             status_code=500, 
#             detail=f"Failed to reset unread count: {str(e)}"
#         )

@router.get("/conversations/{conversation_id}/ai")
async def get_whatsapp_ai_status(conversation_id: str, user: CurrentUser):
    """Get AI status for a specific WhatsApp conversation"""
    try:

        org_id = user.org_id 
        # Get the WhatsApp-specific collection
        conversations_collection = db[f"conversations_{org_id}"]
        
        # Find the conversation
        conversation = conversations_collection.find_one({"conversation_id": conversation_id})
        
        if conversation:
            is_ai_enabled = conversation.get("is_ai_enabled", True)  # Default to True if not set
            return is_ai_enabled
        
        return {"status": "Error", "message": "No conversation found"}
    
    except Exception as e:
        logger.error(f"Error checking WhatsApp AI status: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to check AI status: {str(e)}"
        )

@router.post("/{whatsapp_id}/conversations/{conversation_id}/toggle_ai")
async def toggle_whatsapp_ai_status(whatsapp_id: str, conversation_id: str, body: dict, user: CurrentUser):
    """Toggle AI status for a specific WhatsApp conversation"""
    try:
        org_id = user.org_id 
        # Get the enabled status from request body
        ai_enabled = body.get("enabled")
        
        if ai_enabled is None:
            return {"status": "error", "message": "Incorrect request body"}
        
        # Get the WhatsApp-specific collection
        conversations_collection = db[f"conversations_{org_id}"]
        
        # Update the conversation
        result = conversations_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"is_ai_enabled": ai_enabled}}
        )
        
        if result.matched_count == 0:
            return {"status": "error", "message": "Conversation not found"}
        
        # Get updated conversation for broadcasting
        updated_conversation = conversations_collection.find_one({"conversation_id": conversation_id})
        
        if updated_conversation:
            # Format for broadcasting
            broadcast_conversation = dict(updated_conversation)
            if "last_message_timestamp" in broadcast_conversation:
                broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
            if "_id" in broadcast_conversation:
                broadcast_conversation["_id"] = str(broadcast_conversation["_id"])
            
            # Broadcast to WhatsApp-specific connections
            if whatsapp_id in services.whatsapp_connections_map:
                await broadcast_whatsapp_message(whatsapp_id, "conversation_updated", {
                    "conversation": broadcast_conversation
                })
        
        return {"status": "success", "message": f"AI status updated to {ai_enabled}"}
    
    except Exception as e:
        logger.error(f"Error updating WhatsApp AI status: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to update AI status: {str(e)}"
        )

@router.post("/conversations/send_message")
async def process_whatsapp_message(message: dict, user: CurrentUser):
    """Send a message to a WhatsApp conversation"""
    # Get Instagram-specific collections
    org_id = user.org_id
    org_conversations_collection = db[f"conversations_{org_id}"]
    
    # Check if conversation exists
    conversation = org_conversations_collection.find_one({"conversation_id": message["conversation_id"]})
    
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    res = await send_whatsapp_message(message, org_id)
    return res
    
@router.post("/{whatsapp_id}/media/send")
async def upload_and_send_whatsapp_media(
    whatsapp_id: str, 
    recipient_id: str = Form(...),
    caption: Optional[str] = Form(None), 
    file: UploadFile = File(...), 
    _: None = Depends(require_auth)
):
    """
    Upload media to WhatsApp servers and send it to a recipient
    
    Args:
        whatsapp_id: The WhatsApp user ID from your application
        recipient_id: The phone number of the recipient (with country code)
        caption: Optional caption for the media
        file: The file to upload
    
    Returns:
        Status and message ID details along with the uploaded media URL
    """
    try:
        # Check storage limit before uploading
        storage_ok = await check_cloudinary_storage_limit()
        if not storage_ok:
            raise HTTPException(
                status_code=507,
                detail="Storage limit reached. Please contact support."
            )
        
        # Find the WhatsApp connection to get the access token and business account ID
        connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "access_token": 1, "phone_id": 1, "_id": 0}
        )
        
        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        wa_id = connection["wa_id"]
        access_token = connection["access_token"]
        phone_id = connection.get("phone_id")
        
        if not phone_id:
            raise HTTPException(status_code=400, detail="Phone ID not found in connection details")
        
        # First upload the file to Cloudinary to get a public URL that frontend can use
        cloudinary_result = cloudinary.uploader.upload(
            file.file,
            folder="whatsapp_media",
            resource_type="auto"
        )
        
        # Get the secure URL for the frontend to display the image
        media_url = cloudinary_result.get("secure_url")
        
        # Rewind the file to use it again for WhatsApp upload
        await file.seek(0)
        
        # Step 1: Upload media to WhatsApp servers
        upload_url = f"https://graph.facebook.com/v22.0/{phone_id}/media"
        
        # Read file content
        content = await file.read()
        content_type = file.content_type
        
        # Determine media type from content type
        if content_type.startswith('image/'):
            media_type = 'image'
        elif content_type.startswith('video/'):
            media_type = 'video'
        elif content_type.startswith('audio/'):
            media_type = 'audio'
        elif content_type == 'application/pdf':
            media_type = 'document'
        else:
            media_type = 'document'
        
        # Prepare upload request
        upload_files = {
            'file': (file.filename, content, content_type),
            'messaging_product': (None, 'whatsapp'),
            'type': (None, media_type)
        }
        
        upload_headers = {
            "Authorization": f"Bearer {access_token}"
        }

        # Make the upload request
        async with httpx.AsyncClient() as client:
            upload_response = await client.post(
                upload_url,
                headers=upload_headers,
                files=upload_files
            )
            
            if upload_response.status_code != 200:
                logger.error(f"WhatsApp Media Upload Error: {upload_response.text}")
                raise HTTPException(
                    status_code=upload_response.status_code,
                    detail=f"Failed to upload media to WhatsApp: {upload_response.text}"
                )
            
            upload_result = upload_response.json()
            media_id = upload_result.get("id")
            
            if not media_id:
                raise HTTPException(
                    status_code=400,
                    detail="No media ID returned from WhatsApp API"
                )
        
        # Step 2: Send media message using the obtained media ID
        send_url = f"https://graph.facebook.com/v22.0/{phone_id}/messages"
        send_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        send_data = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": media_type,
            media_type: {
                "id": media_id,
            }
        }
        
        # Add caption if provided for media types that support it
        if caption and media_type in ['image', 'video', 'document']:
            send_data[media_type]["caption"] = caption
        
        # Send the media message
        async with httpx.AsyncClient() as client:
            send_response = await client.post(
                send_url,
                headers=send_headers,
                json=send_data
            )
            
            if send_response.status_code != 200:
                logger.error(f"WhatsApp Media Send Error: {send_response.text}")
                raise HTTPException(
                    status_code=send_response.status_code,
                    detail=f"Failed to send WhatsApp media message: {send_response.text}"
                )
            
            send_result = send_response.json()
            message_id = send_result.get("messages", [{}])[0].get("id")
        
        # Create conversation ID for this recipient
        conversation_id = f"whatsapp_{recipient_id}"
        
        # Store message in WhatsApp-specific collection
        await store_whatsapp_message(
            content=caption or f"[{media_type.capitalize()}]",
            conversation_id=conversation_id,
            sender_id="agent",
            sender_name="Agent",
            recipient_id=wa_id,
            role=MessageRole.AGENT,
            message_id=message_id,
            message_type=media_type,
            raw_data={
                "media_id": media_id, 
                "filename": file.filename, 
                "mime_type": content_type,
                "media_url": media_url  # Store the Cloudinary URL in the message
            }
        )
        
        # Return success response with details including the media URL
        return {
            "status": "success",
            "message": "Media uploaded and sent successfully",
            "details": {
                "message_id": message_id,
                "media_id": media_id,
                "media_type": media_type,
                "recipient_id": recipient_id,
                "file_name": file.filename,
                "media_url": media_url  # Return the media URL to frontend
            }
        }
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error uploading and sending WhatsApp media: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload and send media: {str(e)}"
        )

@router.get("/profile")
async def get_whatsapp_profile(user: CurrentUser):
    """
    Fetch detailed WhatsApp profile information
    
    Returns:
        WhatsApp profile information.
    """
    try:
        org_id = user.org_id 

        org_document = db.organizations.find_one({"org_id": org_id})

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")
    
        if not wa_id:
            raise HTTPException(status_code=400, detail="WhatsApp ID not configured for this organization")

        # Get the page access token for this WhatsApp account
        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0}
        )
        logger.success(f"Fetched WhatsApp connection: {connection} for wa_id: {wa_id}")

        if not connection or not connection.get("access_token"):
            raise HTTPException(status_code=404, detail="WhatsApp connection not found or inactive")
        
        connection = serialize_mongo(connection)

        return {
            "user_id": connection.get("user_id"),
            "business_info": connection.get("business_info", {}),
            "name" : connection.get("name"),
            "phone" : connection.get("phone"),
        }
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching WhatsApp profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch WhatsApp profile: {str(e)}")


@router.get("/templates")
async def fetch_templates(user: CurrentUser):
    """
    fetch prebuilt templates:
    """
    try:
        org_id = user.org_id
        org_document = db.organizations.find_one({"org_id": org_id})

        wa_id = org_document.get("wa_id")
        logger.success(f"Fetched wa_id: {wa_id} for org_id: {org_id}")

        connection = db.whatsapp_connections.find_one(
            {"wa_id": wa_id, "org_id": org_id},
            {"_id": 0, "access_token": 1}
        )

        access_token = connection["access_token"] if connection else None

        if not connection or "access_token" not in connection:
            raise HTTPException(
                status_code=404,
                detail="WhatsApp connection not found or access token missing"
            )

        logger.success(f"Fetched WhatsApp connection for wa_id: {wa_id}")


        templates = await get_templates(org_id, wa_id, access_token)

        return {
            "status": "success",
            "message": "templates fetched successfully",
            "templates": templates
        }

    except Exception as e:
        logger.error(f"Error fetching prebuilt templates: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch prebuilt templates: {str(e)}")