from datetime import datetime, timezone
from typing import Optional
import uuid
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.utils import serialize_mongo
from services.websocket_service import broadcast_org_ws, send_to_user


TEAM_TYPES = {"sales", "billing", "support", "general"}
ACTIVE_WORK_STATUSES = {"pending", "in_progress", "needs_review"}
ADMIN_ROLES = {"admin", "org:admin"}
ACTIVE_TODAY_TIMEZONE = ZoneInfo("Asia/Kolkata")


def is_admin(role: str) -> bool:
    return role in ADMIN_ROLES


def can_user_access_conversation(user_id: str, role: str, conversation: dict, teams: list[dict]) -> bool:
    if is_admin(role) or not teams:
        return True
    member_teams = [team for team in teams if user_id in team.get("member_ids", [])]
    if any(team.get("type") == "sales" for team in member_teams):
        return True
    team_ids = {team["team_id"] for team in member_teams}
    return any(
        item.get("team_id") in team_ids and item.get("status") in ACTIVE_WORK_STATUSES
        for item in conversation.get("active_work", [])
    )


async def can_claim_conversation_ownership(org_id: str, user_id: str, role: str) -> bool:
    teams = await list_work_teams(org_id)
    return not teams or any(
        team.get("type") == "sales" and user_id in team.get("member_ids", [])
        for team in teams
    )


async def _restore_active_conversation_work(org_id: str, collection, work_id: str) -> None:
    latest = await collection.find_one({"work_id": work_id})
    if not latest or latest.get("status") not in ACTIVE_WORK_STATUSES:
        return
    conversations = get_async_mongo_db()[f"conversations_{org_id}"]
    conversation = await conversations.find_one(
        {"conversation_id": latest["conversation_id"]}, {"_id": 1}
    )
    if conversation:
        await _attach_conversation_work(org_id, conversation["_id"], latest)


async def record_team_presence(org_id: str, user_id: str) -> None:
    if not user_id:
        return
    now = datetime.now(timezone.utc)
    collection = get_async_mongo_db()[f"work_team_presence_{org_id}"]
    await collection.create_index("user_id", unique=True)
    await collection.create_index("last_active_at", expireAfterSeconds=31 * 24 * 60 * 60)
    await collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "active_date": now.astimezone(ACTIVE_TODAY_TIMEZONE).date().isoformat(),
            "last_active_at": now,
        }},
        upsert=True,
    )


async def list_work_teams(org_id: str) -> list[dict]:
    rows = await get_async_mongo_db()[f"work_teams_{org_id}"].find(
        {}, {"_id": 0, "active": 0}
    ).sort([("type", 1), ("name", 1)]).to_list(length=100)
    today = datetime.now(timezone.utc).astimezone(ACTIVE_TODAY_TIMEZONE).date().isoformat()
    active_ids = {
        row["user_id"]
        for row in await get_async_mongo_db()[f"work_team_presence_{org_id}"].find(
            {"active_date": today}, {"_id": 0, "user_id": 1}
        ).to_list(length=1000)
        if row.get("user_id")
    }
    for team in rows:
        team["active_today_member_ids"] = [
            member_id for member_id in team.get("member_ids", []) if member_id in active_ids
        ]
        team["active_today_count"] = len(team["active_today_member_ids"])
    return serialize_mongo(rows)


async def conversation_access_condition(org_id: str, user_id: str, role: str) -> dict:
    if is_admin(role):
        return {}
    teams = await list_work_teams(org_id)
    if not teams:  # Preserve existing organizations until they configure departments.
        return {}
    member_teams = [team for team in teams if user_id in team.get("member_ids", [])]
    if any(team.get("type") == "sales" for team in member_teams):
        return {}
    team_ids = [team["team_id"] for team in member_teams]
    access = []
    if team_ids:
        access.append({"active_work": {"$elemMatch": {
                "team_id": {"$in": team_ids},
                "status": {"$in": list(ACTIVE_WORK_STATUSES)},
            }}})
    return {"$or": access} if access else {"_id": {"$exists": False}}


