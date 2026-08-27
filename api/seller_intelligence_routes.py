from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.entitlement_service import require_feature
from services.seller_intelligence_service import (
    build_snapshot,
    get_item,
    get_preferences,
    save_preferences,
    update_item_state,
)


router = APIRouter(prefix="/api/seller-intelligence", tags=["Seller intelligence"])


class ItemStatePayload(BaseModel):
    action: Literal["acknowledge", "snooze", "dismiss"]
    snooze_until: datetime | None = None


class PreferencesPayload(BaseModel):
    default_scope: Literal["organization", "team", "mine"]
    minimum_score: int = Field(default=0, ge=0, le=100)
    show_acknowledged: bool = True


def _require_access(user: CurrentUser) -> None:
    require_feature(user.org_id, "SELLER_INTELLIGENCE")


@router.get("")
@limiter.limit("30/minute")
async def seller_intelligence(
    request: Request,
    user: CurrentUser,
    scope: str = Query("", pattern="^$|^(organization|team|mine)$"),
    limit: int = Query(60, ge=1, le=200),
    cursor: str = Query("0", pattern=r"^\d+$"),
):
    _require_access(user)
    return await build_snapshot(user.org_id, user.user_id, user.role, scope, limit=limit, cursor=cursor)


@router.get("/preferences")
@limiter.limit("60/minute")
async def seller_preferences(request: Request, user: CurrentUser):
    _require_access(user)
    return {"preferences": await get_preferences(user.org_id, user.user_id, user.role)}


@router.patch("/preferences")
@limiter.limit("20/minute")
async def update_seller_preferences(request: Request, payload: PreferencesPayload, user: CurrentUser):
    _require_access(user)
    return {"preferences": await save_preferences(user.org_id, user.user_id, user.role, payload.model_dump())}


@router.get("/items/{item_id:path}")
@limiter.limit("60/minute")
async def seller_item(request: Request, item_id: str, user: CurrentUser):
    _require_access(user)
    return {"item": await get_item(user.org_id, user.user_id, user.role, item_id)}


@router.patch("/items/{item_id:path}")
@limiter.limit("30/minute")
async def update_seller_item(request: Request, item_id: str, payload: ItemStatePayload, user: CurrentUser):
    _require_access(user)
    return await update_item_state(
        user.org_id, user.user_id, user.role, item_id, payload.action, payload.snooze_until,
    )
