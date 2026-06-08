import uuid
import time
import re
from bson import ObjectId
from typing import Optional, List, Dict
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from pymongo import ReturnDocument
from datetime import datetime, timezone, timedelta
from core.services import services
from services.wa_service import send_whatsapp_message, upload_and_send_wa_media_message, send_whatsapp_template_message
from services.ig_service import send_ig_message, upload_and_send_ig_media_message
from services.utils import serialize_mongo
# from services.websocket_service import broadcast_on_stats_update
from services.websocket_service import broadcast_team_message

from database import get_mongo_db, get_async_mongo_db

from loguru import logger
from dateutil import parser


db = get_mongo_db()

async def fetch_conversations_new(
    org_id: str, 
    current_user_id: Optional[str] = None, 
    skip: int = 0, 
    limit: int = 50,
    search: Optional[str] = None,
    active_tab: str = "all",
    unread_only: bool = False,
    platform: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[str] = None
) -> List[Dict]:
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

    # Only customer conversations — team conversations are in their own
    # dedicated collection (team_conversations_{org_id}) and fetched
    # via GET /api/team/conversations
    base_conditions = [
        {"type": {"$ne": "team"}}
    ]

    # Labels filter
    if labels:
        labels_list = [l.strip() for l in labels.split(",") if l.strip()]
        if labels_list:
            contact_col = async_db[f"contacts_{org_id}"]
            
            # Optimization: Use regex for case-insensitive exact matching
            regex_labels = [re.compile(f"^{re.escape(l)}$", re.IGNORECASE) for l in labels_list]
            
            # Fetch unique conversation_ids from contacts who have any of these labels
            matching_ids = await contact_col.distinct("conversation_id", {"labels": {"$in": regex_labels}})
            
            if not matching_ids:
                return [] # Early exit to save resources
            
            base_conditions.append({"conversation_id": {"$in": matching_ids}})

    # Tab filters
    if active_tab == "open":
        base_conditions.append({"status": {"$in": ["open", "follow-up"]}})
    elif active_tab == "closed":
        base_conditions.append({"status": "closed"})

    # Unread filter
    if unread_only:
        base_conditions.append({"unread_count": {"$gt": 0}})

    # Platform Filter
    if platform:
        # Split "whatsapp,instagram" into ["whatsapp", "instagram"]
        platform_list = [p.strip() for p in platform.split(",")]
        base_conditions.append({"platform": {"$in": platform_list}})

    # Priority Filter
    if priority:
        priority_list = [p.strip() for p in priority.split(",")]
        base_conditions.append({"priority": {"$in": priority_list}})

    # Search filter
    if search:
        # $options: "i" makes the search case-insensitive
        safe_search = re.escape(search)
        search_regex = {"$regex": safe_search, "$options": "i"}
        base_conditions.append({
            "$or": [
                {"customer_name": search_regex},
                {"last_message": search_regex},
                {"conversation_id": search_regex}
            ]
        })

    # Final query
    query = {"$and": base_conditions} if base_conditions else {}

    cursor = (
        collection
        .find(query, projection)
        .sort("last_message_timestamp", -1)
        .skip(skip)   # <-- Added skip
        .limit(limit) # <-- Added limit
    )

    # Fetch all docs
    raw_docs = await cursor.to_list(length=limit)

    fetch_time = time.time()
    logger.info(f"[TIMING] Mongo fetch: {fetch_time - start_time:.4f}s")

    # Transform (keep it simple and fast)
    result = []
    for doc in raw_docs:
        conv_type = doc.get("type")
        platform = doc.get("platform")
        
        conv_obj = {
            "id": doc.get("conversation_id"),
            "platform": platform,
            "type": conv_type,
            "customer_id": doc.get("customer_username"),
            "phone_number": doc.get("customer_id") if platform == "whatsapp" else None,
            "customer_name": doc.get("customer_name") or "Unknown",
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
            "assigned_agent": doc.get("assigned_agent", {}),
            "is_ai_enabled": doc.get("is_ai_enabled"),
            "reply_window_ends_at": doc.get("reply_window_ends_at").isoformat() if isinstance(doc.get("reply_window_ends_at"), datetime) else None,
            "assignment_history": doc.get("assignment_history", []),
            "suggestions": doc.get("suggestions", []),
            "summary": doc.get("summary", []),
            "participants": doc.get("participants", []),
            "unread": doc.get("unread", {})
        }
            
        result.append(conv_obj)

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

