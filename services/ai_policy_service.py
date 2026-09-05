from datetime import datetime, timedelta, timezone
import asyncio
import logging
from typing import Literal

from fastapi import HTTPException
from pymongo import ReturnDocument

from database import get_async_mongo_db
from services.utils import serialize_mongo
from services.websocket_service import broadcast_org_ws
from services.whatsapp_connection_service import MAX_WHATSAPP_CONNECTIONS


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


async def _active_whatsapp_phone_ids(org_id: str) -> list[str]:
    rows = await db.whatsapp_connections.find(
        {"org_id": org_id, "is_active": {"$ne": False}},
        {"_id": 0, "phone_id": 1, "phone_number_id": 1, "slot": 1},
    ).sort([("slot", 1), ("phone_id", 1)]).to_list(length=MAX_WHATSAPP_CONNECTIONS)
    return [
        str(row.get("phone_id") or row.get("phone_number_id") or "").strip()
        for row in rows
        if row.get("phone_id") or row.get("phone_number_id")
    ]


def _disabled_whatsapp_phone_ids(organization: dict) -> set[str]:
    values = organization.get("ai_disabled_whatsapp_phone_ids")
    return {str(value) for value in values} if isinstance(values, list) else set()


async def _conversation_whatsapp_phone_id(org_id: str, conversation_id: str, conversation: dict) -> str | None:
    if conversation.get("platform") != "whatsapp" and not conversation_id.startswith("whatsapp_"):
        return None
    phone_id = str(conversation.get("business_phone_id") or "").strip()
    if phone_id:
        return phone_id
    primary = await db.whatsapp_connections.find_one(
        {"org_id": org_id, "is_active": {"$ne": False}, "slot": 1},
        {"_id": 0, "phone_id": 1, "phone_number_id": 1},
    )
    return str((primary or {}).get("phone_id") or (primary or {}).get("phone_number_id") or "").strip() or None


async def get_org_ai_policy(org_id: str) -> dict:
    organization = await db.organizations.find_one(
        {"org_id": org_id},
        {
            "_id": 0,
            "is_ai_enabled": 1,
            "ai_pause_minutes": 1,
            "quote_followup_prompt_enabled": 1,
            "ai_disabled_whatsapp_phone_ids": 1,
        },
    )
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found.")
    active_phone_ids = await _active_whatsapp_phone_ids(org_id)
    disabled_phone_ids = _disabled_whatsapp_phone_ids(organization)
    return {
        "enabled": bool(organization.get("is_ai_enabled", False)),
        "pause_minutes": _pause_minutes(organization.get("ai_pause_minutes")),
        "quote_followup_prompt_enabled": organization.get("quote_followup_prompt_enabled") is not False,
        "whatsapp_enabled_phone_ids": [phone_id for phone_id in active_phone_ids if phone_id not in disabled_phone_ids],
    }


