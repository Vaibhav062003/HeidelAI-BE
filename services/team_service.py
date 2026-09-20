"""
team_service.py

Dedicated service for team (internal) conversations and messages.
Reads from isolated collections:
  - team_conversations_{org_id}
  - team_messages_{org_id}

Sending team messages is still handled by common_service.send_message
(platform="internal"), which writes into these same collections.
Room and message changes are broadcast through the shared team scope.
"""

import asyncio
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

from fastapi import HTTPException
from loguru import logger

from core.services import services
from database import get_mongo_db, get_async_mongo_db
from services.utils import serialize_mongo


db = get_mongo_db()
ESCALATION_RETENTION = timedelta(days=90)
_escalation_indexes_ready: set[str] = set()
EVERYONE_ROOM_ID = "team_room_everyone"
ANNOUNCEMENTS_ROOM_ID = "team_room_announcements"
SYSTEM_ROOMS = {
    EVERYONE_ROOM_ID: ("everyone", "Everyone"),
    ANNOUNCEMENTS_ROOM_ID: ("announcements", "Announcements"),
}


def resolve_team_scope(org_id: str) -> str:
    """Use the shared workspace group for team chat, or the org for legacy tenants."""
    organization = db.organizations.find_one(
        {"org_id": org_id}, {"workspace_group_id": 1, "_id": 0}
    ) or {}
    return str(organization.get("workspace_group_id") or org_id)


def _group_organizations(org_id: str) -> tuple[str, list[dict], dict]:
    current = db.organizations.find_one(
        {"org_id": org_id},
        {"org_id": 1, "org_name": 1, "workspace_group_id": 1, "workspace_kind": 1, "_id": 0},
    ) or {"org_id": org_id, "org_name": org_id}
    scope = str(current.get("workspace_group_id") or org_id)
    if not current.get("workspace_group_id"):
        return scope, [current], current
    organizations = list(db.organizations.find(
        {"workspace_group_id": scope},
        {"org_id": 1, "org_name": 1, "workspace_kind": 1, "_id": 0},
    ))
    return scope, organizations or [current], current


def _membership_created_at(membership: dict) -> datetime:
    value = membership.get("created_at") or membership.get("createdAt")
    try:
        if isinstance(value, datetime):
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), timezone.utc)
        if isinstance(value, str) and value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        pass
    return datetime.now(timezone.utc)


async def _group_member_records(org_id: str) -> tuple[str, dict, dict[str, dict], set[str]]:
    """Return active team-scope members and the admins allowed to manage rooms."""
    from api.transfer_routes import get_org_memberships, membership_public_data

    scope, organizations, current = _group_organizations(org_id)
    membership_lists = await asyncio.gather(*(
        get_org_memberships(str(organization["org_id"])) for organization in organizations
    ))
    is_grouped = bool(current.get("workspace_group_id"))
    child_ids = {
        str(organization["org_id"])
        for organization in organizations
        if is_grouped and organization.get("workspace_kind") != "management"
    }
    members: dict[str, dict] = {}
    room_admin_ids: set[str] = set()
    for organization, memberships in zip(organizations, membership_lists):
        workspace_id = str(organization["org_id"])
        workspace_name = str(organization.get("org_name") or workspace_id)
        for membership in memberships:
            public = membership_public_data(membership)
            user_id = str(public.get("user_id") or "")
            if not user_id or public.get("banned"):
                continue
            member = members.setdefault(user_id, {
                "userId": user_id,
                "name": " ".join(filter(None, [public.get("first_name"), public.get("last_name")]))
                    or public.get("identifier") or "Unknown",
                "email": public.get("identifier") or "",
                "avatarUrl": public.get("image_url"),
                "workspaceNames": [],
                "_workspaceIds": set(),
                "_joinedAt": _membership_created_at(membership),
            })
            member["_joinedAt"] = min(member["_joinedAt"], _membership_created_at(membership))
            role = str(membership.get("role") or "").removeprefix("org:").lower()
            manages_scope = (
                organization.get("workspace_kind") == "management"
                if is_grouped else workspace_id == org_id
            )
            if manages_scope and role == "admin":
                room_admin_ids.add(user_id)
            if is_grouped and organization.get("workspace_kind") != "management":
                member["workspaceNames"].append(workspace_name)
                member["_workspaceIds"].add(workspace_id)

    for member in members.values():
        workspace_ids = member["_workspaceIds"]
        member["workspaceNames"] = sorted(set(member["workspaceNames"]))
        member["allWorkspaces"] = is_grouped and bool(child_ids) and workspace_ids >= child_ids
    return scope, current, members, room_admin_ids


