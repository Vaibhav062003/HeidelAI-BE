import uuid
import time
from bson import ObjectId
from typing import Optional, List, Dict
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorCollection
from datetime import datetime, timezone
from pymongo.errors import PyMongoError
from pymongo.collection import Collection
from pymongo import ReturnDocument
from core.services import services
from services.wa_service import send_whatsapp_message, upload_and_send_wa_media_message
from services.ig_service import send_ig_message, upload_and_send_ig_media_message
from services.utils import serialize_mongo

from database import get_mongo_db, get_async_mongo_db

from core.logger import get_logger

logger = get_logger(__name__)

db = get_mongo_db()

async def fetch_conversations_new(org_id: str) -> List[Dict]:
    start_time = time.time()

    async_db = get_async_mongo_db()


    collection = async_db[f"conversations_{org_id}"]

    projection = {
        "whatsapp_id": 0,
        "instagram_id": 0,
        "last_sender": 0,
        "message_counter": 0,
        "next_action_at": 0,
        "reminder_stage": 0,
        "started_at": 0,
        "last_analyzed": 0,
        "_id": 0,
    }

    cursor = (
        collection
        .find({}, projection)
        .sort("last_message_timestamp", -1)
        .batch_size(1000)
    )

    # Fetch all docs
    raw_docs = await cursor.to_list(length=None)

    fetch_time = time.time()
    logger.info(f"[TIMING] Mongo fetch: {fetch_time - start_time:.4f}s")

    # Transform (keep it simple and fast)
    result = [
        {
            "id": doc.get("conversation_id"),
            "platform": doc.get("platform"),
            "customer_id": doc.get("customer_username"),
            "phone_number": doc.get("customer_id") if doc.get("platform") == "whatsapp" else None,
            "customer_name": doc.get("customer_name") or "",
            "last_message": doc.get("last_message") or "",
            "last_message_is_private_note": doc.get("last_message_is_private_note", False),
            "timestamp": (
                doc["last_message_timestamp"].isoformat()
                if isinstance(doc.get("last_message_timestamp"), datetime)
                else None
            ),
            "unread_count": doc.get("unread_count", 0),
            "status": doc.get("status"),
            "priority": doc.get("priority"),
            "sentiment": doc.get("sentiment"),
            "assigned_agent_id": doc.get("assigned_agent_id"),
            "is_ai_enabled": doc.get("is_ai_enabled"),
            "reply_window_ends_at": doc.get("reply_window_ends_at").isoformat() if isinstance(doc.get("reply_window_ends_at"), datetime) else None,
            "assigned_agent": doc.get("assigned_agent", {}),
            "assignment_history": doc.get("assignment_history", []),
            "suggestions": doc.get("suggestions", []),
            "summary": doc.get("summary", []),
        }
        for doc in raw_docs
    ]

    end_time = time.time()
    logger.info(f"[TIMING] Python transform: {end_time - fetch_time:.4f}s")
    logger.info(f"[TIMING] Total: {end_time - start_time:.4f}s")

    return result

async def fetch_conversation(org_id: str, conversation_id: str):
    """
    Fetch a specific conversation by its ID for a given organization.
    """
    try:
        async_db = get_async_mongo_db()

        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Get the collection
        org_conversations_collection = async_db[org_conversations_collection_name]

        conversation = await org_conversations_collection.find_one(
            {"conversation_id": conversation_id},
            {"_id": 0}  
        )
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        
        for field in conversation:
            if isinstance(conversation[field], datetime):
                conversation[field] = conversation[field].isoformat()
        
        return conversation
    
    except HTTPException:
        raise
    
    except Exception as e:
        logger.error(f"[fetch_conversation] {org_id=} {conversation_id=} error={e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch conversation"
        )