async def update_org_ai_policy(
    org_id: str,
    enabled: bool,
    pause_minutes: int,
    quote_followup_prompt_enabled: bool | None = None,
    whatsapp_enabled_phone_ids: list[str] | None = None,
) -> dict:
    pause_minutes = _pause_minutes(pause_minutes)
    fields = {
        "is_ai_enabled": enabled,
        "ai_pause_minutes": pause_minutes,
        "ai_policy_updated_at": datetime.now(timezone.utc),
    }
    if quote_followup_prompt_enabled is not None:
        fields["quote_followup_prompt_enabled"] = quote_followup_prompt_enabled
    if whatsapp_enabled_phone_ids is not None:
        requested_phone_ids = [str(value).strip() for value in whatsapp_enabled_phone_ids]
        if (
            any(not value or len(value) > 128 for value in requested_phone_ids)
            or len(requested_phone_ids) != len(set(requested_phone_ids))
        ):
            raise HTTPException(status_code=422, detail="WhatsApp phone IDs must be non-empty and unique.")
        active_phone_ids = await _active_whatsapp_phone_ids(org_id)
        unknown_phone_ids = set(requested_phone_ids) - set(active_phone_ids)
        if unknown_phone_ids:
            raise HTTPException(
                status_code=422,
                detail="One or more WhatsApp numbers are not active in this organization.",
            )
        requested = set(requested_phone_ids)
        organization = await db.organizations.find_one(
            {"org_id": org_id},
            {"_id": 0, "ai_disabled_whatsapp_phone_ids": 1},
        )
        inactive_disabled = _disabled_whatsapp_phone_ids(organization or {}) - set(active_phone_ids)
        fields["ai_disabled_whatsapp_phone_ids"] = sorted(
            inactive_disabled | (set(active_phone_ids) - requested)
        )
    result = await db.organizations.update_one(
        {"org_id": org_id},
        {"$set": fields},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return await get_org_ai_policy(org_id)


async def ai_access_state(org_id: str, conversation_id: str) -> str:
    try:
        from services.entitlement_service import quota_status

        quota = await asyncio.to_thread(quota_status, org_id, "AI", "monthly_messages")
        if quota["remaining"] == 0:
            return "quota_exhausted"
        organization = await db.organizations.find_one(
            {"org_id": org_id},
            {"_id": 0, "is_ai_enabled": 1, "ai_disabled_whatsapp_phone_ids": 1},
        )
        if not organization or not organization.get("is_ai_enabled", False):
            return "organization_disabled"
        conversation = await db[f"conversations_{org_id}"].find_one(
            {"conversation_id": conversation_id},
            {
                "_id": 0,
                "platform": 1,
                "business_phone_id": 1,
                "is_ai_enabled": 1,
                "auto_ai_disabled": 1,
                "auto_ai_disabled_until": 1,
            },
        )
        if not conversation:
            return "conversation_disabled"
        phone_id = await _conversation_whatsapp_phone_id(org_id, conversation_id, conversation)
        if (conversation.get("platform") == "whatsapp" or conversation_id.startswith("whatsapp_")) and (
            not phone_id or phone_id in _disabled_whatsapp_phone_ids(organization)
        ):
            return "number_disabled"
        if not conversation.get("is_ai_enabled", True):
            return "conversation_disabled"
        paused_until = conversation.get("auto_ai_disabled_until")
        if conversation.get("auto_ai_disabled") and paused_until:
            if paused_until.tzinfo is None:
                paused_until = paused_until.replace(tzinfo=timezone.utc)
            return "active" if paused_until <= datetime.now(timezone.utc) else "conversation_paused"
        return "active" if not conversation.get("auto_ai_disabled", False) else "conversation_paused"
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return "commercial_unavailable" if detail.get("code") == "feature_unavailable" else "error"
    except Exception:
        logger.exception(
            "AI policy check failed closed org=%s conversation=...%s",
            org_id,
            conversation_id[-6:],
        )
        return "error"


async def is_effective_ai_enabled(org_id: str, conversation_id: str) -> bool:
    return await ai_access_state(org_id, conversation_id) == "active"


async def set_conversation_ai_state(
    org_id: str,
    conversation_id: str,
    action: Literal["pause", "resume"],
    duration_minutes: int | None = None,
) -> dict:
    if not conversation_id.startswith(("whatsapp_", "instagram_")):
        raise HTTPException(status_code=400, detail="AI controls are available only for customer conversations.")
    existing_conversation = await db[f"conversations_{org_id}"].find_one(
        {"conversation_id": conversation_id},
        {"_id": 0, "platform": 1, "business_phone_id": 1},
    )
    if not existing_conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    policy = await get_org_ai_policy(org_id)
    if action == "resume" and not policy["enabled"]:
        raise HTTPException(
            status_code=409,
            detail="AI is disabled for this organization. An admin must turn it on in Settings.",
        )
    if action == "resume" and (
        existing_conversation.get("platform") == "whatsapp" or conversation_id.startswith("whatsapp_")
    ):
        phone_id = await _conversation_whatsapp_phone_id(org_id, conversation_id, existing_conversation)
        if not phone_id or phone_id not in policy["whatsapp_enabled_phone_ids"]:
            raise HTTPException(
                status_code=409,
                detail="AI is disabled for this WhatsApp number. An admin must enable it in AI & Handover.",
            )

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
