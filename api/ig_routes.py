from fastapi import APIRouter, File, Form, HTTPException, Depends, Response, Query, UploadFile
from datetime import datetime
from pymongo import ReturnDocument

from config.settings import *
from services.cloudinary_service import upload_media_to_cloudinary
from services.ig_service import *
from schemas.models import PlatformDisconnectRequest, MessageRole
from auth.dependencies import CurrentUser
from auth.auth_utils import admin_required, require_auth
from services.platforms.instagram_service import InstagramService

from database import get_mongo_db
from pymongo import ReturnDocument
import cloudinary

from core.logger import get_logger

logger = get_logger(__name__)


instagram_service = InstagramService()

db = get_mongo_db()

router = APIRouter(prefix="/api/instagram", tags=["Instagram"])

def exchange_for_long_lived_token(short_lived_token):
    """
    Exchange a short-lived Instagram token for a long-lived token
    
    Args:
        short_lived_token: The short-lived token received from Instagram
        
    Returns:
        dict: Response containing the long-lived token or error details
    """
    try:
        # Build the URL with parameters
        url = "https://graph.instagram.com/access_token"
        params = {
            "grant_type": "ig_exchange_token",
            "client_secret": INSTAGRAM_APP_SECRET,
            "access_token": short_lived_token
        }
        
        # Make the GET request
        response = requests.get(url, params=params)
        
        logger.info(f"Long-lived token exchange status: {response.status_code}")
        logger.info(f"Long-lived token exchange response: {response.text}")
        
        # Check for successful response
        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "access_token": data.get("access_token"),
                "token_type": data.get("token_type"),
                "expires_in": data.get("expires_in")
            }
        else:
            return {
                "success": False,
                "error": response.text
            }
    except Exception as e:
        logger.error(f"Error exchanging token: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

@router.get("/profile")
async def get_instagram_profile(user: CurrentUser):
    """
    Fetch detailed Instagram profile information
    
    Args:
        instagram_id: The Instagram business account ID
        
    Returns:
        Instagram profile information including bio, followers count, etc.
    """
    try:
        org_id = user.org_id 

        org_document = db.organizations.find_one({"org_id": org_id})

        instagram_id = org_document.get("ig_id")

        if not instagram_id:
            raise HTTPException(status_code=400, detail="Instagram ID not configured for this organization")

        # Get the page access token for this Instagram account
        connection = db.instagram_connections.find_one(
            {"instagram_id": instagram_id, "is_active": True, "org_id": org_id},
            {"page_access_token": 1,"access_token" : 1, "page_id": 1}
        )

        # print("Details retrieved for /profile from db:", connection)
        
        if not connection or not connection.get("page_access_token"):
            raise HTTPException(status_code=404, detail="Instagram connection not found or inactive")
        
        page_access_token = connection["page_access_token"]
        
        # Get detailed profile information
        url = f"https://graph.instagram.com/me"
        params = {
            "access_token": page_access_token,
            "fields": "biography,followers_count,follows_count,id,media_count,name,profile_picture_url,username,website"
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"Instagram API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch Instagram profile: {response.json().get('error', {}).get('message')}"
            )
        
        profile_data = response.json()

        return profile_data
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching Instagram profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch Instagram profile: {str(e)}")

@router.post("/auth")
async def instagram_auth(request: dict, user: CurrentUser):
    try:
        # Extract authorization code from frontend
        code = request.get("code")
        redirect_uri = request.get("redirect_uri")
        org_id = user.org_id 
        
        if not code:
            raise HTTPException(status_code=400, detail="Authorization code is required")

        # Exchange code for access token with Instagram
        token_url = "https://api.instagram.com/oauth/access_token"
        token_data = {
            "client_id": INSTAGRAM_APP_ID,
            "client_secret": INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code
        }
        
        # Make POST request to Instagram
        token_response = requests.post(token_url, data=token_data)
        
        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail=f"Failed to exchange authorization code: {token_response.text}"
            )
        
        token_info = token_response.json()
        logger.info(f"Instagram token response: {token_info}")
        access_token = token_info.get("access_token")
        user_id = token_info.get("user_id")
        
        if not access_token or not user_id:
            raise HTTPException(status_code=400, detail="Failed to get valid access token")
        
        long_lived_token_response = exchange_for_long_lived_token(access_token)
        if long_lived_token_response and "access_token" in long_lived_token_response:
            access_token = long_lived_token_response["access_token"]
        
        # Get user profile data
        profile_url = f"https://graph.instagram.com/me?fields=user_id,biography,followers_count,follows_count,id,media_count,name,profile_picture_url,username,website&access_token={access_token}"
        profile_response = requests.get(profile_url)
        
        if profile_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail="Failed to get user profile information from Instagram"
            )
        
        profile_data = profile_response.json()
        username = profile_data.get("username")
        profile_pic_url = profile_data.get("profile_picture_url", "")
        instagram_id = profile_data.get("user_id")

        previously_connected = db.organizations.find_one(
            {"ig_id": str(instagram_id)},
            {"org_id": 1, "_id": 0}
        )
        
        if previously_connected:
            raise HTTPException(
                status_code=403, 
                detail="This Instagram account is already connected to another organization. If you think this is incorrect, please contact us at support@heidelai.com for further assistance."
            )

        # subscribe user to our webhook
        webhook_response = requests.post(
            f"https://graph.instagram.com/v22.0/{user_id}/subscribed_apps",
            params={
                "access_token": access_token,
                "subscribed_fields": "messages"
            }
        )

        if webhook_response.status_code != 200:
            raise HTTPException(
                status_code=400, 
                detail="Failed to sync Instagram account DMs."
            )

        # Create collections for messages and conversations
        messages_collection = db[f"messages_{org_id}"]
        conversations_collection = db[f"conversations_{org_id}"]

        # Ensure indexes for efficient querying
        messages_collection.create_index("message_id", unique=True)
        conversations_collection.create_index("conversation_id", unique=True)

        # Store connection in database
        new_connection = {
            "user_id" : str(user_id),
            "org_id": org_id,
            "instagram_id": str(instagram_id),
            "username": username,
            "profile_picture": profile_pic_url,
            "page_access_token": access_token,
            "connected_at": datetime.now(),
            "is_active": True,
        }
        
        # Store in database (upsert to update if exists)
        result = db.instagram_connections.update_one(
            {"instagram_id": str(instagram_id), "org_id": org_id},
            {"$set": new_connection},
            upsert=True
        )

        result = db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"ig_id": str(instagram_id)}}
        )
        
        return {
            "status": "success",
            "id": str(instagram_id),
            "username": username,
            "profile_picture_url": profile_pic_url,
            "biography": profile_data.get("biography", ""),
            "followers_count": profile_data.get("followers_count", 0),
            "follows_count": profile_data.get("follows_count", 0),
            "media_count": profile_data.get("media_count", 0),
            "name": profile_data.get("name", ""),
        }
        
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        logger.error(f"Instagram authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Instagram authentication error: {str(e)}")

