# === Third-party Modules ===
import dateparser
from pymongo.errors import PyMongoError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

# === Internal Modules ===
from database import get_mongo_db
from services.scheduler_service import schedule_followup_message
from schemas.models import ChatbotPromptUpdate, ChatAnalysis
from auth.dependencies import CurrentUser
from services.ai_chatbot import update_prompt_in_cache, get_prompt
from services.chat_analysis_service import analyze_and_update_conversation

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

def _org_metadata_collection():
    return db["organizations_metadata"]

def conversation_doc_to_analysis(doc: dict) -> ChatAnalysis:
    return ChatAnalysis(
        conversation_id=doc.get("conversation_id"),
        summary=doc.get("summary", []),
        priority=doc.get("priority"),
        sentiment=doc.get("sentiment"),
        suggestions=doc.get("suggestions", []),
        last_analyzed=doc.get("last_analyzed"),
    )

@router.get("/chatbot-prompt")
async def get_chatbot_prompt(user: CurrentUser):
    """Get the chatbot prompt for the authenticated user's organization."""
    prompt = await get_prompt(user.org_id)
    
    return prompt

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
            result = _org_metadata_collection().update_one(
                {"org_id": org_id},
                {"$set": {"chatbot.prompt": payload.chatbot_prompt}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Organization not found in MongoDB.")

        try:
            update_prompt_in_cache(org_id, payload.chatbot_prompt)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update cache: {str(e)}")

        return {"message": "Chatbot prompt updated successfully."}

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
        return await schedule_followup_message(user.org_id, conversation_id)
    except HTTPException as e:
        # Graceful return for API
        raise e
    except Exception as e:
        # Catch anything unexpected that leaked through
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@router.get("/chat-analysis/{conversation_id}")
async def run_chat_analysis(conversation_id: str, user: CurrentUser, force: bool = False):
    """
    This endpoint retrieves the analysis results of the chat for a specific conversation.
    force: If true, forces re-analysis even if recent analysis exists.
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
        
        cached_analysis = conversation_doc_to_analysis(conversation)

        # check last analysis time
        last_analysis_time = conversation.get("last_analyzed", None)

        if last_analysis_time:
            # Make sure it's timezone-aware
            if last_analysis_time.tzinfo is None:
                last_analysis_time = last_analysis_time.replace(tzinfo=timezone.utc)

        if force or not last_analysis_time or ((datetime.now(timezone.utc) - last_analysis_time).total_seconds()/60 >= 10):
            new_result = await analyze_and_update_conversation(org_id, conversation_id) 

            if new_result is None:
                return cached_analysis
                
        return cached_analysis

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")