async def fetch_team_directory(org_id: str, current_user_id: str | None = None) -> Dict:
    """Return active Clerk members across every workspace in this group."""
    scope, current, members, room_admin_ids = await _group_member_records(org_id)

    result = []
    for member in members.values():
        public_member = {key: value for key, value in member.items() if not key.startswith("_")}
        result.append(public_member)
    result.sort(key=lambda member: (member["name"].casefold(), member["email"].casefold()))
    return {
        "team_scope_id": scope,
        "workspace_kind": current.get("workspace_kind") or "standalone",
        "canManageRooms": bool(current_user_id and current_user_id in room_admin_ids),
        "members": result,
    }


async def require_team_recipient(org_id: str, sender_id: str, recipient_id: str) -> tuple[str, dict]:
    if not recipient_id or recipient_id == sender_id:
        raise HTTPException(status_code=422, detail="Choose another active team member")
    directory = await fetch_team_directory(org_id)
    by_id = {member["userId"]: member for member in directory["members"]}
    if sender_id not in by_id or recipient_id not in by_id:
        raise HTTPException(status_code=403, detail="The recipient is not an active team member")
    return str(directory["team_scope_id"]), by_id[recipient_id]


def _room_name(value: str) -> str:
    name = " ".join((value or "").split())
    if not 1 <= len(name) <= 80:
        raise HTTPException(status_code=422, detail="Room name must be 1-80 characters")
    return name


def _room_response(
    doc: dict, current_user_id: str | None = None, can_manage_rooms: bool = False
) -> dict:
    participants = list(doc.get("participants") or [])
    is_participant = bool(current_user_id and current_user_id in participants)
    timestamp = doc.get("last_message_timestamp")
    return {
        "id": doc.get("conversation_id"),
        "customer_name": doc.get("name") or doc.get("customer_name") or doc.get("conversation_id"),
        "name": doc.get("name"),
        "last_message": doc.get("last_message", "") if is_participant else "",
        "timestamp": (serialize_mongo(timestamp) if isinstance(timestamp, datetime) else timestamp) if is_participant else None,
        "unread_count": (doc.get("unread") or {}).get(current_user_id, 0) if is_participant else 0,
        "platform": "internal",
        "type": "team",
        "chat_type": doc.get("chat_type") or "direct",
        "participants": participants,
        "member_count": len(participants),
        "is_participant": is_participant,
        "can_post": is_participant and (
            doc.get("chat_type") != "announcements" or can_manage_rooms
        ),
        "is_online": False,
    }


async def _ensure_system_rooms(org_id: str) -> tuple[str, dict[str, dict], dict[str, dict], set[str]]:
    scope, _, members, admin_ids = await _group_member_records(org_id)
    collection = get_async_mongo_db()[f"team_conversations_{scope}"]
    participant_ids = sorted(members)
    rooms: dict[str, dict] = {}
    for room_id, (chat_type, name) in SYSTEM_ROOMS.items():
        existing = await collection.find_one({"conversation_id": room_id}, {"_id": 0}) or {}
        unread = {
            user_id: int((existing.get("unread") or {}).get(user_id, 0))
            for user_id in participant_ids
        }
        existing_joined_at = existing.get("joined_at") or {}
        joined_at = {
            user_id: existing_joined_at.get(user_id, member["_joinedAt"])
            for user_id, member in members.items()
        }
        room_update: dict = {"$set": {"participants": participant_ids, "joined_at": joined_at}}
        if not existing:
            room_update["$setOnInsert"] = {
                "conversation_id": room_id,
                "type": "team",
                "chat_type": chat_type,
                "name": name,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "last_message": "",
                "unread": {},
            }
        removed_unread = set(existing.get("unread") or {}) - set(participant_ids)
        if removed_unread:
            room_update["$unset"] = {f"unread.{user_id}": "" for user_id in removed_unread}
        await collection.update_one({"conversation_id": room_id}, room_update, upsert=True)
        rooms[room_id] = {
            **existing,
            "conversation_id": room_id,
            "type": "team",
            "chat_type": chat_type,
            "name": name,
            "participants": participant_ids,
            "joined_at": joined_at,
            "unread": unread,
        }
    return scope, rooms, members, admin_ids