async def require_conversation_access(org_id: str, conversation_id: str, user_id: str, role: str) -> dict:
    access = await conversation_access_condition(org_id, user_id, role)
    query = {"conversation_id": conversation_id}
    if access:
        query = {"$and": [query, access]}
    conversation = await get_async_mongo_db()[f"conversations_{org_id}"].find_one(query)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found or not assigned to your team")
    return conversation


async def require_team(org_id: str, team_id: str) -> dict:
    team = await get_async_mongo_db()[f"work_teams_{org_id}"].find_one(
        {"team_id": team_id}, {"_id": 0, "active": 0}
    )
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


async def team_queue_condition(org_id: str, team_id: str, user_id: str, role: str) -> dict:
    team = await require_team(org_id, team_id)
    if not is_admin(role) and user_id not in team.get("member_ids", []):
        raise HTTPException(status_code=403, detail="You are not a member of this team")
    return {"active_work": {"$elemMatch": {
            "team_id": team_id,
            "status": {"$in": list(ACTIVE_WORK_STATUSES)},
        }}}


async def save_work_team(org_id: str, payload: dict, team_id: Optional[str] = None) -> dict:
    if payload.get("type") not in TEAM_TYPES:
        raise HTTPException(status_code=422, detail="Invalid team type")
    now = datetime.now(timezone.utc)
    collection = get_async_mongo_db()[f"work_teams_{org_id}"]
    await collection.create_index("team_id", unique=True)
    await collection.create_index("member_ids")
    conversations = get_async_mongo_db()[f"conversations_{org_id}"]
    await conversations.create_index("assigned_agent_id")
    await conversations.create_index("active_work.team_id")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Team name is required")
    document = {
        "name": name,
        "type": payload["type"],
        "member_ids": list(dict.fromkeys(payload.get("member_ids") or [])),
        "is_default_inbox_team": bool(payload.get("is_default_inbox_team")),
        "updated_at": now,
    }
    team_id = team_id or str(uuid.uuid4())
    if document["is_default_inbox_team"]:
        await collection.update_many(
            {"team_id": {"$ne": team_id}},
            {"$set": {"is_default_inbox_team": False, "updated_at": now}},
        )
    team = await collection.find_one_and_update(
        {"team_id": team_id},
        {"$set": document, "$unset": {"active": ""}, "$setOnInsert": {"team_id": team_id, "created_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    return serialize_mongo(team)


async def create_conversation_work(
    org_id: str,
    conversation_id: str,
    user_id: str,
    user_name: str,
    role: str,
    payload: dict,
) -> tuple[dict, bool]:
    conversation = await require_conversation_access(org_id, conversation_id, user_id, role)
    team = await require_team(org_id, payload["team_id"])
    assigned_agent_id = payload.get("assigned_agent_id")
    if assigned_agent_id and assigned_agent_id not in team.get("member_ids", []):
        raise HTTPException(status_code=422, detail="That team member is not in the selected team")
    work_type = payload.get("type") or ("pricing" if team.get("type") == "sales" else team.get("type"))
    if work_type not in {"pricing", "billing", "support", "general"}:
        raise HTTPException(status_code=422, detail="Invalid work type")
    quote_id = payload.get("quote_id")
    open_key = ":".join([conversation_id, team["team_id"], work_type])
    collection = get_async_mongo_db()[f"conversation_work_{org_id}"]
    await collection.create_index("work_id", unique=True)
    await collection.create_index("open_key", unique=True, sparse=True)
    existing = await collection.find_one({"open_key": open_key}, {"_id": 0})
    if existing:
        existing = await _assign_open_work(
            org_id, conversation["_id"], collection, existing,
            assigned_agent_id, payload.get("assigned_agent_name"),
        )
        existing = await _update_open_work_context(collection, existing, payload)
        await _attach_conversation_work(org_id, conversation["_id"], existing)
        await broadcast_org_ws(org_id, "conversation_work_updated", {
            "conversation_id": conversation_id,
            "team_id": team["team_id"],
            "work_id": existing["work_id"],
            "status": existing["status"],
        })
        return serialize_mongo(existing), False
    now = datetime.now(timezone.utc)
    work = {
        "work_id": str(uuid.uuid4()),
        "open_key": open_key,
        "conversation_id": conversation_id,
        "type": work_type,
        "team_id": team["team_id"],
        "team_name": team["name"],
        "assigned_agent_id": assigned_agent_id,
        "assigned_agent_name": payload.get("assigned_agent_name") if assigned_agent_id else None,
        "status": "pending",
        "quote_id": quote_id,
        "quote_revision": payload.get("quote_revision"),
        "note": (payload.get("note") or "").strip() or None,
        "referenced_message_ids": list(payload.get("referenced_message_ids") or []),
        "result": None,
        "invoice_reference": None,
        "version": 1,
        "created_by": user_id,
        "created_by_name": user_name,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await collection.insert_one(work)
    except DuplicateKeyError:
        existing = await collection.find_one({"open_key": open_key}, {"_id": 0})
        if not existing:
            raise
        existing = await _assign_open_work(
            org_id, conversation["_id"], collection, existing,
            assigned_agent_id, payload.get("assigned_agent_name"),
        )
        existing = await _update_open_work_context(collection, existing, payload)
        await _attach_conversation_work(org_id, conversation["_id"], existing)
        await broadcast_org_ws(org_id, "conversation_work_updated", {
            "conversation_id": conversation_id,
            "team_id": team["team_id"],
            "work_id": existing["work_id"],
            "status": existing["status"],
        })
        return serialize_mongo(existing), False
    await _attach_conversation_work(org_id, conversation["_id"], work)
    await broadcast_org_ws(org_id, "conversation_work_updated", {
        "conversation_id": conversation_id,
        "team_id": team["team_id"],
        "work_id": work["work_id"],
        "status": "pending",
    })
    return serialize_mongo(work), True


async def _update_open_work_context(collection, work: dict, payload: dict) -> dict:
    updated = await collection.find_one_and_update(
        {"work_id": work["work_id"], "version": work.get("version", 1)},
        {
            "$set": {
                "note": (payload.get("note") or "").strip() or None,
                "referenced_message_ids": list(payload.get("referenced_message_ids") or []),
                "updated_at": datetime.now(timezone.utc),
            },
            "$inc": {"version": 1},
        },
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    return updated or work


async def _assign_open_work(
    org_id: str, conversation_oid, collection, work: dict,
    assigned_agent_id: Optional[str], assigned_agent_name: Optional[str],
) -> dict:
    if not assigned_agent_id:
        return work
    if work.get("assigned_agent_id") == assigned_agent_id:
        return work
    if work.get("assigned_agent_id"):
        raise HTTPException(status_code=409, detail="This work is already assigned to another team member")
    updated = await collection.find_one_and_update(
        {"work_id": work["work_id"], "assigned_agent_id": None, "version": work["version"]},
        {"$set": {
            "assigned_agent_id": assigned_agent_id,
            "assigned_agent_name": assigned_agent_name,
            "updated_at": datetime.now(timezone.utc),
        }, "$inc": {"version": 1}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This work was assigned to someone else. Refresh and try again")
    await get_async_mongo_db()[f"conversations_{org_id}"].update_one(
        {"_id": conversation_oid, "active_work.work_id": work["work_id"]},
        {"$set": {
            "active_work.$.assigned_agent_id": assigned_agent_id,
            "active_work.$.assigned_agent_name": assigned_agent_name,
            "active_work.$.version": updated["version"],
            "active_work.$.updated_at": updated["updated_at"],
        }},
    )
    return updated


async def _attach_conversation_work(org_id: str, conversation_id, work: dict) -> None:
    embedded = {key: value for key, value in work.items() if key not in {"_id", "open_key", "created_by"}}
    conversations = get_async_mongo_db()[f"conversations_{org_id}"]
    updated = await conversations.update_one(
        {"_id": conversation_id, "active_work.work_id": work["work_id"]},
        {"$set": {"active_work.$": embedded}},
    )
    if updated.matched_count:
        return
    await conversations.update_one(
        {"_id": conversation_id, "active_work.work_id": {"$ne": work["work_id"]}},
        {"$push": {"active_work": embedded}},
    )


async def list_conversation_work(org_id: str, conversation_id: str, user_id: str, role: str) -> list[dict]:
    await require_conversation_access(org_id, conversation_id, user_id, role)
    rows = await get_async_mongo_db()[f"conversation_work_{org_id}"].find(
        {"conversation_id": conversation_id}, {"_id": 0, "open_key": 0}
    ).sort("created_at", -1).to_list(length=200)
    return serialize_mongo(rows)


async def update_conversation_work(
    org_id: str, work_id: str, user_id: str, user_name: str, role: str, payload: dict
) -> dict:
    collection = get_async_mongo_db()[f"conversation_work_{org_id}"]
    work = await collection.find_one({"work_id": work_id})
    if not work:
        raise HTTPException(status_code=404, detail="Work item not found")
    team = await require_team(org_id, work["team_id"])
    version = int(payload.get("version") or 0)
    status = payload.get("status")
    if status not in ACTIVE_WORK_STATUSES | {"completed", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid work status")
    assigned_agent_id = work.get("assigned_agent_id")
    team_member = user_id in team.get("member_ids", [])
    can_cancel = user_id == work.get("created_by") and status == "cancelled"
    if not is_admin(role) and not can_cancel and (
        not team_member or (assigned_agent_id and assigned_agent_id != user_id)
    ):
        raise HTTPException(status_code=403, detail="This work is assigned to another team member")
    transitions = {
        "pending": {"in_progress", "cancelled"},
        "needs_review": {"in_progress", "cancelled"},
        "in_progress": {"needs_review", "completed", "cancelled"},
    }
    if status not in transitions.get(work.get("status"), set()):
        raise HTTPException(status_code=409, detail="This work status has already changed")
    now = datetime.now(timezone.utc)
    updates = {
        "status": status,
        "updated_at": now,
        "result": payload.get("result"),
        "invoice_reference": payload.get("invoice_reference"),
    }
    if status == "in_progress" and not assigned_agent_id:
        updates["assigned_agent_id"] = user_id
        updates["assigned_agent_name"] = user_name
    update = {"$set": updates, "$inc": {"version": 1}}
    terminal = status in {"completed", "cancelled"}
    conversations = get_async_mongo_db()[f"conversations_{org_id}"]
    if terminal:
        update["$unset"] = {"open_key": ""}
        # Remove temporary access first. A crash here fails closed; a retry can
        # still finish the authoritative work transition.
        await conversations.update_one(
            {"conversation_id": work["conversation_id"]},
            {"$pull": {"active_work": {"work_id": work_id}}},
        )
    try:
        updated = await collection.find_one_and_update(
            {"work_id": work_id, "version": version},
            update,
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0, "open_key": 0},
        )
    except Exception:
        if terminal:
            await _restore_active_conversation_work(org_id, collection, work_id)
        raise
    if not updated:
        if terminal:
            await _restore_active_conversation_work(org_id, collection, work_id)
        raise HTTPException(status_code=409, detail="Work changed. Refresh and try again")
    if not terminal:
        embedded_result = await conversations.update_one(
            {"conversation_id": work["conversation_id"], "active_work.work_id": work_id},
            {"$set": {
                "active_work.$.status": status,
                "active_work.$.updated_at": now,
                "active_work.$.version": updated["version"],
                "active_work.$.result": payload.get("result"),
                "active_work.$.invoice_reference": payload.get("invoice_reference"),
                "active_work.$.assigned_agent_id": updated.get("assigned_agent_id"),
                "active_work.$.assigned_agent_name": updated.get("assigned_agent_name"),
            }},
        )
        if not embedded_result.matched_count:
            conversation = await conversations.find_one(
                {"conversation_id": work["conversation_id"]}, {"_id": 1}
            )
            if conversation:
                await _attach_conversation_work(org_id, conversation["_id"], updated)
    event = {
        "conversation_id": work["conversation_id"],
        "team_id": work["team_id"],
        "work_id": work_id,
        "status": status,
    }
    await broadcast_org_ws(org_id, "conversation_work_updated", event)
    if terminal:
        for member_id in team.get("member_ids", []):
            await send_to_user(
                member_id, {"type": "conversation_work_updated", **event}, org_id=org_id
            )
    return serialize_mongo(updated)
