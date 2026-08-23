from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from api.transfer_routes import get_org_memberships
from services.work_team_service import (
    create_conversation_work,
    list_conversation_work,
    list_work_teams,
    record_team_presence,
    save_work_team,
    update_conversation_work,
)


router = APIRouter(tags=["Work Teams"])


class TeamPayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: Literal["sales", "billing", "support", "general"]
    member_ids: list[str] = Field(default_factory=list, max_length=200)
    is_default_inbox_team: bool = False


class WorkPayload(BaseModel):
    type: Optional[Literal["pricing", "billing", "support", "general"]] = None
    team_id: str
    assigned_agent_id: Optional[str] = None
    quote_id: Optional[str] = None
    quote_revision: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=500)


class WorkUpdatePayload(BaseModel):
    status: Literal["pending", "in_progress", "needs_review", "completed", "cancelled"]
    version: int = Field(ge=1)
    result: Optional[Literal["quoted", "confirmed", "billed", "resolved"]] = None
    invoice_reference: Optional[str] = Field(default=None, max_length=120)


async def _validated_team_payload(user: CurrentUser, payload: TeamPayload) -> dict:
    memberships = await get_org_memberships(user.org_id)
    active_ids = {
        item.get("public_user_data", {}).get("user_id")
        for item in memberships
        if not item.get("public_user_data", {}).get("banned")
    }
    unknown = set(payload.member_ids) - active_ids
    if unknown:
        raise HTTPException(status_code=422, detail="One or more selected members are no longer active")
    return payload.model_dump()


@router.get("/api/work-teams")
@limiter.limit("60/minute")
async def get_work_teams(request: Request, user: CurrentUser):
    await record_team_presence(user.org_id, user.user_id)
    return {"teams": await list_work_teams(user.org_id)}


@router.post("/api/work-teams", status_code=201)
@limiter.limit("20/minute")
async def create_work_team(request: Request, payload: TeamPayload, user: CurrentUser):
    await admin_required(user)
    return {"team": await save_work_team(user.org_id, await _validated_team_payload(user, payload))}


@router.put("/api/work-teams/{team_id}")
@limiter.limit("20/minute")
async def update_work_team(request: Request, team_id: str, payload: TeamPayload, user: CurrentUser):
    await admin_required(user)
    return {"team": await save_work_team(user.org_id, await _validated_team_payload(user, payload), team_id)}


@router.get("/api/conversations/{conversation_id}/work")
@limiter.limit("60/minute")
async def get_conversation_work(request: Request, conversation_id: str, user: CurrentUser):
    return {"work_items": await list_conversation_work(user.org_id, conversation_id, user.user_id, user.role)}


@router.post("/api/conversations/{conversation_id}/work", status_code=201)
@limiter.limit("30/minute")
async def create_work(request: Request, conversation_id: str, payload: WorkPayload, user: CurrentUser):
    work, created = await create_conversation_work(
        user.org_id, conversation_id, user.user_id, user.full_name or "Team member", user.role, payload.model_dump()
    )
    return {"work_item": work, "created": created}


@router.patch("/api/conversation-work/{work_id}")
@limiter.limit("30/minute")
async def patch_work(request: Request, work_id: str, payload: WorkUpdatePayload, user: CurrentUser):
    await record_team_presence(user.org_id, user.user_id)
    return {"work_item": await update_conversation_work(
        user.org_id, work_id, user.user_id, user.full_name or "Team member", user.role, payload.model_dump()
    )}
