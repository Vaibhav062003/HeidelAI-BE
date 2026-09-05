"""
team_service.py

Dedicated service for team (internal) conversations and messages.
Reads from isolated collections:
  - team_conversations_{org_id}
  - team_messages_{org_id}

Sending team messages is still handled by common_service.send_message
(platform="internal"), which writes into these same collections.
WebSocket broadcasting (broadcast_team_message) is unchanged.
"""

import asyncio
import base64
import json
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
    async_db = get_async_mongo_db()
    collection = async_db[f"team_conversations_{org_id}"]

    query = {"participants": current_user_id}
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
        other_participants = [p for p in participants if p != current_user_id]

        # Is any other participant currently online?
        is_online = any(
            p_id in services.agent_clients and services.agent_clients[p_id]
            for p_id in other_participants
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
            "participants": participants,
            "is_online": is_online,
        })

    logger.info(f"[team_service] Fetched {len(result)} team conversations for {current_user_id} in org {org_id}")
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
        async_db = get_async_mongo_db()
        team_messages_col = async_db[f"team_messages_{org_id}"]
        team_convos_col = async_db[f"team_conversations_{org_id}"]

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
        cursor = team_messages_col.find(
            {"conversation_id": conversation_id},
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
            f"for conversation {conversation_id} in org {org_id}"
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