async def fetch_messages(
    org_id: str,
    conversation_id: str,
    agent_id: str,
    agent_username: str,
    before: Optional[str] = None,      # ISO timestamp cursor
    limit: int = 30
):
    """
    Fetch messages and private notes for a specific conversation with cursor-based pagination.

    Arguments:
    - org_id: Organization ID
    - conversation_id: Conversation ID
    - agent_id: ID of the agent
    - agent_username: Username of the agent accessing this conversation
    - before: ISO timestamp — fetch messages strictly before this time (pagination cursor)
    - limit: Max number of messages to return (default 30)

    Returns:
    - A dictionary with:
        - 'messages': list of messages and notes (oldest→newest)
        - 'assignment': assignment details
        - 'has_more': whether older messages exist beyond this page
        - 'next_cursor': ISO timestamp to pass as `before` in the next request
    """
    try:
        org_messages_collection_name = f"messages_{org_id}"
        private_note_collection_name = f"private_notes_{org_id}"
        org_conversations_collection_name = f"conversations_{org_id}"

        convo_collection = db[org_conversations_collection_name]
        now = datetime.now(timezone.utc)

        # --- Auto-assign agent if conversation is unassigned ---
        assigned_convo = convo_collection.find_one_and_update(
            {
                "conversation_id": conversation_id,
                "$or": [
                    {"assigned_agent": {"$exists": False}},
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
            projection={"assigned_agent": 1, "assignment_history": 1, "type": 1, "participants": 1, "_id": 0},
            return_document=ReturnDocument.AFTER
        )

        if assigned_convo is None:
            assigned_convo = convo_collection.find_one(
                {"conversation_id": conversation_id},
                projection={"assigned_agent": 1, "assignment_history": 1, "type": 1, "participants": 1, "_id": 0}
            )

        if not assigned_convo or assigned_convo.get("type") != "team":
            convo_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {"unread_count": 0}}
            )

        # Parse compound cursor
        time_filter: dict = {"conversation_id": conversation_id}
        last_id = None

        if before:
            try:
                # Expect cursor format: "2024-01-01T12:00:00+00:00__msg_abc123"
                if "__" in before:
                    ts_part, last_id = before.split("__", 1)
                else:
                    ts_part = before  # backwards compat

                before_dt = parser.isoparse(ts_part) 
                if before_dt.tzinfo is None:
                    before_dt = before_dt.replace(tzinfo=timezone.utc)

                if last_id:
                    # Stable compound filter: same timestamp but id not yet seen, OR strictly older
                    time_filter["$or"] = [
                        {"timestamp": {"$lt": before_dt}},
                        {
                            "timestamp": before_dt,
                            "id": {"$lt": last_id}  # lexicographic tie-break on id
                        }
                    ]
                else:
                    time_filter["timestamp"] = {"$lt": before_dt}

            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid `before` cursor")

        fetch_limit = limit + 1

        messages, notes = [], []

        if org_messages_collection_name in db.list_collection_names():
            messages = list(
                db[org_messages_collection_name]
                .find(time_filter, {"_id": 0})
                .sort([("timestamp", -1), ("id", -1)])  # ← compound sort
                .limit(fetch_limit)
            )

        if private_note_collection_name in db.list_collection_names():
            notes = list(
                db[private_note_collection_name]
                .find(time_filter, {"_id": 0})
                .sort([("timestamp", -1), ("id", -1)])  # ← compound sort
                .limit(fetch_limit)
            )

        combined = messages + notes
        combined.sort(key=lambda x: (x.get("timestamp"), x.get("id")), reverse=True)

        has_more = len(combined) > limit
        if has_more:
            combined = combined[:limit]

        # Normalize timestamps
        for msg in combined:
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                msg["timestamp"] = ts.isoformat()

        combined.reverse()  # back to chronological for frontend

        # ← Compound cursor: oldest message's timestamp + id
        if has_more and combined:
            oldest = combined[0]
            next_cursor = f"{oldest.get('timestamp')}__{oldest.get('id', '')}"
        else:
            next_cursor = None

        return {
            "messages": combined,      
            "assignment": assigned_convo,
            "has_more": has_more,
            "next_cursor": next_cursor
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching messages for {conversation_id} of org {org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch messages: {str(e)}")

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

        # await broadcast_on_stats_update(org_id)

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

async def send_message(org_id: str, platform: str, conversation_id: str, content: str, mode:str, user_full_name: Optional[str] = None, file: Optional[any] = None, temp_id: Optional[str] = None, context_type: Optional[str] = None, context: Optional[dict] = None, timestamp: Optional[str] = None):
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
            # Check if content is a JSON template
            try:
                import json
                content_data = json.loads(content)
                if isinstance(content_data, dict) and "name" in content_data and "language" in content_data:
                    recipient_id = conversation_id.replace("whatsapp_", "")
                    res = await send_whatsapp_template_message(
                        recipient_id=recipient_id,
                        template=content_data,
                        wa_id=wa_id,
                        org_id=org_id,
                        temp_id=temp_id,
                        mode=mode,
                        context_type=context_type,
                        context=context
                    )
                else:
                    raise ValueError("Not a template")
            except (json.JSONDecodeError, ValueError, TypeError):
                res = await send_whatsapp_message(
                    {
                        "conversation_id": conversation_id,
                        "content": content,
                        "sender_id": wa_id,
                    }, 
                    org_id=org_id, 
                    mode=mode, 
                    temp_id=temp_id, 
                    context_type=context_type, 
                    context=context, 
                    user_full_name=user_full_name
                )
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
    
    elif platform == "internal":
        participants = []
        if context and "participants" in context:
            participants = context["participants"]
            
        if not conversation_id or conversation_id == "new":
            if not participants or len(participants) < 2:
                raise HTTPException(status_code=400, detail="Participants required for internal chat")
            
            sorted_ids = sorted(participants)
            conversation_id = f"team_{sorted_ids[0]}_{sorted_ids[1]}"
        
        if timestamp:
            try:
                now = parser.parse(timestamp)
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                else:
                    now = now.astimezone(timezone.utc)
            except Exception:
                now = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)

        # Team conversations and messages live in their own dedicated collections
        convo_collection = db[f"team_conversations_{org_id}"]
        
        convo = convo_collection.find_one({"conversation_id": conversation_id})
        
        if not convo:
            if not participants:
                # Reconstruct participants correctly from team_userA_userB
                import re
                # Find all occurrences of 'user_' followed by alphanumeric characters
                participants = re.findall(r"user_[A-Za-z0-9]+", conversation_id)
                
                if not participants and len(conversation_id.split("_")) >= 3:
                    # Fallback for IDs that might not follow the 'user_' prefix pattern
                    participants = conversation_id.split("_")[1:]
            
            convo_doc = {
                "conversation_id": conversation_id,
                "type": "team",
                "participants": participants,
                "created_at": now,
                "updated_at": now,
                "last_message": content,
                "last_message_timestamp": now,
                "last_sender": context.get("sender_id") if context else None,
                "unread": {p: 0 for p in participants if p}
            }
            convo_collection.insert_one(convo_doc)
            convo = convo_doc
        
        # Ensure participants are populated for broadcasting
        if not participants:
            participants = convo.get("participants", [])
        
        # Self-healing: if participants list is broken (e.g. contains 'user' as a separate element)
        if participants and ("user" in participants):
            logger.info(f"Repairing broken participants list for {conversation_id}")
            import re
            reconstructed = re.findall(r"user_[A-Za-z0-9]+", conversation_id)
            
            if reconstructed:
                participants = reconstructed
                convo_collection.update_one(
                    {"conversation_id": conversation_id},
                    {
                        "$set": {
                            "participants": participants,
                            "unread": {p: 0 for p in participants if p}
                        }
                    }
                )
        
        messages_collection = db[f"team_messages_{org_id}"]
        sender_id = context.get("sender_id") if context else None
        sender_name = context.get("sender_name") if context else None
        
        msg_id = str(uuid.uuid4())
        msg_doc = {
            "id": msg_id,
            "message_id": msg_id,
            "temp_id": temp_id,
            "conversation_id": conversation_id,
            "platform": "internal",
            "type": "text",
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "role": "agent",
            "timestamp": now,
            "status": "sent"
        }
        messages_collection.insert_one(msg_doc)
        
        recipient_id = None
        if "participants" in convo:
            for p in convo["participants"]:
                if p != sender_id:
                    recipient_id = p
                    break
        
        update_query = {
            "$set": {
                "last_message": content,
                "last_message_timestamp": now,
                "last_sender": sender_id,
                "updated_at": now
            }
        }
        
        if recipient_id:
            update_query["$inc"] = {f"unread.{recipient_id}": 1}
            
        convo_collection.update_one(
            {"conversation_id": conversation_id},
            update_query
        )

        from services.utils import serialize_mongo
        broadcast_payload = serialize_mongo(msg_doc)
        await broadcast_team_message(org_id, conversation_id, participants, broadcast_payload)
        
        return {"status": "sent", "conversation_id": conversation_id, "message_id": msg_id}
    else:
        raise HTTPException(status_code=400, detail="Unsupported platform")
