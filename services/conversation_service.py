from pymongo.errors import PyMongoError
from pymongo.collection import Collection
from fastapi import HTTPException
from datetime import datetime, timezone

from database import get_mongo_db

db = get_mongo_db()

def fetch_conversations(org_id: str, status: str | None):
    """
    Fetch conversations for a given org_id with optional status filter.
    - status=None: return all conversations
    - status='open': return open + follow-up
    - status='closed': return closed
    """

    orgs_collection: Collection = db.organizations
    org_data = orgs_collection.find_one({"org_id": org_id})
    if not org_data:
        raise HTTPException(status_code=404, detail="Organization not found")

    # organization specific collection name
    org_conversations_collection_name = f"conversations_{org_id}"
    if org_conversations_collection_name not in db.list_collection_names():
        return []

    org_conversations_collection: Collection = db[org_conversations_collection_name]

    # Determine allowed platforms
    allowed_platforms = []
    if org_data.get("ig_id"):
        allowed_platforms.append("instagram")
    if org_data.get("wa_id"):
        allowed_platforms.append("whatsapp")

    # Build status filter
    if status is None:
        status_filter = {"$in": ["open", "follow-up", "closed"]}
    elif status.lower() == "open":
        status_filter = {"$in": ["open", "follow-up"]}
    elif status.lower() == "closed":
        status_filter = "closed"
    else:
        raise HTTPException(status_code=400, detail="Invalid status query parameter")

    query_filter = {
        "platform": {"$in": allowed_platforms},
        "status": status_filter,
    }

    conversationFieldProjection = {
        "conversation_id": 1,
        "platform": 1,
        "customer_username": 1,
        "customer_name": 1,
        "last_message": 1,
        "last_message_timestamp": 1,
        "unread_count": 1,
        "is_ai_enabled": 1,
        "assigned_agent_id": 1,
        "priority": 1,
        "sentiment": 1,
        "status": 1,
        "_id": 0,
    }

    raw_conversations = list(
        org_conversations_collection.find(query_filter, conversationFieldProjection)
        .sort("last_message_timestamp", -1)
    )

    formatted_conversations = []
    for conv in raw_conversations:
        timestamp = conv.get("last_message_timestamp")
        if timestamp and isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()

        formatted_conversations.append({
            "id": conv.get("conversation_id"),
            "platform": conv.get("platform"),
            "customer_id": conv.get("customer_username"),
            "customer_name": f"{conv.get('customer_name', '')}",
            "last_message": conv.get("last_message", ""),
            "timestamp": timestamp,
            "unread_count": conv.get("unread_count", 0),
            "is_ai_enabled": conv.get("is_ai_enabled", True),
            "assigned_agent_id": conv.get("assigned_agent_id"),
            "priority": conv.get("priority", "medium"),
            "sentiment": conv.get("sentiment", "neutral"),
            "query": conv.get("query", ""),
        })

    return formatted_conversations

def reopen_conversation(org_id: str, conversation_id: str):
    """
    Reopen a closed conversation by removing the 'closed_at' field.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        try:
            result = conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {"$unset": {"closed_at": ""}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error updating conversation: {str(e)}")

        if result.matched_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"No conversation found with conversation_id={conversation_id}"
            )

        return {"message": "'closed_at' removed, conversation reopened successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

def close_conversation(org_id: str, conversation_id: str):
    """
    Mark a conversation as closed for a given organization.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        result = conversation_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"status": "closed", "closed_at": datetime.now(timezone.utc)}}
        )

        if result.matched_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"Conversation not found for conversation_id={conversation_id}"
            )

        return {"message": "Conversation closed successfully."}

    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")