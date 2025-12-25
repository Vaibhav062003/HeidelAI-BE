import dateparser
from bson import ObjectId
from fastapi import HTTPException
from datetime import datetime, timezone
from pymongo.errors import PyMongoError

# === Internal Modules ===
from database import get_mongo_db
from .followup_agent_service import get_followup_message
from core.logger import get_logger

logger = get_logger(__name__)

db = get_mongo_db()

async def schedule_followup_message(org_id: str, conversation_id: str):
    """
    Core business logic for scheduling a follow-up message.
    Raises HTTPException if something goes wrong, so API layer can respond properly.
    """
    try:
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found.")

        platform = conversation_id.split("_")[0]

        scheduled_messages_collection = db["scheduled_messages"]
        conversation_collection = db[f"conversations_{org_id}"]

        conversation = conversation_collection.find_one({"conversation_id": conversation_id})
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        if conversation.get("status") != "open":
            return {"message": "Follow-up can only be scheduled for open conversations."}

        receiver_id = conversation.get("customer_id")

        if platform == "whatsapp":
            sender_id = conversation.get("whatsapp_id")
        else:
            sender_id = conversation.get("instagram_id")

        if not receiver_id or not sender_id:
            raise HTTPException(
                status_code=400,
                detail="Missing sender_id or receiver_id in conversation"
            )

        # Run AI agent for follow-up
        try:
            followup_result = await get_followup_message(org_id, conversation_id)
            if callable(followup_result):  
                raise RuntimeError("get_followup_message should be awaited outside.")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Follow-up generation failed: {str(e)}")

        # Parse delay into datetime
        try:
            send_time = dateparser.parse(
                followup_result.delay,
                settings={
                    "TIMEZONE": "UTC",
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future"
                }
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Delay parsing error: {str(e)}")

        if not send_time:
            raise HTTPException(status_code=500, detail="Failed to parse follow-up delay.")

        followup_doc = {
            "org_id": org_id,
            "platform": platform,
            "conversation_id": conversation_id,
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "followup_text": followup_result.followup,
            "delay": followup_result.delay,
            "scheduled_time": send_time,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }

        # Insert follow-up + update conversation
        try:
            scheduled_messages_collection.insert_one(followup_doc)
            conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {"status": "follow-up"}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        return {"message": "Follow-up message scheduled successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

async def abort_scheduled_message(org_id: str, conversation_id: str):
    """
    Mark a scheduled follow-up message as 'aborted', meaning it won't be sent
    because the customer or the agent replied before the follow-up was sent.
    Raises HTTPException for consistent error handling.
    """
    try:
        scheduled_messages_collection = db["scheduled_messages"]

        try:
            result = scheduled_messages_collection.update_one(
                {
                    "conversation_id": conversation_id, 
                    "org_id": org_id,
                    "status": "pending" 
                },
                {"$set": {
                    "status": "aborted", 
                    "aborted_at": datetime.now(timezone.utc)
                    }
                }
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error updating scheduled message: {str(e)}")

        if result.matched_count == 0:
            return {"message": "No pending scheduled message found to abort."}
        
        return {"message": "Scheduled message marked as 'aborted' successfully."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
async def clean_expired_archives():
    """Find and clean all expired archived data entries."""
    try:
        now = datetime.now(timezone.utc)

        expired_docs = list(db.archived_tracker.find({
            "expires_at": {"$lt": now},
            "status": "active"
            }))

        if not expired_docs:
            logger.info("No expired archived data entries found.")
            return
        
        logger.success(f"Found {len(expired_docs)} expired archive records. Starting cleanup...")

        for doc in expired_docs:
            await purge_archived_data(doc)

    except Exception as e:
        logger.error(f"Error during clean_expired_archives: {str(e)}")

async def purge_archived_data(doc: dict):   
    """Delete specific archive data safely using archive_id."""
    try:
        archive_id = doc.get("archive_id")
        org_id = doc.get("org_id")
        
        archived_msgs_col = f"archived_messages_{org_id}_{archive_id}"
        archived_convs_col = f"archived_conversations_{org_id}_{archive_id}"

        deleted_counts = {}

        message_result = db[archived_msgs_col].delete_many({})
        deleted_counts["messages"] = message_result.deleted_count
        db.drop_collection(archived_msgs_col)

        conv_result = db[archived_convs_col].delete_many({})
        deleted_counts["conversations"] = conv_result.deleted_count
        db.drop_collection(archived_convs_col)

        db.archived_tracker.update_one(
            {"archive_id": doc["archive_id"]},
            {"$set": {"status": "purged", 
                      "cleaned_at": datetime.now(timezone.utc),
                      "deleted_counts": deleted_counts
                     }
            }
        )

        logger.success(
            f"org_id={org_id}, platform={doc['platform']}: "
            f"deleted {deleted_counts['messages']} messages and "
            f"{deleted_counts['conversations']} conversations (marked as purged)."
        )
        
    except Exception as e:
         logger.error(f"Error purging archive for org_id={org_id}, platform={doc['platform']}: {str(e)}")