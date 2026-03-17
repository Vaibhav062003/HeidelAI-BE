"""
team_routes.py

Dedicated API routes for team (internal) conversations.

Endpoints:
  GET  /api/team/conversations                        → list team conversations
  GET  /api/team/conversations/{conversation_id}/messages → fetch messages

Message sending continues to use the existing shared endpoint:
  POST /api/conversations/send-message  (platform="internal")

WebSocket events (new_team_message) are also unchanged.
"""

from fastapi import APIRouter, HTTPException
from loguru import logger

from auth.dependencies import CurrentUser
from services.team_service import fetch_team_conversations, fetch_team_messages


router = APIRouter(prefix="/api/team", tags=["Team Routes"])


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
