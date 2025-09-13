# === Third-party Modules ===
import dateparser
from pymongo.errors import PyMongoError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

# === Internal Modules ===
from database import get_mongo_db
from services.followup_agent_service import get_followup_message
from schemas.models import ChatbotPromptUpdate
from auth.dependencies import CurrentUser
from services.ai_chatbot import update_prompt_in_cache

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

@router.get("/product-recommendations/{conversation_id}")
async def get_product_recommendations(conversation_id: str, user: CurrentUser):
    """
    This endpoint retrieves product recommendations for a specific conversation.
    """
    try:
        org_id = user.org_id 
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID missing in user context")

        conversation_col_name = f"conversations_{org_id}"
        conversations_collection = db[conversation_col_name]

        try:
            conversation = conversations_collection.find_one({"id": conversation_id})
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if conversation:
            products = conversation.get("product_recommendations")
            if products:
                return {"status": "success", "data": products}
            return {"status": "error", "message": "No product recommendations found"}

        return {"status": "error", "message": "No conversation found"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@router.put("/chatbot-prompt")
def update_chatbot_prompt(payload: ChatbotPromptUpdate, user: CurrentUser):
    """
    Update the chatbot prompt for the authenticated user's organization.
    """
    try:
        org_id = user.org_id 
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found in user context.")

        try:
            result = db.organizations.update_one(
                {"org_id": org_id},
                {"$set": {"chatbot_prompt": payload.chatbot_prompt}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Organization not found in MongoDB.")

        try:
            update_prompt_in_cache(org_id, payload.chatbot_prompt)
        except Exception as e:
            # Cache update failure should not break the API
            raise HTTPException(status_code=500, detail=f"Failed to update cache: {str(e)}")

        return {"status": "success", "message": "Chatbot prompt updated successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@router.post("/schedule-message/{conversation_id}")
async def schedule_message(conversation_id: str, user: CurrentUser):
    """
    Schedule a follow-up message for a conversation using an AI agent to determine the content and timing.
    """
    try:
        org_id = user.org_id 
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found.")

        platform = conversation_id.split("_")[0]

        scheduled_messages_collection = db["scheduled_messages"]

        conversation_collection = db[f"conversations_{org_id}"]
        conversation = conversation_collection.find_one({"conversation_id": conversation_id})

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        receiver_id = conversation.get("customer_id")
        sender_id = conversation.get("instagram_id")

        if not receiver_id or not sender_id:
            raise HTTPException(
                status_code=400, 
                detail="Missing sender_id or receiver_id in conversation"
            )

        # Running through LLM follow-up agent
        try:
            followup_result = await get_followup_message(org_id, conversation_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Follow-up generation failed: {str(e)}")

        # Converting delay (natural language) → ISO datetime
        try:
            send_time = dateparser.parse(
                followup_result.delay,
                settings={
                    "TIMEZONE": "UTC",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Delay parsing error: {str(e)}")

        if not send_time:
            raise HTTPException(status_code=500, detail="Failed to parse follow-up delay.")

        # iso_time = send_time.isoformat()
        
        followup_doc = {
            "org_id": org_id,
            "platform": platform,
            "conversation_id": conversation_id,
            "sender_id": sender_id,        
            "receiver_id": receiver_id, 
            "followup_text": followup_result.followup,
            "delay": followup_result.delay,
            "scheduled_time": send_time,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }

        # Store follow-up message
        try:
            scheduled_messages_collection.insert_one(followup_doc)
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        return {"status": "success", "message": "Follow-up message scheduled successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

    