async def fetch_messages(org_id: str, conversation_id: str, agent_id: str, agent_username: str):
    """
    Fetch messages and private notes for a specific conversation.
    Also auto-assigns the agent to the conversation if it is unassigned.
    Arguments:
    - org_id: Organization ID
    - conversation_id: Conversation ID
    - agent_username: Username of the agent accessing this conversation

    Returns:
    - A dictionary with 'messages' (list of messages and notes) and 'assignment' (assignment details)
    """

    try:
        org_messages_collection_name = f"messages_{org_id}"
        private_note_collection_name = f"private_notes_{org_id}"

        # Auto assigning agent to the conversation if it is unassigned
        org_conversations_collection_name = f"conversations_{org_id}"
        convo_collection = db[org_conversations_collection_name]

        now = datetime.now(timezone.utc)

        assigned_convo = convo_collection.find_one_and_update(
            {
                "conversation_id": conversation_id,
                "$or": [
                    {"assigned_agent" : {"$exists": False}},
                    {"assigned_agent": None}
                ],
                "status": "open"
            },
            {
                "$set": {
                    "assigned_agent": {
                        "id": agent_id,
                        "username": agent_username,
                        "assigned_at": now
                    }
                },
                "$push": {
                    "assignment_history": {
                        "id": agent_id,
                        "username": agent_username,
                        "assigned_at": now
                    }
                }
            },
            projection={"assigned_agent": 1, "assignment_history": 1, "_id": 0},
            return_document=ReturnDocument.AFTER
        )

        if assigned_convo is None:
             # The conversation was already assigned or closed. Fetch the current assignment details.
             current_convo = convo_collection.find_one(
                 {"conversation_id": conversation_id},
                 projection={"assigned_agent": 1, "assignment_history": 1, "_id": 0}
             )
             assigned_convo = current_convo

        convo_collection.update_one(
            {"conversation_id": conversation_id},
            {
                "$set": {
                    "unread_count": 0
                }
            }
        )

        messages = []
        notes = []
        
        # Fetching messages
        if org_messages_collection_name in db.list_collection_names():
            org_messages_collection = db[org_messages_collection_name]
            messages = list(org_messages_collection.find(
                {"conversation_id": conversation_id},
                {"_id": 0}  
            ).sort("timestamp", 1))  
        
        #Fetching private notes
        if private_note_collection_name in db.list_collection_names():
            private_note_collection = db[private_note_collection_name]
            notes = list(private_note_collection.find(
                    {"conversation_id": conversation_id},
                    {"_id": 0}
                ).sort("timestamp", 1)
            )

        # Merging both messages and notes
        combined = messages + notes

        # Normalizing timestamps
        for msg in combined:
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                msg["timestamp"] = ts.isoformat()

        combined.sort(key=lambda x: x.get("timestamp"), reverse=False)
        
        return {"messages": combined, "assignment": assigned_convo}
    
    except Exception as e:
        logger.error(f"Error fetching messages or private notes for {conversation_id} of org {org_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to fetch messages: {str(e)}"
        )

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

        result = conversation_collection.find_one_and_update(
            {"conversation_id": conversation_id},
            {
                "$set": {
                    "status": "closed",
                    "closed_at": now,
                    "assigned_agent": None,
                    "closure_reason": reason
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if "assignment_history" in conversation and conversation["assignment_history"]:
            last_index = len(conversation["assignment_history"]) - 1
            update_path = f"assignment_history.{last_index}.assigned_until"

            result = conversation_collection.find_one_and_update(
                {"conversation_id": conversation_id},
                {"$set": {update_path: now}},
                return_document=ReturnDocument.AFTER
            )

        convo = serialize_mongo(result)
        return {"conversation": convo}

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

        logger.info(f"Dropping archived collections: {archive_conversations}, {archive_messages}")
        db.drop_collection(archive_conversations)
        db.drop_collection(archive_messages)
        db.drop_collection(archive_private_notes)

        logger.success(f"Unarchiving completed for org_id={org_id}, platform={platform}")

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

async def send_message(org_id: str, platform: str, conversation_id: str, content: str, mode:str, file: Optional[any] = None, temp_id: Optional[str] = None, context_type: Optional[str] = None, context: Optional[dict] = None):
    """
    Sends a message to the specified conversation on the given platform.
    """
    res = {"status": "no_action"}

    if platform == "whatsapp":

        wa_id = db.organizations.find_one({"org_id": org_id}, {"wa_id": 1}).get("wa_id")

        if file:
            recipient_id = conversation_id.replace("whatsapp_", "")
            
            res = await upload_and_send_wa_media_message(recipient_id, file, wa_id, org_id, content, temp_id, mode, context_type, context)

            if res.get("error"):
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to send media message: {res['error']}"
                )

        if content and not file:
            res = await send_whatsapp_message({
                "conversation_id": conversation_id,
                "content": content,
                "sender_id": wa_id,
            }, org_id, mode, temp_id, context_type, context)
        return res
    
    elif platform == "instagram":
        ig_id = db.organizations.find_one({"org_id": org_id}, {"ig_id": 1}).get("ig_id")

        recipient = db[f"conversations_{org_id}"].find_one(
            {"conversation_id": conversation_id},
            {"customer_id": 1}
        ).get("customer_id")


        if file:
            res = await upload_and_send_ig_media_message(recipient, file, ig_id, org_id, mode)

            if res.get("error"):
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to send media message: {res['error']}"
                )
        
        if content :
            res = await send_ig_message(recipient, content, ig_id, mode, org_id)
        return res
    else:
        raise HTTPException(status_code=400, detail="Unsupported platform")