@router.post("/disconnect")
async def instagram_disconnect(request: PlatformDisconnectRequest, user: CurrentUser):
    """Revoke Instagram access token and remove connection"""
    try:
        org_id = user.org_id
        connection_id = request.user_id

        removal_result = await instagram_service.disconnect(org_id, connection_id)

        return {
            "message": (
                f"Instagram disconnected, archived data, "
                f"removed {removal_result['deleted_connections']} connections, "
                f"and cleared org Instagram reference."
            )
        }
       
    except Exception as e:
        logger.error(f"Error disconnecting Instagram: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect Instagram: {str(e)}")

# @router.get("/status")
# async def get_instagram_status(org_id: str, instagram_id: Optional[str] = None, app_user_id: Optional[str] = None, _: None = Depends(require_auth)):
#     """Check if Instagram account is connected and return connection details"""
#     try:
#         # At least one of the IDs must be provided
#         if not instagram_id and not app_user_id:
#             raise HTTPException(status_code=400, detail="Either instagram_id or app_user_id must be provided")
        
#         # Query by Instagram ID first if available - Remove await
#         if instagram_id:
#             connection = db.instagram_connections.find_one(
#                 {"instagram_id": instagram_id, "is_active": True, "org_id": org_id}
#             )
#         # If Instagram ID is not available or no connection found, try with app user ID
#         elif app_user_id:
#             connection = db.instagram_connections.find_one(
#                 {"app_user_id": app_user_id, "is_active": True, "org_id": org_id}
#             )
        
