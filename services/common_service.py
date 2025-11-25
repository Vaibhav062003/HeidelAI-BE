import uuid
from bson import ObjectId
from typing import Optional
from fastapi import HTTPException
from datetime import datetime, timezone
from pymongo.errors import PyMongoError
from pymongo.collection import Collection
from core.services import services

from database import get_mongo_db

db = get_mongo_db()

async def fetch_conversations(
    org_id: str,
    status: Optional[str] = None,
    platform: Optional[str] = None,
    priority: Optional[str] = None,
    # unread_only: Optional[bool] = False,
):
    """
    Fetch conversations for a given org_id with optional filters.
    Status -
    - status=None: return all conversations
    - status='open': return conversations with status 'open' or 'follow-up'
    - status='closed': return conversations with status 'closed'

    Platform - 
    - platform=None: return all connected platforms
    - platform='whatsapp' or 'instagram': filter by platform
    Priority -
    - priority=None: return all conversations regardless of priority
    - priority='low', 'medium', 'high': filter by priority
    """

    orgs_collection: Collection = db.organizations
    org_data = orgs_collection.find_one({"org_id": org_id})
    if not org_data:
        raise HTTPException(status_code=404, detail="Organization not found")

    org_conversations_collection_name = f"conversations_{org_id}"
    if org_conversations_collection_name not in db.list_collection_names():
        return []

    org_conversations_collection: Collection = db[org_conversations_collection_name]

    query_filter = {}

    # Platform filter
    if platform:
        platform = platform.lower()
        if platform not in ["whatsapp", "instagram"]:
            raise HTTPException(status_code=400, detail="Invalid platform parameter")
        query_filter["platform"] = platform
    else:
        # Determine connected platforms
        connected_platforms = []
        if org_data.get("ig_id"):
            connected_platforms.append("instagram")
        if org_data.get("wa_id"):
            connected_platforms.append("whatsapp")

        query_filter = {
            "platform": {"$in": connected_platforms},
        }

    # Status filter
    if status is None:
        query_filter["status"] = {"$in": ["open", "follow-up", "closed"]}
    elif status.lower() == "open":
        query_filter["status"] = {"$in": ["open", "follow-up"]}
    elif status.lower() == "closed":
        query_filter["status"] = "closed"
    else:
        raise HTTPException(status_code=400, detail="Invalid status query parameter")
    
    # Priority filter
    if priority:
        priority = priority.lower()
        if priority not in ["low", "medium", "high"]:
            raise HTTPException(status_code=400, detail="Invalid priority parameter")
        query_filter["priority"] = priority

    # Unread filter
    # if unread_only:
    #     query_filter["unread_count"] = {"$gt": 0}

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

async def reopen_conversation(org_id: str, conversation_id: str):
    """
    Reopen a closed conversation by removing the 'closed_at' field.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        try:
            result = conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {
                    "$unset": {
                        "closed_at": 1, 
                        "closure_reason": 1
                    },
                 "$set": {"started_at": datetime.now(timezone.utc)}}
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

async def close_conversation(org_id: str, conversation_id: str, reason: str):
    """
    Mark a conversation as closed for a given organization.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        now = datetime.now(timezone.utc)

        conversation =  conversation_collection.find_one({"conversation_id": conversation_id})

        if not conversation:
            return {"message": "Conversation not found. Nothing to close."}

        conversation_collection.update_one(
            {"conversation_id": conversation_id},
            {
                "$set": {
                    "status": "closed",
                    "closed_at": now,
                    "assigned_agent": None,
                    "closure_reason": reason
                }
            }
        )

        if "assignment_history" in conversation and conversation["assignment_history"]:
            last_index = len(conversation["assignment_history"]) - 1
            update_path = f"assignment_history.{last_index}.assigned_until"

            conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {update_path: now}}
            )

        return {"message": "Conversation closed successfully."}

    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

