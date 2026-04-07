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
        timestamp = lmt.isoformat() if isinstance(lmt, datetime) else None

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

async def fetch_team_escalations(org_id: str) -> List[Dict]:
    """
    Return all team conversations that are pending human review.
    Discovery endpoint for agents to find new escalations.
    """
    async_db = get_async_mongo_db()
    collection = async_db[f"team_conversations_{org_id}"]

    # Only fetch conversations pending human review
    query = {"status": "pending_human_review"}
    projection = {"_id": 0}

    cursor = (
        collection
        .find(query, projection)
        .sort("last_message_timestamp", -1)
    )

    raw_docs = await cursor.to_list(length=100)

    result = []
    for doc in raw_docs:
        lmt = doc.get("last_message_timestamp")
        timestamp = lmt.isoformat() if isinstance(lmt, datetime) else None

        result.append({
            "id": doc.get("conversation_id"),
            "customer_name": doc.get("customer_name") or f"Escalation: {doc.get('conversation_id')}",
            "last_message": doc.get("last_message", ""),
            "timestamp": timestamp,
            "status": doc.get("status"),
            "inferred_intent": doc.get("inferred_intent"),
            "platform": "internal",
            "type": "team",
            "participants": doc.get("participants", []),
        })

    logger.info(f"[team_service] Fetched {len(result)} escalations for org {org_id}")
    return result


# ---------------------------------------------------------------------------
# Update Conversation Status
# ---------------------------------------------------------------------------

async def update_team_conversation_status(
    org_id: str,
    conversation_id: str,
    status: str,
    current_user_id: str
) -> bool:
    """
    Update the status of a team conversation (e.g., from pending to reviewed).
    """
    async_db = get_async_mongo_db()
    collection = async_db[f"team_conversations_{org_id}"]

    result = await collection.update_one(
        {"conversation_id": conversation_id, "participants": current_user_id},
        {"$set": {
            "status": status,
            "updated_at": datetime.now(timezone.utc)
        }}
    )

    if result.matched_count == 0:
        logger.warning(f"[team_service] No conversation found to update status: {conversation_id}")
        return False

    logger.info(f"[team_service] Updated status to '{status}' for conv {conversation_id} in org {org_id}")
    return True


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
            # Check if conversation exists but user is not a participant
            exists = await team_convos_col.find_one({"conversation_id": conversation_id}, {"_id": 1})
            if exists:
                logger.warning(f"[team_service] Unauthorized message access: user {agent_id} in conv {conversation_id}")
                raise HTTPException(status_code=403, detail="Access denied: You are not a participant in this conversation")
            else:
                raise HTTPException(status_code=404, detail="Conversation not found")

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
                msg["timestamp"] = ts.isoformat()

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

