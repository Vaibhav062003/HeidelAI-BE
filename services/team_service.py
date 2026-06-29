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

from datetime import datetime, timezone
from typing import Optional, List, Dict

from fastapi import HTTPException
from loguru import logger

from core.services import services
from database import get_mongo_db, get_async_mongo_db
from services.utils import serialize_mongo


db = get_mongo_db()


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


# ---------------------------------------------------------------------------
# Update Escalation Status
# ---------------------------------------------------------------------------

async def update_escalation_status(
    org_id: str,
    escalation_id: str,
    status: str
) -> bool:
    """
    Update the status of an AI escalation (e.g., from pending to reviewed).
    Targets the new collection: escalations_{org_id}
    """
    async_db = get_async_mongo_db()
    collection = async_db[f"escalations_{org_id}"]

    result = await collection.update_one(
        {"id": escalation_id},
        {"$set": {
            "status": status,
            "updated_at": datetime.now(timezone.utc)
        }}
    )

    if result.matched_count == 0:
        logger.warning(f"[team_service] No escalation found to update status: {escalation_id}")
        return False

    logger.info(f"[team_service] Updated escalation status to '{status}' for {escalation_id} in org {org_id}")
    return True


# ---------------------------------------------------------------------------
# Create AI Escalation (Programmatic/API)
# ---------------------------------------------------------------------------

async def create_ai_escalation(org_id: str, data: Dict) -> Dict:
    """
    Save a new AI escalation to MongoDB and broadcast to online agents.
    Used by the internal API and AI tools.
    """
    from services.websocket_service import broadcast_ai_escalation
    import uuid

    async_db = get_async_mongo_db()
    collection = async_db[f"escalations_{org_id}"]

    # Prepare document
    escalation_doc = {
        "id": data.get("id") or str(uuid.uuid4()),
        "user_name": data.get("user_name"),
        "customer_number": data.get("customer_number"),
        "customer_text": data.get("customer_text"),
        "inferred_intent": data.get("inferred_intent"),
        "status": data.get("status", "pending_human_review"),
        "timestamp": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "role": data.get("role", "ai_agent"),
        "sender_name": data.get("sender_name", "AI Assistant"),
        "is_ai_escalation": True,
        "metadata": data.get("metadata", {})
    }

    # Save to MongoDB
    await collection.insert_one(escalation_doc.copy())
    logger.success(f"[team_service] Created new AI escalation: {escalation_doc['id']} for org {org_id}")

    # Broadcast via WebSocket
    # Prepare broadcast payload (format timestamps as ISO string)
    broadcast_payload = escalation_doc.copy()
    broadcast_payload["timestamp"] = serialize_mongo(broadcast_payload["timestamp"])
    broadcast_payload["created_at"] = serialize_mongo(broadcast_payload["created_at"])
    
    await broadcast_ai_escalation(org_id, broadcast_payload)

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

