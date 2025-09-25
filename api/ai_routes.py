# === Third-party Modules ===
import dateparser
from pymongo.errors import PyMongoError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

# === Internal Modules ===
from database import get_mongo_db
from services.scheduler_service import schedule_followup_message
from schemas.models import ChatbotPromptUpdate
from auth.dependencies import CurrentUser
from services.ai_chatbot import update_prompt_in_cache
from services.chat_analysis_service import analyze_and_update_conversation

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

@router.put("/chatbot-prompt")
async def update_chatbot_prompt(payload: ChatbotPromptUpdate, user: CurrentUser):
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
        return schedule_followup_message(db, user.org_id, conversation_id)
    except HTTPException as e:
        # Graceful return for API
        raise e
    except Exception as e:
        # Catch anything unexpected that leaked through
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@router.get("/chat-analysis/{conversation_id}")
async def run_chat_analysis(conversation_id: str, user: CurrentUser):
    """
    This endpoint retrieves the analysis results of the chat for a specific conversation.
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found.")
    
    try:
        # get conversation from DB
        conversation_collection_name = f"conversations_{org_id}"
        conversation_collection = db[conversation_collection_name]

        projection = {
            "_id": 0,
            "conversation_id": 1,
            "summary": 1,
            "priority": 1,
            "sentiment": 1,
            "suggestions": 1,
            "last_analyzed": 1,
        }

        conversation = conversation_collection.find_one(
            {"conversation_id": conversation_id}, projection
        )

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        # check last analysis time
        last_analysis_time = conversation.get("last_analyzed", None)

        if last_analysis_time:
            # Make sure it's timezone-aware
            if last_analysis_time.tzinfo is None:
                last_analysis_time = last_analysis_time.replace(tzinfo=timezone.utc)

        if not last_analysis_time or ((datetime.now(timezone.utc) - last_analysis_time).total_seconds() / 60 >= 20):
            result = await analyze_and_update_conversation(org_id, conversation_id) 
        else:
            result = conversation

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")