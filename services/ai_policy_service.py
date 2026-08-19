from datetime import datetime, timedelta, timezone
import logging
from typing import Literal

from fastapi import HTTPException
from pymongo import ReturnDocument

from database import get_async_mongo_db
from services.utils import serialize_mongo
from services.websocket_service import broadcast_org_ws


db = get_async_mongo_db()
logger = logging.getLogger(__name__)
DEFAULT_PAUSE_MINUTES = 60
MIN_PAUSE_MINUTES = 5
MAX_PAUSE_MINUTES = 24 * 60


def _pause_minutes(value: int | None) -> int:
    minutes = DEFAULT_PAUSE_MINUTES if value is None else int(value)
    if not MIN_PAUSE_MINUTES <= minutes <= MAX_PAUSE_MINUTES:
        raise HTTPException(status_code=422, detail="AI pause must be between 5 minutes and 24 hours.")
    return minutes


async def get_org_ai_policy(org_id: str) -> dict:
    organization = await db.organizations.find_one(
        {"org_id": org_id},
        {"_id": 0, "is_ai_enabled": 1, "ai_pause_minutes": 1},
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return {
        "enabled": bool(organization.get("is_ai_enabled", False)),
        "pause_minutes": _pause_minutes(organization.get("ai_pause_minutes")),
    }


async def update_org_ai_policy(org_id: str, enabled: bool, pause_minutes: int) -> dict:
    pause_minutes = _pause_minutes(pause_minutes)
    result = await db.organizations.update_one(
        {"org_id": org_id},
        {"$set": {
            "is_ai_enabled": enabled,
            "ai_pause_minutes": pause_minutes,
            "ai_policy_updated_at": datetime.now(timezone.utc),
        }},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return {"enabled": enabled, "pause_minutes": pause_minutes}


async def is_effective_ai_enabled(org_id: str, conversation_id: str) -> bool:
    try:
        organization = await db.organizations.find_one(
            {"org_id": org_id},
            {"_id": 0, "is_ai_enabled": 1},
        )
        if not organization or not organization.get("is_ai_enabled", False):
            return False
        conversation = await db[f"conversations_{org_id}"].find_one(
            {"conversation_id": conversation_id},
            {"_id": 0, "is_ai_enabled": 1, "auto_ai_disabled": 1, "auto_ai_disabled_until": 1},
        )
        if not conversation or not conversation.get("is_ai_enabled", True):
            return False
        paused_until = conversation.get("auto_ai_disabled_until")
        if conversation.get("auto_ai_disabled") and paused_until:
            if paused_until.tzinfo is None:
                paused_until = paused_until.replace(tzinfo=timezone.utc)
            return paused_until <= datetime.now(timezone.utc)
        return not conversation.get("auto_ai_disabled", False)
    except Exception:
        logger.exception(
            "AI policy check failed closed org=%s conversation=...%s",
            org_id,
            conversation_id[-6:],
        )
        return False


async def set_conversation_ai_state(
    org_id: str,
    conversation_id: str,
    action: Literal["pause", "resume"],
    duration_minutes: int | None = None,
) -> dict:
    if not conversation_id.startswith(("whatsapp_", "instagram_")):
        raise HTTPException(status_code=400, detail="AI controls are available only for customer conversations.")
    policy = await get_org_ai_policy(org_id)
    if action == "resume" and not policy["enabled"]:
        raise HTTPException(status_code=409, detail="AI is disabled for this organization. An admin must turn it on in Settings.")

    now = datetime.now(timezone.utc)
    if action == "pause":
        minutes = _pause_minutes(duration_minutes if duration_minutes is not None else policy["pause_minutes"])
        update = {"$set": {
            "is_ai_enabled": False,
            "auto_ai_disabled": True,
            "auto_ai_disabled_until": now + timedelta(minutes=minutes),
            "ai_state_updated_at": now,
        }}
    else:
        minutes = 0
        update = {"$set": {
            "is_ai_enabled": True,
            "auto_ai_disabled": False,
            "auto_ai_disabled_until": None,
            "ai_state_updated_at": now,
        }}

    conversation = await db[f"conversations_{org_id}"].find_one_and_update(
        {
            "conversation_id": conversation_id,
            "$or": [
                {"platform": {"$in": ["whatsapp", "instagram"]}},
                {"platform": {"$exists": False}},
            ],
        },
        update,
        return_document=ReturnDocument.AFTER,
    )
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    serialized = serialize_mongo(conversation)
    await broadcast_org_ws(org_id, "conversation_updated", {"conversation": serialized})
    return {
        "status": "success",
        "action": action,
        "pause_minutes": minutes or None,
        "conversation": serialized,
    }


async def resume_expired_ai_pauses(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    resumed = 0
    organizations = db.organizations.find({"is_ai_enabled": True}, {"org_id": 1})
    async for organization in organizations:
        org_id = organization.get("org_id")
        if not org_id:
            continue
        collection = db[f"conversations_{org_id}"]
        cursor = collection.find(
            {"auto_ai_disabled": True, "auto_ai_disabled_until": {"$lte": now}},
            {"conversation_id": 1},
        )
        async for item in cursor:
            conversation = await collection.find_one_and_update(
                {
                    "conversation_id": item["conversation_id"],
                    "auto_ai_disabled": True,
                    "auto_ai_disabled_until": {"$lte": now},
                },
                {"$set": {
                    "is_ai_enabled": True,
                    "auto_ai_disabled": False,
                    "auto_ai_disabled_until": None,
                    "ai_state_updated_at": now,
                }},
                return_document=ReturnDocument.AFTER,
            )
            if conversation:
                resumed += 1
                await broadcast_org_ws(
                    org_id,
                    "conversation_updated",
                    {"conversation": serialize_mongo(conversation)},
                )
    return resumed