async def unarchive_data(org_id: str, platform: str, user_id: str):
    """
    Unarchives data from the archived collections back to the main collections. 
    For all the platforms.
    """

    try:
        query = {
            "org_id": org_id,
            "platform": platform,
            f"{platform}_id": user_id,
            "status": "active"
        }

        archived_meta_data = db["archived_tracker"].find_one(query)

        if not archived_meta_data:
            raise HTTPException(status_code=404, detail=f"No active archive found for {query}: {str(e)}")

        archive_id = archived_meta_data.get("archive_id")

        archive_messages = f"archived_messages_{org_id}_{archive_id}"
        archive_conversations = f"archived_conversations_{org_id}_{archive_id}"
        archive_private_notes = f"archived_private_notes_{org_id}_{archive_id}"

        main_messages = f"messages_{org_id}"
        main_conversations = f"conversations_{org_id}"
        main_private_notes = f"private_notes_{org_id}"

        move_conversations_unarchive_safe(archive_conversations, main_conversations)

        move_messages_unarchive_safe(archive_messages, main_messages)

        move_private_notes_unarchive_safe(archive_private_notes, main_private_notes)

        db["archived_tracker"].update_one(
            {"archive_id": archive_id},
            {"$set": {"status": "recovered", "restored_at": datetime.utcnow()}}
        )

        print(f"Dropping archived collections: {archive_conversations}, {archive_messages}")
        db.drop_collection(archive_conversations)
        db.drop_collection(archive_messages)
        db.drop_collection(archive_private_notes)

        print(f"Unarchiving completed for org_id={org_id}, platform={platform}")

        return {"message": "Unarchiving completed successfully."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
def move_messages_unarchive_safe(source_name, target_name):
    cursor = db[source_name].find({}, {"_id": 0})  
    buffer = []
    for doc in cursor:
        buffer.append(doc)
        if len(buffer) >= services.BATCH_SIZE:
            db[target_name].insert_many(buffer)
            buffer = []
    if buffer:
        db[target_name].insert_many(buffer)
    
    db[source_name].delete_many({})

def move_private_notes_unarchive_safe(source_name: str, target_name: str):
    """Moves private note docs from archive to main collection."""
    cursor = db[source_name].find({}, {"_id": 0})

    buffer = []
    for note in cursor:
        buffer.append(note)

        if len(buffer) >= services.BATCH_SIZE:
            if buffer:
                db[target_name].insert_many(buffer)
                buffer = []

    if buffer:
        db[target_name].insert_many(buffer)

    db[source_name].delete_many({})

def move_conversations_unarchive_safe(source_name: str, target_name: str):
    """
    Moves conversation docs from archive to main collection.
    If a doc with the same conversation_id already exists in main,
    skip inserting that doc (keep the main one).
    """
    cursor = db[source_name].find({}, {"_id": 0})

    buffer = []
    for convo in cursor:
        existing = db[target_name].find_one({"conversation_id": convo["conversation_id"]})
        if not existing:
            buffer.append(convo)

        if len(buffer) >= services.BATCH_SIZE:
            if buffer:
                db[target_name].insert_many(buffer)
                buffer = []

    if buffer:
        db[target_name].insert_many(buffer)

    db[source_name].delete_many({})

async def add_private_note(org_id: str, conversation_id: str, note: str, posted_by: str, role: str, platform: str, connection_id: str) -> bool:
    """
    Adds a private note for a specific conversation.
    Stores the note in the private_notes collection
    """

    try:
        private_notes_collection = db[f"private_notes_{org_id}"]

        note_doc = {
            "note_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "platform": platform,
            "connection_id": connection_id,
            "content": note,
            "timestamp": datetime.now(timezone.utc),
            "user_name": posted_by,
            "role": role
        }

        result = private_notes_collection.insert_one(note_doc)

        if not result.inserted_id:
            raise HTTPException(status_code=500, detail="Failed to add private note")
        
        return True
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
async def delete_private_note(org_id: str, note_id: str) -> bool:
    """
    Removes the private_note field from a specific conversation document.
    """

    try:
        messages_collection = db[f"messages_{org_id}"]

        try:
            _id = ObjectId(note_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid note ID")

        note = messages_collection.find_one({
            "_id": _id, "message_type": "private_note"
        })

        if not note:
            raise HTTPException(status_code=404, detail="Private note not found")
        
        messages_collection.delete_one({"_id": _id})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting private note: {str(e)}")
    