async def require_team_room(
    org_id: str, user_id: str, conversation_id: str, for_post: bool = False
) -> tuple[str, dict] | None:
    """Return a room with active participants, or None when the ID is a direct chat."""
    scope = resolve_team_scope(org_id)
    collection = get_async_mongo_db()[f"team_conversations_{scope}"]
    if conversation_id in SYSTEM_ROOMS:
        scope, rooms, members, admin_ids = await _ensure_system_rooms(org_id)
        room = rooms[conversation_id]
    else:
        room = await collection.find_one(
            {"conversation_id": conversation_id, "chat_type": "room"},
            {"_id": 0},
        )
        if room:
            scope, _, members, admin_ids = await _group_member_records(org_id)
    if not room:
        if conversation_id.startswith("team_room_"):
            raise HTTPException(status_code=404, detail="Team room not found")
        return None

    active_ids = set(members)
    if room.get("chat_type") == "room":
        participants = [item for item in room.get("participants", []) if item in active_ids]
        if participants != room.get("participants", []):
            joined_at = {key: value for key, value in (room.get("joined_at") or {}).items() if key in participants}
            unread = {key: value for key, value in (room.get("unread") or {}).items() if key in participants}
            room_update: dict = {"$set": {"participants": participants, "joined_at": joined_at}}
            removed_ids = set(room.get("participants") or []) - set(participants)
            if removed_ids:
                room_update["$unset"] = {f"unread.{user_id}": "" for user_id in removed_ids}
            await collection.update_one(
                {"conversation_id": conversation_id},
                room_update,
            )
            room.update({"participants": participants, "joined_at": joined_at, "unread": unread})
    if user_id not in active_ids or user_id not in room.get("participants", []):
        raise HTTPException(status_code=403, detail="You are not a member of this team room")
    if for_post and room.get("chat_type") == "announcements" and user_id not in admin_ids:
        raise HTTPException(status_code=403, detail="Only organization admins can post announcements")
    return scope, room


async def create_team_room(org_id: str, creator_id: str, name: str, member_ids: list[str]) -> dict:
    scope, _, members, admin_ids = await _group_member_records(org_id)
    if creator_id not in admin_ids:
        raise HTTPException(status_code=403, detail="Only an organization admin can create rooms")
    participants = list(dict.fromkeys([creator_id, *member_ids]))
    if not 2 <= len(participants) <= 100:
        raise HTTPException(status_code=422, detail="Choose 1-99 other active team members")
    if any(user_id not in members for user_id in participants):
        raise HTTPException(status_code=422, detail="Every room member must be an active team member")
    now = datetime.now(timezone.utc)
    room = {
        "conversation_id": f"team_room_{uuid.uuid4().hex}",
        "type": "team",
        "chat_type": "room",
        "name": _room_name(name),
        "participants": participants,
        "joined_at": {user_id: now for user_id in participants},
        "created_by": creator_id,
        "created_at": now,
        "updated_at": now,
        "last_message": "",
        "unread": {user_id: 0 for user_id in participants},
    }
    await get_async_mongo_db()[f"team_conversations_{scope}"].insert_one(room.copy())
    from services.websocket_service import broadcast_team_conversation_updated
    await broadcast_team_conversation_updated(
        scope, room["conversation_id"], sorted(set(participants) | admin_ids), {"action": "created"}
    )
    return _room_response(room, creator_id, True)


