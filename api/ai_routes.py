# === Third-party Modules ===
from typing import Literal, Optional
import uuid

from pymongo.errors import PyMongoError
from bson import ObjectId
from bson.errors import InvalidId
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

# === Internal Modules ===
from core.limiter import limiter
from loguru import logger
from database import get_mongo_db, get_async_mongo_db
from services.followup_service import (
    cancel_future_followup,
    list_scheduled_followups,
    schedule_followup_message,
    transition_scheduled_followup,
)
from services.entitlement_service import commit_quota_async, record_ai_usage_async, release_quota_async, require_feature, reserve_quota_async
from services.agent_service import send_to_agent
from schemas.models import ChatbotPromptUpdate, ChatAnalysis
from auth.dependencies import CurrentUser
from auth.auth_utils import admin_required
from services.ai_chatbot import update_prompt_in_cache, get_prompt
from services.chat_analysis_service import analyze_and_update_conversation
from services.ai_policy_service import (
    get_org_ai_policy,
    set_conversation_ai_state,
    update_org_ai_policy,
)
from services.work_team_service import conversation_access_condition, require_conversation_access

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])


def _followup_object_id(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Follow-up not found.")

def _org_metadata_collection():
    return db["organizations_metadata"]


class ScheduleFollowUpRequest(BaseModel):
    source: Literal["manual", "quotation"] = "manual"
    quote_id: Optional[str] = Field(default=None, max_length=80)
    scheduled_for: Optional[datetime] = None


class FollowUpStatusRequest(BaseModel):
    status: Literal["cancelled", "fulfilled"]


class AIPolicyRequest(BaseModel):
    enabled: bool
    pause_minutes: int = Field(default=60, ge=5, le=1440)
    quote_followup_prompt_enabled: Optional[bool] = None


class ConversationAIStateRequest(BaseModel):
    action: Literal["pause", "resume"]
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=1440)


class PlaygroundChatRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=12_000)
    persona: str = Field(default="fallback", max_length=100)
    location: str = Field(default="india", max_length=100)


@router.get("/policy")
@limiter.limit("60/minute")
async def get_ai_policy(request: Request, user: CurrentUser):
    return await get_org_ai_policy(user.org_id)


@router.put("/policy")
@limiter.limit("30/minute")
async def put_ai_policy(request: Request, payload: AIPolicyRequest, user: CurrentUser):
    await admin_required(user)
    return await update_org_ai_policy(
        user.org_id,
        payload.enabled,
        payload.pause_minutes,
        payload.quote_followup_prompt_enabled,
    )


@router.post("/playground/chat")
@limiter.limit("30/minute")
async def playground_chat(request: Request, payload: PlaygroundChatRequest, user: CurrentUser):
    require_feature(user.org_id, "AI")
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    event_key = f"playground:{user.user_id}:{request_id}"
    await reserve_quota_async(user.org_id, "AI", "monthly_messages", 1, event_key)
    try:
        result = await send_to_agent(
            conversation_id=payload.conversation_id,
            user_input=payload.message,
            persona=payload.persona,
            location=payload.location,
            customer_name=user.full_name or "User",
            org_id=user.org_id,
            source="ai_playground",
            message_id=request_id,
        )
    except Exception:
        await release_quota_async(user.org_id, "monthly_messages", event_key)
        raise
    await commit_quota_async(user.org_id, "monthly_messages", event_key, "0")
    usage = result.get("usage") or {}
    await record_ai_usage_async(user.org_id, event_key, "playground", 1, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)), int(usage.get("recorded_cost_microusd", 0)))
    return {"response": result["final_response"]}


@router.post("/conversations/{conversation_id}/state")
@limiter.limit("60/minute")
async def change_conversation_ai_state(
    request: Request,
    conversation_id: str,
    payload: ConversationAIStateRequest,
    user: CurrentUser,
):
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    return await set_conversation_ai_state(
        user.org_id,
        conversation_id,
        payload.action,
        payload.duration_minutes,
    )

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
        require_feature(user.org_id, "FOLLOW_UPS")
        body = payload or ScheduleFollowUpRequest()
        await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
        if body.source == "quotation" and not body.quote_id:
            raise HTTPException(status_code=422, detail="A quote is required for quotation follow-ups.")
        result = await schedule_followup_message(
            user.org_id,
            conversation_id,
            source=body.source,
            quote_id=body.quote_id,
            requested_by=user.user_id,
            requested_by_name=user.full_name,
            scheduled_for=body.scheduled_for,
        )
        if body.source == "quotation" and result.get("requires_agent_time"):
            policy = await get_org_ai_policy(user.org_id)
            result["prompt_immediately"] = policy["quote_followup_prompt_enabled"]
        return result
    except HTTPException as e:
        # Graceful return for API
        raise e
    except Exception as e:
        logger.error(f"Unexpected follow-up scheduling error for {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="The follow-up could not be scheduled. Try again.")


@router.get("/followups")
@limiter.limit("60/minute")
async def get_followups(
    request: Request,
    user: CurrentUser,
    limit: int = Query(200, ge=1, le=500),
    conversation_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
):
    allowed_ids = None
    if conversation_id:
        await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    else:
        access = await conversation_access_condition(user.org_id, user.user_id, user.role)
        if access:
            allowed_ids = await get_async_mongo_db()[f"conversations_{user.org_id}"].distinct("conversation_id", access)
    return {
        "followups": await list_scheduled_followups(
            user.org_id,
            limit,
            conversation_id=conversation_id,
            conversation_ids=allowed_ids,
        )
    }


@router.patch("/followups/{followup_id}")
@limiter.limit("60/minute")
async def update_followup_status(
    request: Request,
    followup_id: str,
    payload: FollowUpStatusRequest,
    user: CurrentUser,
):
    require_feature(user.org_id, "FOLLOW_UPS")
    existing = await get_async_mongo_db().scheduled_messages.find_one(
        {"_id": _followup_object_id(followup_id), "org_id": user.org_id},
        {"conversation_id": 1},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    await require_conversation_access(user.org_id, existing["conversation_id"], user.user_id, user.role)
    followup = await transition_scheduled_followup(
        user.org_id,
        followup_id,
        payload.status,
        user.user_id,
        user.full_name or "Team member",
    )
    return {"followup": followup}


@router.post("/followups/{followup_id}/cancel-future")
@limiter.limit("60/minute")
async def cancel_next_followup(
    request: Request,
    followup_id: str,
    user: CurrentUser,
):
    require_feature(user.org_id, "FOLLOW_UPS")
    existing = await get_async_mongo_db().scheduled_messages.find_one(
        {"_id": _followup_object_id(followup_id), "org_id": user.org_id},
        {"conversation_id": 1},
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    await require_conversation_access(user.org_id, existing["conversation_id"], user.user_id, user.role)
    return await cancel_future_followup(
        user.org_id,
        followup_id,
        user.user_id,
        user.full_name or "Team member",
    )


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
    await require_conversation_access(org_id, conversation_id, user.user_id, user.role)
    
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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat analysis failed for {conversation_id}: {type(e).__name__}")
        raise HTTPException(status_code=503, detail="AI analysis is temporarily unavailable.")
