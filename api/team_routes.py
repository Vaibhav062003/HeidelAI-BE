"""
team_routes.py

Dedicated API routes for team (internal) conversations.

Endpoints:
  GET  /api/team/conversations                        → list team conversations
  GET  /api/team/conversations/{conversation_id}/messages → fetch messages
  POST /api/team/rooms                                → create a private room
  PATCH /api/team/rooms/{room_id}                     → manage a private room
  POST /api/team/rooms/{room_id}/leave                → leave a private room

Message sending continues to use the existing shared endpoint:
  POST /api/conversations/send-message  (platform="internal")

WebSocket events keep messages and room membership changes in sync.
"""

from services.team_service import (
    fetch_team_messages,
    fetch_team_conversations,
    fetch_ai_escalations,
    fetch_ai_escalations_v2,
    update_escalation_status,
    create_ai_escalation,
    fetch_team_directory,
    create_team_room,
    update_team_room,
    leave_team_room,
)
from fastapi import APIRouter, HTTPException, Header, Query
from auth.dependencies import CurrentUser
from config import settings
from typing import Literal, Optional
from pydantic import BaseModel, Field
from loguru import logger

router = APIRouter(prefix="/api/team", tags=["Team Routes"])


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    member_ids: list[str] = Field(default_factory=list, max_length=99)


class RoomUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    member_ids: Optional[list[str]] = Field(default=None, max_length=100)


@router.get("/directory")
async def get_team_directory(user: CurrentUser):
    if not user.org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    return await fetch_team_directory(user.org_id, user.user_id)


@router.post("/rooms")
async def add_team_room(payload: RoomCreate, user: CurrentUser):
    if not user.org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    return await create_team_room(user.org_id, user.user_id, payload.name, payload.member_ids)


@router.patch("/rooms/{room_id}")
async def edit_team_room(room_id: str, payload: RoomUpdate, user: CurrentUser):
    if not user.org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    if payload.name is None and payload.member_ids is None:
        raise HTTPException(status_code=422, detail="Provide a room name or members")
    return await update_team_room(user.org_id, user.user_id, room_id, payload.name, payload.member_ids)


@router.post("/rooms/{room_id}/leave")
async def exit_team_room(room_id: str, user: CurrentUser):
    if not user.org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    return await leave_team_room(user.org_id, user.user_id, room_id)


@router.get("/conversations")
async def get_team_conversations(user: CurrentUser):
    """
    List all team (internal) conversations for the authenticated user.
    Only returns conversations where the user is a participant.
    Reads from: team_conversations_{org_id}
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    try:
        conversations = await fetch_team_conversations(
            org_id=org_id,
            current_user_id=user.user_id,
        )
        return conversations
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_team_conversations error for org={org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch team conversations")


@router.get("/conversations/{conversation_id}/messages")
async def get_team_messages(conversation_id: str, user: CurrentUser):
    """
    Fetch all messages for a specific team (internal) conversation.
    Also resets the per-user unread count for this conversation.
    Reads from: team_messages_{org_id}
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    try:
        result = await fetch_team_messages(
            org_id=org_id,
            conversation_id=conversation_id,
            agent_id=user.user_id,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"get_team_messages error for conv={conversation_id} org={org_id}: {e}"
        )
        raise HTTPException(status_code=500, detail="Failed to fetch team messages")
@router.get("/escalations")
async def get_team_escalations(user: CurrentUser):
    """
    List all AI escalations that are pending human review.
    Discovery endpoint for agents to find new escalations in their org.
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    try:
        escalations = await fetch_ai_escalations(org_id=org_id)
        return escalations
    except Exception as e:
        logger.error(f"get_team_escalations error for org={org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch escalations")


@router.get("/escalations/v2")
async def get_team_escalations_v2(
    user: CurrentUser,
    status: Literal["all", "pending", "pending_human_review", "reviewed"] = Query("pending"),
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
):
    """Cursor-paginated pending or reviewed escalations for the current org."""
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    try:
        return await fetch_ai_escalations_v2(
            org_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"get_team_escalations_v2 error for org={org_id}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to fetch escalations") from exc


class StatusUpdate(BaseModel):
    status: Literal["pending_human_review", "reviewed"]


@router.patch("/escalations/{id}/status")
async def update_escalation_status_route(
    id: str,
    update: StatusUpdate,
    user: CurrentUser
):
    """
    Update the status of a specific AI escalation.
    Example: Change from 'pending_human_review' to 'reviewed'.
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    try:
        success = await update_escalation_status(
            org_id=org_id,
            escalation_id=id,
            status=update.status,
            reviewer_id=user.user_id,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Escalation not found")
        
        return {"status": "success", "new_status": update.status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_escalation_status error for id={id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update status")


# ---------------------------------------------------------------------------
# Internal Endpoint: Create AI Escalation
# ---------------------------------------------------------------------------

@router.post("/escalations/internal")
async def internal_create_escalation(
    payload: dict,
    x_internal_secret: str = Header(None, alias="X-Internal-Secret")
):
    """
    Internal endpoint for AI tools to push new escalations.
    Broadcasts a live notification to connected agents.
    """
    if not settings.X_INTERNAL_SECRET or x_internal_secret != settings.X_INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized internal access")

    org_id = payload.get("org_id")
    data = payload.get("data")

    if not org_id or not data:
        raise HTTPException(status_code=400, detail="Missing org_id or escalation data")

    try:
        new_esc = await create_ai_escalation(org_id, data)
        return {"status": "success", "escalation_id": new_esc["id"]}
    except Exception as e:
        logger.error(f"Error in internal_create_escalation: {e}")
        raise HTTPException(status_code=500, detail="Failed to create escalation")