async def update_team_room(
    org_id: str, admin_id: str, room_id: str, name: str | None, member_ids: list[str] | None
) -> dict:
    scope, _, members, admin_ids = await _group_member_records(org_id)
    if admin_id not in admin_ids:
        raise HTTPException(status_code=403, detail="Only an organization admin can manage rooms")
    collection = get_async_mongo_db()[f"team_conversations_{scope}"]
    room = await collection.find_one({"conversation_id": room_id, "chat_type": "room"}, {"_id": 0})
    if not room:
        raise HTTPException(status_code=404, detail="Team room not found")

    update: dict = {"updated_at": datetime.now(timezone.utc)}
    old_participants = list(room.get("participants") or [])
    if name is not None:
        update["name"] = _room_name(name)
    if member_ids is not None:
        participants = list(dict.fromkeys(member_ids))
        if len(participants) > 100 or any(user_id not in members for user_id in participants):
            raise HTTPException(status_code=422, detail="Room members must be active team members")
        now = datetime.now(timezone.utc)
        previous_joined = room.get("joined_at") or {}
        update["participants"] = participants
        update["joined_at"] = {user_id: previous_joined.get(user_id, now) for user_id in participants}
        previous_unread = room.get("unread") or {}
        update["unread"] = {user_id: int(previous_unread.get(user_id, 0)) for user_id in participants}
    database_update: dict = {"$set": {key: value for key, value in update.items() if key != "unread"}}
    if member_ids is not None:
        previous_unread = room.get("unread") or {}
        for user_id in update["participants"]:
            if user_id not in previous_unread:
                database_update["$set"][f"unread.{user_id}"] = 0
        removed_ids = set(old_participants) - set(update["participants"])
        if removed_ids:
            database_update["$unset"] = {f"unread.{user_id}": "" for user_id in removed_ids}
    await collection.update_one({"conversation_id": room_id}, database_update)
    room.update(update)
    notify_ids = sorted(set(old_participants) | set(room.get("participants") or []) | admin_ids)
    from services.websocket_service import broadcast_team_conversation_updated
    await broadcast_team_conversation_updated(scope, room_id, notify_ids, {"action": "updated"})
    return _room_response(room, admin_id, True)


async def leave_team_room(org_id: str, user_id: str, room_id: str) -> dict:
    scope, _, _, admin_ids = await _group_member_records(org_id)
    collection = get_async_mongo_db()[f"team_conversations_{scope}"]
    room = await collection.find_one({"conversation_id": room_id, "chat_type": "room"}, {"_id": 0})
    if not room or user_id not in room.get("participants", []):
        raise HTTPException(status_code=404, detail="Team room not found")
    participants = [item for item in room["participants"] if item != user_id]
    joined_at = {key: value for key, value in (room.get("joined_at") or {}).items() if key != user_id}
    await collection.update_one(
        {"conversation_id": room_id},
        {
            "$set": {"participants": participants, "joined_at": joined_at,
                     "updated_at": datetime.now(timezone.utc)},
            "$unset": {f"unread.{user_id}": ""},
        },
    )
    from services.websocket_service import broadcast_team_conversation_updated
    await broadcast_team_conversation_updated(
        scope, room_id, sorted(set(room["participants"]) | admin_ids), {"action": "left", "user_id": user_id}
    )
    return {"status": "success"}


def _canonical_escalation_status(status: str) -> str:
    return "reviewed" if status == "reviewed" else "pending_human_review"


async def ensure_escalation_indexes(org_id: str) -> None:
    """Create the indexes required by durable writes and cursor discovery."""
    if not org_id or org_id in _escalation_indexes_ready:
        return
    collection = get_async_mongo_db()[f"escalations_{org_id}"]
    indexes = (
        (("id",), {"unique": True}),
        (([("status", 1), ("timestamp", -1), ("id", -1)],), {}),
        (([("conversation_id", 1), ("status", 1), ("timestamp", -1)],), {}),
        (("expires_at",), {"expireAfterSeconds": 0}),
    )
    complete = True
    for args, kwargs in indexes:
        try:
            await collection.create_index(*args, **kwargs)
        except Exception as exc:
            complete = False
            logger.error(f"[team_service] Escalation index unavailable for org={org_id}: {exc}")
    if complete:
        _escalation_indexes_ready.add(org_id)