#         # If no connection found with either ID
#         if not connection:
#             return {
#                 "instagram_connected": False,
#                 "instagram_data": None
#             }
        
#         # Return connection details
#         return {
#             "instagram_connected": True,
#             "org_id": org_id,
#             "instagram_data": {
#                 "username": connection.get("username"),
#                 "user_id": connection.get("instagram_id"),
#                 "profile_picture": connection.get("profile_picture"),
#                 "business_info": connection.get("business_info"),
#                 "connected_at": connection.get("connected_at").isoformat() if connection.get("connected_at") else None
#             }
#         }
            
#     except HTTPException as http_exc:
#         raise http_exc
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))

@router.put("/link-user")
async def link_instagram_to_user(instagram_id: str, app_user_id: str, user: CurrentUser):
    """Link an Instagram connection to a specific app user ID"""
    try:
        org_id = user.org_id 
        # Update the Instagram connection record to add the app user ID - Remove await
        result = db.instagram_connections.update_one(
            {"instagram_id": instagram_id, "org_id": org_id},
            {
                "$set": {
                    "app_user_id": app_user_id,
                    "last_updated": datetime.utcnow()
                }
            }
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Instagram connection not found")
            
        return {"status": "success", "message": "Instagram account linked to user successfully"}
        
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/posts")
async def get_instagram_posts(user: CurrentUser, limit: int = 24):
    """
    Fetch Instagram posts for a specific Instagram business account
    
    Args:
        instagram_id: The Instagram business account ID
        limit: Maximum number of posts to return (default: 24)
        
    Returns:
        List of Instagram posts with media URLs and metadata
    """
    try:
        org_id = user.org_id 

        org_document = db.organizations.find_one({"org_id": org_id})

        instagram_id = org_document.get("ig_id")

        if not instagram_id:
            raise HTTPException(status_code=400, detail="Instagram ID not configured for this organization")

        # First, get the page access token for this Instagram account
        connection = db.instagram_connections.find_one(
            {"instagram_id": instagram_id, "is_active": True},
            {"page_access_token": 1, "page_id": 1}
        )
        
        if not connection or not connection.get("page_access_token"):
            raise HTTPException(status_code=404, detail="Instagram connection not found or inactive")
        
        page_access_token = connection["page_access_token"]
        
        # Call Instagram Graph API to get the media
        url = f"https://graph.instagram.com/{instagram_id}/media"
        params = {
            "access_token": page_access_token,
            "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,username",
            "limit": limit
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            logger.error(f"Instagram API Error: {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Failed to fetch Instagram posts: {response.json().get('error', {}).get('message')}"
            )
        
        posts_data = response.json()
        posts = posts_data.get("data", [])
        
        # Format posts and prepare response
        formatted_posts = []
        for post in posts:
            formatted_post = {
                "id": post.get("id"),
                "caption": post.get("caption", ""),
                "media_type": post.get("media_type"),
                "media_url": post.get("media_url", ""),
                "permalink": post.get("permalink", ""),
                "thumbnail_url": post.get("thumbnail_url", post.get("media_url", "")),
                "timestamp": post.get("timestamp"),
                "username": post.get("username", "")
            }
            formatted_posts.append(formatted_post)
        
        return {
            "status": "success",
            "data": formatted_posts,
            "paging": posts_data.get("paging", {})
        }
    
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching Instagram posts: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch Instagram posts: {str(e)}")

@router.get("/conversations/{conversation_id}")
async def get_instagram_conversation(conversation_id: str, user: CurrentUser):
    """
    Fetch the conversation of a specific user for the organization.
    Returns the conversation in the standard format for compatibility with the frontend
    """
    try:
        org_id = user.org_id 

        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if org_conversations_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        org_conversations_collection = db[org_conversations_collection_name]

        conversation = org_conversations_collection.find_one(
            {"conversation_id": conversation_id,
            "platform": "instagram"},
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
        logger.error(f"Error fetching Instagram conversation {conversation_id} for {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch Instagram conversation: {str(e)}"
        )

@router.get("/conversations")
async def get_instagram_conversations(user: CurrentUser):
    """
    Fetch all the Instagram conversations for a specific Organization using org_id
    Returns them in the standard conversation format for compatibility with the frontend
    """
    try:
        org_id = user.org_id 
        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Check if collection exists in database
        if org_conversations_collection_name not in db.list_collection_names():
            return []
        
        # Get the collection
        org_conversations_collection = db[org_conversations_collection_name]

        # Fetch conversations and sort by last_message_timestamp
        raw_conversations = list(org_conversations_collection.find(
            {"platform": "instagram"}, 
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
                'platform': 'instagram',
                'customer_id': conv.get('customer_username'),
                'customer_name': f"{conv.get('customer_name', '')}",
                'last_message': conv.get('last_message', ''),
                'timestamp': timestamp,
                'unread_count': conv.get('unread_count', 0),
                'is_ai_enabled': conv.get('is_ai_enabled', True),
                'assigned_agent_id': conv.get('assigned_agent_id'),
                'priority': conv.get('priority', 'medium'),  # Default to medium
                'sentiment': conv.get('sentiment', 'neutral'),  # Default to neutral
                'query': conv.get('query', '')  # Optional query field
            }
            
            formatted_conversations.append(formatted_conversation)

        return formatted_conversations
    
    except Exception as e:
        logger.error(f"Error fetching Instagram conversations for {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch Instagram conversations: {str(e)}"
        )

# Instagram-specific messages endpoint
@router.get("/conversations/{conversation_id}/messages")
async def get_instagram_messages(conversation_id: str, 
                                 user: CurrentUser,
                                 agent_username: str = Query(..., description="Username of the agent accessing this conversation")
                                 ):
    """Fetch messages for a specific Instagram conversation"""
    try:
        org_id = user.org_id 
        org_messages_collection_name = f"messages_{org_id}"
        private_note_collection_name = f"private_notes_{org_id}"

        # Auto assigning agent to the conversation if it is unassigned
        org_conversations_collection_name = f"conversations_{org_id}"
        convo_collection = db[org_conversations_collection_name]

        now = datetime.now(timezone.utc)

        assigned_convo = convo_collection.find_one_and_update(
            {
                "conversation_id": conversation_id,
                "$or": [
                    {"assigned_agent" : {"$exists": False}},
                    {"assigned_agent": None}
                ],
                "status": "open"
            },
            {
                "$set": {
                    "assigned_agent": {
                        "id": user.user_id,
                        "username": agent_username,
                        "assigned_at": now
                    }
                },
                "$push": {
                    "assignment_history": {
                        "id": user.user_id,
                        "username": agent_username,
                        "assigned_at": now
                    }
                }
            },
            projection={"assigned_agent": 1, "assignment_history": 1, "_id": 0},
            return_document=ReturnDocument.AFTER
        )

        messages = []
        notes = []
        
        # Fetching messages
        if org_messages_collection_name in db.list_collection_names():
            org_messages_collection = db[org_messages_collection_name]
            messages = list(org_messages_collection.find(
                {"conversation_id": conversation_id},
                {"_id": 0}  
            ).sort("timestamp", 1))  
        
        #Fetching private notes
        if private_note_collection_name in db.list_collection_names():
            private_note_collection = db[private_note_collection_name]
            notes = list(private_note_collection.find(
                    {"conversation_id": conversation_id},
                    {"_id": 0}
                ).sort("timestamp", 1)
            )

        # Merging both messages and notes
        combined = messages + notes

        # Normalizing timestamps
        for msg in combined:
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                msg["timestamp"] = ts.isoformat()

        combined.sort(key=lambda x: x.get("timestamp"), reverse=False)

        logger.info(f"Assigned conversation details: {assigned_convo}")
        
        return {"messages": combined, "assignment": assigned_convo}
    
    except Exception as e:
        logger.error(f"Error fetching Instagram messages or private notes for {conversation_id} of org {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch Instagram messages: {str(e)}"
        )

# Instagram-specific AI status endpoint
@router.get("/conversations/{conversation_id}/ai")
async def get_instagram_ai_status(conversation_id: str, user: CurrentUser):
    """Get AI status for a specific Instagram conversation"""
    try:
        org_id = user.org_id 
        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Get the collection
        org_conversations_collection = db[org_conversations_collection_name]
        
        # Find the conversation
        conversation = org_conversations_collection.find_one({"conversation_id": conversation_id})
        
        if conversation:
            is_ai_enabled = conversation.get("is_ai_enabled", True)  # Default to True if not set
            return is_ai_enabled
        
        return {"status": "Error", "message": "No conversation found"}
    
    except Exception as e:
        logger.error(f"Error checking Instagram AI status for conversation {conversation_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to check AI status: {str(e)}"
        )

# Instagram-specific AI toggle endpoint
@router.post("/conversations/{conversation_id}/toggle_ai")
async def toggle_instagram_ai_status(conversation_id: str, body: dict, user: CurrentUser):
    """Toggle AI status for a specific Instagram conversation"""
    try:
        org_id = user.org_id 
        # Get the enabled status from request body
        ai_enabled = body.get("enabled")
        
        if ai_enabled is None:
            return {"status": "error", "message": "Incorrect request body"}
        
        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Get the collection
        org_conversations_collection = db[org_conversations_collection_name]
        
        # Update the conversation
        result = org_conversations_collection.update_one(
            {"conversation_id": conversation_id}, 
            {"$set": {"is_ai_enabled": ai_enabled}}
        )
        
        if result.matched_count == 0:
            return {"status": "error", "message": "Conversation not found"}
        
        # Get updated conversation for broadcasting
        updated_conversation = org_conversations_collection.find_one({"conversation_id": conversation_id})

        instagram_id = updated_conversation.get("customer_id")
        
        # if updated_conversation:
        #     # Format for broadcasting
        #     broadcast_conversation = dict(updated_conversation)
        #     if "last_message_timestamp" in broadcast_conversation:
        #         broadcast_conversation["last_message_timestamp"] = broadcast_conversation["last_message_timestamp"].isoformat()
        #     if "_id" in broadcast_conversation:
        #         broadcast_conversation["_id"] = str(broadcast_conversation["_id"])
            
        #     # Broadcast to Instagram-specific connections
        #     if instagram_id in instagram_connections_map:
        #         await broadcast_instagram_message(instagram_id, "conversation_updated", {
        #             "conversation": broadcast_conversation
        #         })
        
        return {"status": "success", "message": f"AI status updated to {ai_enabled} for conversation {conversation_id}"}
    
    except Exception as e:
        logger.error(f"Error updating Instagram AI status: {e} for conversation {conversation_id}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to update AI status: {str(e)}"
        )

# Instagram-specific unread count reset endpoint
# @router.get("/conversations/{conversation_id}/read")
# async def update_instagram_unread_count(conversation_id: str, user: CurrentUser):
#     """Reset unread count for a specific Instagram conversation"""
#     try:
#         org_id = user.org_id 
#         # Get the Instagram-specific collection
#         org_conversations_collection = db[f"conversations_{org_id}"]

#         # Update the conversation
#         result = org_conversations_collection.update_one(
#             {"conversation_id": conversation_id},
#             {"$set": {"unread_count": 0}}
#         )
        
#         if result.matched_count == 0:
#             return {"status": "error", "message": "No conversation found"}
        
#         return {"status": "success", "message": f"Unread count reset to 0 for conversation {conversation_id}"}
    
#     except Exception as e:
#         print(f"Error resetting Instagram unread count: {e}")
#         raise HTTPException(
#             status_code=500, 
#             detail=f"Failed to reset unread count: {str(e)}"
#         )

# Instagram-specific send message endpoint
@router.post("/conversations/send_message")
async def send_instagram_message(message: dict, user: CurrentUser):
    """Send a message to an Instagram conversation"""
    try:
        org_id = user.org_id 
        conversation_id = message["conversation_id"]
        content = message["content"]
        sender_id = message["sender_id"]
        
        # Get Instagram-specific collections
        org_conversations_collection = db[f"conversations_{org_id}"]
        
        # Check if conversation exists
        conversation = org_conversations_collection.find_one({"conversation_id": conversation_id})
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Get user's Instagram ID from the conversation
        user_instagram_id = conversation.get("customer_id")
        
        if not user_instagram_id:
            raise HTTPException(status_code=400, detail="No Instagram ID found for this user")

        instagram_id = conversation.get("instagram_id")
        
        # Send message via Instagram API
        await send_ig_message(user_instagram_id, content, instagram_id)
        
        # no need to store message and broadcast here as due to insta sending us our own message using is_echo:true it is handled in webhook code to broadcast and store this msg
        
        return {"status": "success", "message": "Message sent"}
    
    except Exception as e:
        logger.error(f"Error sending Instagram message: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to send message: {str(e)}"
        )
    
# @router.post("/conversations/send_reaction")
# async def send_instagram_reaction(reaction: dict, user: CurrentUser):
#     """Send a reaction to an Instagram message"""
#     try:
#         org_id = user.org_id 
#         # emoji = reaction["emoji"]
#         message_id = reaction["message_id"]

#         messages_collection = db[f"messages_{org_id}"]

#         # Find the message first
#         message = messages_collection.find_one({"message_id": message_id})
#         recipient_id = message.get("sender_id")

#         if not message:
#             logger.error("Message not found")
#             return {"status": "error", "message": "Message not found"}
        
#         org_document = db.organizations.find_one({"org_id": org_id})
#         instagram_id = org_document.get("ig_id")

#         if not instagram_id:
#             raise HTTPException(status_code=400, detail="Instagram ID not configured for this organization")

#         # Get the page access token for this Instagram account
#         connection = db.instagram_connections.find_one(
#             {"instagram_id": instagram_id, "is_active": True, "org_id": org_id},
#             {"page_access_token": 1,"access_token" : 1, "page_id": 1}
#         )

#         if not connection or not connection.get("page_access_token"):
#             raise HTTPException(status_code=404, detail="Instagram connection not found or inactive")
        
#         page_access_token = connection["page_access_token"]

#         url = f"https://graph.instagram.com/me"
#         params = {
#             "access_token": page_access_token,
#             "fields": "name,profile_picture_url,username"
#         }
        
#         response = requests.get(url, params=params)
        
#         if response.status_code != 200:
#             logger.error(f"Instagram API Error: {response.text}")
#             raise HTTPException(
#                 status_code=response.status_code,
#                 detail=f"Failed to fetch Instagram profile: {response.json().get('error', {}).get('message')}"
#             )
        
#         owner_details = response.json()

#         # If reaction exists and same user reacted before → remove it
#         if message.get("reaction"):
#             current_reaction = message["reaction"]
#             if current_reaction.get("username") == owner_details.get("username") and current_reaction.get("emoji") == "❤️":
#                 response = await send_message_reaction(message_id, instagram_id, page_access_token, action="unreact", recipient_id=recipient_id)

#                 if response == "error":
#                     return {"status": "error", "message": "Failed to remove reaction IG API Exception"}
#                 # Unset the reaction
#                 broadcast_message = messages_collection.find_one_and_update(
#                     {"message_id": message_id},
#                     {"$unset": {"reaction": 1}}, 
#                     return_document=ReturnDocument.AFTER
#                 )
#                 logger.success(f"Reaction removed for message {message_id}")

#                 if "_id" in broadcast_message:
#                     broadcast_message["_id"] = str(broadcast_message["_id"])

#                 await broadcast_main_ws(
#                     platform_id=instagram_id,
#                     platform_type="instagram",
#                     event_type="message_reaction",
#                     payload={"message": broadcast_message}
#                 )

#                 # Broadcast the reaction update to connected clients
#                 if instagram_id in services.instagram_connections_map:
#                     logger.info(f"Broadcasting message_reaction event to Frontend for {instagram_id}")
#                     await broadcast_instagram_message(instagram_id, "message_reaction", {
#                         "message": broadcast_message
#                     })

#                 logger.success(f"Reaction removed for message {message_id} with emoji ❤️")


#                 return {"status": "success", "message": "sent"}

#         response = await send_message_reaction(message_id, instagram_id, page_access_token, action="react", recipient_id=recipient_id)

#         if response == "error":
#             return {"status": "error", "message": "Failed to remove reaction IG API Exception"}

#         # Otherwise set a new reaction
#         broadcast_message = messages_collection.find_one_and_update(
#             {"message_id": message_id},
#             {"$set": {
#                 "reaction": {
#                     "emoji": "❤️",
#                     "username": owner_details.get("username"),
#                     "full_name": owner_details.get("name"),
#                     "profile_picture_url": owner_details.get("profile_picture_url"),
#                     "timestamp": datetime.now()
#                 }
#             }}, 
#             return_document=ReturnDocument.AFTER
#         )

#         if "_id" in broadcast_message:
#             broadcast_message["_id"] = str(broadcast_message["_id"])

#         await broadcast_main_ws(
#             platform_id=instagram_id,
#             platform_type="instagram",
#             event_type="message_reaction",
#             payload={"message": broadcast_message}
#         )

#         # Broadcast the reaction update to connected clients
#         if instagram_id in services.instagram_connections_map:
#             logger.info(f"Broadcasting message_reaction event to Frontend for {instagram_id}")
#             await broadcast_instagram_message(instagram_id, "message_reaction", {
#                 "message": broadcast_message
#             })

#         logger.success(f"Reaction updated for message {message_id} with emoji ❤️")
#         return {"status": "success", "message": "sent"}
        
    
#     except Exception as e:
#         logger.error(f"Error sending Instagram reaction: {e}")
#         raise HTTPException(
#             status_code=500, 
#             detail=f"Failed to send reaction: {str(e)}"
#         )
    
@router.post("/conversations/send_media_message")
async def upload_and_send_instagram_media(
    user: CurrentUser,
    caption: str = Form(...),
    conversation_id: str = Form(...), 
    file: UploadFile = File(...), 
):
    """
    Upload media to Cloudinary servers, get the public URL, then send it as media message to instagram via Instagram API.
    
    Args:
        caption: The caption for the media (Text to be sent along with image message)
        conversation_id: The ID of the conversation
        file: The file to upload
    
    Returns:
        Status and message ID details along with the uploaded media URL
    """
    logger.info("Received request to upload and send Instagram media")
    logger.info(f"Caption: {caption}, Conversation ID: {conversation_id}, Filename: {file.filename}")


    try:
        org_id = user.org_id
        agent_user_id = user.user_id

        # Get Instagram-specific collections
        org_conversations_collection = db[f"conversations_{org_id}"]
        
        # Check if conversation exists
        conversation = org_conversations_collection.find_one({"conversation_id": conversation_id})
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Get user's Instagram ID from the conversation
        user_instagram_id = conversation.get("customer_id")
        
        if not user_instagram_id:
            raise HTTPException(status_code=400, detail="No Instagram ID found for this user")

        instagram_id = conversation.get("instagram_id")
        
        # Get the secure URL for the frontend to display the image
        media_url = upload_media_to_cloudinary(file, platform="instagram", org_id=org_id)

        if not media_url:
            raise HTTPException(
                status_code=500,
                detail="Media upload failed"
            )
        
        # Now send the media message via Instagram API
        await send_ig_media_message(
            user_instagram_id,
            media_url,
            caption,
            instagram_id,
        )

        if caption : await send_ig_message(user_instagram_id, caption, instagram_id) and logger.success(f"Caption sent: {caption}")

        return {"status": "success", "message": "Message sent"}
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error uploading and sending Instagram media: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload and send media: {str(e)}"
        )

# Proxy endpoint to fetch Instagram media content and return back to frontend
@router.get("/proxy/instagram-media")
def proxy_instagram_media(url: str = Query(..., description="Full Instagram CDN URL")):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive"
        }
        r = requests.get(url, headers=headers, stream=True, timeout=10)

        logger.info(f"Proxy fetch status: {r.status_code} for URL: {url}")
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"CDN fetch failed ({r.status_code})")

        return Response(
            content=r.content,
            media_type=r.headers.get("content-type", "application/octet-stream")
        )

    except Exception as e:
        logger.error(f"Error fetching Instagram media: {e}")
        raise HTTPException(status_code=500, detail=str(e))
