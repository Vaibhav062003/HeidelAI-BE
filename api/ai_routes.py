# === Third-party Modules ===
from typing import Literal, Optional

from pymongo.errors import PyMongoError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

# === Internal Modules ===
from core.limiter import limiter
from loguru import logger
from database import get_mongo_db
from services.followup_service import list_scheduled_followups, schedule_followup_message, transition_scheduled_followup
from schemas.models import ChatbotPromptUpdate, ChatAnalysis
from auth.dependencies import CurrentUser
from services.ai_chatbot import update_prompt_in_cache, get_prompt
from services.chat_analysis_service import analyze_and_update_conversation

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

def _org_metadata_collection():
    return db["organizations_metadata"]


class ScheduleFollowUpRequest(BaseModel):
    source: Literal["manual", "quotation"] = "manual"
    quote_id: Optional[str] = Field(default=None, max_length=80)
    scheduled_for: Optional[datetime] = None


class FollowUpStatusRequest(BaseModel):
    status: Literal["cancelled", "fulfilled"]

def conversation_doc_to_analysis(doc: dict) -> ChatAnalysis:
    return ChatAnalysis(
        conversation_id=doc.get("conversation_id"),
        summary=doc.get("summary", []),
        priority=doc.get("priority"),
        sentiment=doc.get("sentiment"),
        suggestions=doc.get("suggestions", []),
        tags=doc.get("tags", []),
        labels=doc.get("labels", []),
        last_analyzed=doc.get("last_analyzed"),
    )

@router.get("/chatbot-prompt")
@limiter.limit("20/minute")
async def get_chatbot_prompt(request: Request, user: CurrentUser):
    """Get the chatbot prompt for the authenticated user's organization."""
    prompt = await get_prompt(user.org_id)
    
    return prompt

@router.put("/chatbot-prompt")
@limiter.limit("20/minute")
async def update_chatbot_prompt(request: Request, payload: ChatbotPromptUpdate, user: CurrentUser):
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
@limiter.limit("100/minute")
async def schedule_message(
    request: Request,
    conversation_id: str,
    user: CurrentUser,
    payload: Optional[ScheduleFollowUpRequest] = None,
):
    """
    Schedule a follow-up using the customer's timeline or the fixed three-hour default.
    """
    try:
        body = payload or ScheduleFollowUpRequest()
        if body.source == "quotation" and not body.quote_id:
            raise HTTPException(status_code=422, detail="A quote is required for quotation follow-ups.")
        return await schedule_followup_message(
            user.org_id,
            conversation_id,
            source=body.source,
            quote_id=body.quote_id,
            requested_by=user.user_id,
            requested_by_name=user.full_name,
            scheduled_for=body.scheduled_for,
        )
    except HTTPException as e:
        # Graceful return for API
        raise e
    except Exception as e:
        logger.error(f"Unexpected follow-up scheduling error for {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="The follow-up could not be scheduled. Try again.")


@router.get("/followups")
@limiter.limit("60/minute")
async def get_followups(request: Request, user: CurrentUser, limit: int = Query(200, ge=1, le=500)):
    return {"followups": await list_scheduled_followups(user.org_id, limit)}


@router.patch("/followups/{followup_id}")
@limiter.limit("60/minute")
async def update_followup_status(
    request: Request,
    followup_id: str,
    payload: FollowUpStatusRequest,
    user: CurrentUser,
):
    followup = await transition_scheduled_followup(
        user.org_id,
        followup_id,
        payload.status,
        user.user_id,
        user.full_name or "Team member",
    )
    return {"followup": followup}

@router.get("/chat-analysis/{conversation_id}")
@limiter.limit("20/minute")
async def run_chat_analysis(request: Request, conversation_id: str, user: CurrentUser, force: bool = False):
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
        contact_collection_name = f"contacts_{org_id}"
        conversation_collection = db[conversation_collection_name]
        contact_collection = db[contact_collection_name]

        projection = {
            "_id": 0,
            "conversation_id": 1,
            "summary": 1,
            "priority": 1,
            "sentiment": 1,
            "suggestions": 1,
            "tags": 1,
            "labels": 1,
            "last_analyzed": 1,
        }

        conversation = conversation_collection.find_one(
            {"conversation_id": conversation_id}, projection
        )

        contact = contact_collection.find_one(
            {"conversation_id": conversation_id},
            {"_id": 0, "categories": 1, "labels": 1}
        )

        logger.debug(f"Contact document {'found' if contact else 'not found'} for conversation_id={conversation_id}")
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")
        
        cached_analysis = conversation_doc_to_analysis(conversation)

        # Merge tags/labels from contact document
        if contact:
            cached_analysis.tags = contact.get("categories", [])
            cached_analysis.labels = contact.get("labels", [])

        # check last analysis time
        last_analysis_time = conversation.get("last_analyzed", None)

        if last_analysis_time:
            # Make sure it's timezone-aware
            if last_analysis_time.tzinfo is None:
                last_analysis_time = last_analysis_time.replace(tzinfo=timezone.utc)

        if force or not last_analysis_time or ((datetime.now(timezone.utc) - last_analysis_time).total_seconds()/60 >= 10):
            new_result = await analyze_and_update_conversation(org_id, conversation_id) 

            if new_result is not None:
                # Re-fetch contact to get updated tags/labels
                contact = contact_collection.find_one(
                    {"conversation_id": conversation_id},
                    {"_id": 0, "categories": 1, "labels": 1}
                )
                if contact:
                    new_result.tags = contact.get("categories", [])
                    new_result.labels = contact.get("labels", [])
                return new_result
                
        return cached_analysis

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")