def _encode_escalation_cursor(timestamp: datetime, escalation_id: str) -> str:
    payload = json.dumps(
        {"timestamp": timestamp.astimezone(timezone.utc).isoformat(), "id": escalation_id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_escalation_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        timestamp = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        escalation_id = str(payload["id"])
        if not escalation_id:
            raise ValueError("missing escalation id")
        return timestamp.astimezone(timezone.utc), escalation_id
    except Exception as exc:
        raise ValueError("Invalid escalation cursor") from exc


# ---------------------------------------------------------------------------
# Fetch team conversations
# ---------------------------------------------------------------------------

async def fetch_team_conversations(
    org_id: str,
    current_user_id: str,
) -> List[Dict]:
    """
    Return all team conversations where current_user_id is a participant.
    Reads from: team_conversations_{org_id}
    """
    scope, _, members, admin_ids = await _ensure_system_rooms(org_id)
    if current_user_id not in members:
        raise HTTPException(status_code=403, detail="You are not an active organization member")
    async_db = get_async_mongo_db()
    collection = async_db[f"team_conversations_{scope}"]

    query = (
        {"$or": [{"participants": current_user_id}, {"chat_type": "room"}]}
        if current_user_id in admin_ids else {"participants": current_user_id}
    )
    projection = {"_id": 0}

    cursor = (
        collection
        .find(query, projection)
        .sort("last_message_timestamp", -1)
    )

    raw_docs = await cursor.to_list(length=200)

    result = []
    for doc in raw_docs:
        participants = doc.get("participants", [])
        if doc.get("chat_type") == "room":
            active_participants = [user_id for user_id in participants if user_id in members]
            if active_participants != participants:
                joined_at = {
                    key: value for key, value in (doc.get("joined_at") or {}).items()
                    if key in active_participants
                }
                unread = {
                    key: value for key, value in (doc.get("unread") or {}).items()
                    if key in active_participants
                }
                room_update: dict = {"$set": {"participants": active_participants, "joined_at": joined_at}}
                removed_ids = set(participants) - set(active_participants)
                if removed_ids:
                    room_update["$unset"] = {f"unread.{user_id}": "" for user_id in removed_ids}
                await collection.update_one(
                    {"conversation_id": doc.get("conversation_id")},
                    room_update,
                )
                doc.update({"participants": active_participants, "joined_at": joined_at, "unread": unread})
                participants = active_participants
            if current_user_id not in participants and current_user_id not in admin_ids:
                continue

        if doc.get("chat_type") in {"room", "everyone", "announcements"}:
            result.append(
                _room_response(doc, current_user_id, current_user_id in admin_ids)
            )
            continue

        other_participants = [p for p in participants if p != current_user_id]

        # Is any other participant currently online?
        is_online = any(
            services.client_metadata.get(client_id, {}).get("team_scope_id") == scope
            for p_id in other_participants
            for client_id in services.agent_clients.get(p_id, set())
        )

        unread_map = doc.get("unread", {})
        user_unread = unread_map.get(current_user_id, 0)

        lmt = doc.get("last_message_timestamp")
        timestamp = serialize_mongo(lmt) if isinstance(lmt, datetime) else None

        result.append({
            "id": doc.get("conversation_id"),
            "customer_name": doc.get("customer_name") or doc.get("conversation_id"),
            "last_message": doc.get("last_message", ""),
            "timestamp": timestamp,
            "unread_count": user_unread,
            "platform": "internal",
            "type": "team",
            "chat_type": "direct",
            "name": None,
            "participants": participants,
            "member_count": len(participants),
            "is_participant": True,
            "can_post": True,
            "is_online": is_online,
        })

    logger.info(f"[team_service] Fetched {len(result)} team conversations for {current_user_id} in org {scope}")
    return result


# ---------------------------------------------------------------------------
# Fetch AI Escalations
# ---------------------------------------------------------------------------

async def fetch_ai_escalations(org_id: str) -> List[Dict]:
    """
    Return all AI escalations that are pending human review.
    Discovery endpoint for agents to find new escalations in their org.
    Reads from: escalations_{   org_id} (without org_ prefix)
    """
    await ensure_escalation_indexes(org_id)
    async_db = get_async_mongo_db()
    collection = async_db[f"escalations_{org_id}"]

    # Only fetch escalations pending human review
    query = {"status": "pending_human_review"}
    projection = {"_id": 0}

    cursor = (
        collection
        .find(query, projection)
        .sort("timestamp", -1)
    )

    raw_docs = await cursor.to_list(length=100)

    for doc in raw_docs:
        # Format timestamps to ISO strings
        for field in ["timestamp", "created_at"]:
            val = doc.get(field)
            if isinstance(val, datetime):
                doc[field] = serialize_mongo(val)

    logger.info(f"[team_service] Fetched {len(raw_docs)} AI escalations for org {org_id}")
    return raw_docs


async def fetch_ai_escalations_v2(
    org_id: str,
    *,
    status: str = "pending",
    cursor: Optional[str] = None,
    limit: int = 50,
) -> Dict:
    """Cursor-paginated escalation discovery with explicit lifecycle status."""
    await ensure_escalation_indexes(org_id)
    canonical_status = _canonical_escalation_status(status)
    query: Dict = {
        "status": {"$in": ["pending_human_review", "reviewed"]}
        if status == "all" else canonical_status
    }
    if cursor:
        timestamp, escalation_id = _decode_escalation_cursor(cursor)
        query["$or"] = [
            {"timestamp": {"$lt": timestamp}},
            {"timestamp": timestamp, "id": {"$lt": escalation_id}},
        ]
    bounded_limit = max(1, min(int(limit), 100))
    collection = get_async_mongo_db()[f"escalations_{org_id}"]
    docs = await (
        collection.find(query, {"_id": 0})
        .sort([("timestamp", -1), ("id", -1)])
        .limit(bounded_limit + 1)
        .to_list(length=bounded_limit + 1)
    )
    has_more = len(docs) > bounded_limit
    items = docs[:bounded_limit]
    next_cursor = None
    if has_more and items:
        last_timestamp = items[-1].get("timestamp")
        if isinstance(last_timestamp, datetime):
            next_cursor = _encode_escalation_cursor(last_timestamp, str(items[-1].get("id") or ""))
    for doc in items:
        for field in ("timestamp", "created_at", "updated_at", "reviewed_at", "expires_at"):
            if isinstance(doc.get(field), datetime):
                doc[field] = serialize_mongo(doc[field])
    return {
        "items": items,
        "next_cursor": next_cursor,
        "status": "all" if status == "all" else "pending" if canonical_status == "pending_human_review" else "reviewed",
    }


# ---------------------------------------------------------------------------
# Update Escalation Status
# ---------------------------------------------------------------------------

async def update_escalation_status(
    org_id: str,
    escalation_id: str,
    status: str,
    reviewer_id: Optional[str] = None,
) -> bool:
    """
    Update the status of an AI escalation (e.g., from pending to reviewed).
    Targets the new collection: escalations_{org_id}
    """
    await ensure_escalation_indexes(org_id)
    async_db = get_async_mongo_db()
    collection = async_db[f"escalations_{org_id}"]
    canonical_status = _canonical_escalation_status(status)
    now = datetime.now(timezone.utc)
    update: Dict = {
        "$set": {
            "status": canonical_status,
            "updated_at": now,
        }
    }
    if canonical_status == "reviewed":
        update["$set"].update({
            "reviewed_at": now,
            "reviewed_by": reviewer_id,
            "expires_at": now + ESCALATION_RETENTION,
        })
    else:
        update["$unset"] = {"reviewed_at": "", "reviewed_by": "", "expires_at": ""}
    result = await collection.update_one(
        {"id": escalation_id},
        update,
    )

    if result.matched_count == 0:
        logger.warning(f"[team_service] No escalation found to update status: {escalation_id}")
        return False

    logger.info(f"[team_service] Updated escalation status to '{canonical_status}' for {escalation_id} in org {org_id}")
    return True


# ---------------------------------------------------------------------------
# Create AI Escalation (Programmatic/API)
# ---------------------------------------------------------------------------

async def create_ai_escalation(org_id: str, data: Dict) -> Dict:
    """
    Save a new AI escalation to MongoDB and broadcast to online agents.
    Used by the internal API and AI tools.
    """
    import uuid

    await ensure_escalation_indexes(org_id)
    async_db = get_async_mongo_db()
    collection = async_db[f"escalations_{org_id}"]
    metadata = data.get("metadata") or {}
    if not data.get("id") and metadata.get("reason"):
        existing = await collection.find_one(
            {
                "customer_number": data.get("customer_number"),
                "status": "pending_human_review",
                "metadata.sku": metadata.get("sku"),
                "metadata.reason": metadata["reason"],
                "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=1)},
            },
            {"_id": 0},
        )
        if existing:
            return existing

    # Prepare document
    now = datetime.now(timezone.utc)
    canonical_status = _canonical_escalation_status(data.get("status", "pending_human_review"))
    escalation_doc = {
        "org_id": org_id,
        "id": data.get("id") or str(uuid.uuid4()),
        "type": data.get("type"),
        "schema_version": data.get("schema_version"),
        "user_name": data.get("user_name"),
        "customer_number": data.get("customer_number"),
        "customer_text": data.get("customer_text"),
        "inferred_intent": data.get("inferred_intent"),
        "priority": data.get("priority", "high"),
        "kind": data.get("kind", "actionable"),
        "status": canonical_status,
        "timestamp": now,
        "created_at": now,
        "role": data.get("role", "ai_agent"),
        "sender_name": data.get("sender_name", "AI Assistant"),
        "conversation_id": data.get("conversation_id") or metadata.get("conversation_id"),
        "channel": data.get("channel") or metadata.get("channel"),
        "context": data.get("context"),
        "is_ai_escalation": True,
        "metadata": metadata
    }
    if canonical_status == "reviewed":
        escalation_doc.update({
            "reviewed_at": now,
            "reviewed_by": data.get("reviewed_by"),
            "expires_at": now + ESCALATION_RETENTION,
        })

    # Mongo's native unique _id makes concurrent retries safe without a new index.
    dedupe_id = data.get("dedupe_id")
    dedupe_filter = (
        {"_id": dedupe_id}
        if isinstance(dedupe_id, str) and 1 <= len(dedupe_id) <= 128
        else {"id": escalation_doc["id"]}
    )
    result = await collection.update_one(
        dedupe_filter,
        {"$setOnInsert": escalation_doc.copy()},
        upsert=True,
    )
    if not result.upserted_id:
        return await collection.find_one(dedupe_filter, {"_id": 0})
    logger.success(f"[team_service] Created new AI escalation: {escalation_doc['id']} for org {org_id}")

    # Broadcast via WebSocket
    # Prepare broadcast payload (format timestamps as ISO string)
    broadcast_payload = escalation_doc.copy()
    for field in ("timestamp", "created_at", "reviewed_at", "expires_at"):
        if isinstance(broadcast_payload.get(field), datetime):
            broadcast_payload[field] = serialize_mongo(broadcast_payload[field])
    
    try:
        from services.websocket_service import broadcast_ai_escalation

        await asyncio.wait_for(
            broadcast_ai_escalation(org_id, broadcast_payload),
            timeout=1,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[team_service] Escalation broadcast timed out id={escalation_doc['id']} org={org_id}"
        )
    except Exception as exc:
        logger.warning(
            f"[team_service] Escalation broadcast failed id={escalation_doc['id']} "
            f"org={org_id} error={type(exc).__name__}"
        )

    return escalation_doc


# ---------------------------------------------------------------------------
# Fetch team messages for a specific conversation
# ---------------------------------------------------------------------------

async def fetch_team_messages(
    org_id: str,
    conversation_id: str,
    agent_id: str,
) -> Dict:
    """
    Return messages for a team conversation and reset the per-user unread count.
    Reads from: team_messages_{org_id}
    Resets unread in: team_conversations_{org_id}
    """
    try:
        scope = resolve_team_scope(org_id)
        room_access = await require_team_room(org_id, agent_id, conversation_id)
        room = room_access[1] if room_access else None
        async_db = get_async_mongo_db()
        team_messages_col = async_db[f"team_messages_{scope}"]
        team_convos_col = async_db[f"team_conversations_{scope}"]

        # 1. Authorization & Reset unread count for this agent
        # We combine this into one update_one to verify participation
        result = await team_convos_col.update_one(
            {"conversation_id": conversation_id, "participants": agent_id},
            {"$set": {f"unread.{agent_id}": 0}}
        )

        if result.matched_count == 0:
            logger.info(f"[team_service] Conversation {conversation_id} not found or user {agent_id} not a participant. Returning empty list.")
            return {"messages": []}

        # 2. Fetch messages
        message_query: dict = {"conversation_id": conversation_id}
        joined_at = (room or {}).get("joined_at", {}).get(agent_id)
        if joined_at:
            message_query["timestamp"] = {"$gte": _membership_created_at({"created_at": joined_at})}
        cursor = team_messages_col.find(
            message_query,
            {"_id": 0}
        ).sort("timestamp", 1)
        
        messages = await cursor.to_list(length=None)

        # Normalize timestamps to ISO strings
        for msg in messages:
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                msg["timestamp"] = serialize_mongo(ts)

        logger.info(
            f"[team_service] Fetched {len(messages)} messages "
            f"for conversation {conversation_id} in org {scope}"
        )
        return {"messages": messages}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[team_service] fetch_team_messages error: {e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch team messages"
        )
