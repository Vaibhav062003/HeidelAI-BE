import dateparser
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException
from datetime import datetime, timezone
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

# === Internal Modules ===
from database import get_mongo_db
from .followup_agent_service import get_followup_message
from services.utils import serialize_mongo
from loguru import logger

db = get_mongo_db()

async def schedule_followup_message(
    org_id: str,
    conversation_id: str,
    source: str = "manual",
    quote_id: str | None = None,
    requested_by: str | None = None,
    requested_by_name: str | None = None,
):
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

        existing = scheduled_messages_collection.find_one({
            "org_id": org_id,
            "conversation_id": conversation_id,
            "status": "pending",
        })
        if existing and source != "quotation":
            return {"message": "A follow-up is already scheduled.", "followup": serialize_mongo(existing)}

        if conversation.get("status") != "open" and not existing:
            return {"message": "Follow-up can only be scheduled for open conversations."}

        quote = None
        if quote_id:
            quote = db[f"quotes_{org_id}"].find_one({
                "org_id": org_id,
                "conversation_id": conversation_id,
                "quote_id": quote_id,
            })
            if not quote:
                raise HTTPException(status_code=404, detail="Quote not found for this conversation.")
        else:
            quote = db[f"quotes_{org_id}"].find_one(
                {"org_id": org_id, "conversation_id": conversation_id},
                sort=[("created_at", -1)],
            )

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
            quote_context = ""
            if quote and source == "quotation":
                quote_context = (
                    f"Quotation {quote.get('quote_number', quote_id)} with a total of "
                    f"INR {quote.get('public_grand_total', 0):,.0f} was just shared with the customer."
                )
            followup_result = await get_followup_message(org_id, conversation_id, quote_context)
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
            "source": source,
            "quote_id": quote_id or (quote.get("quote_id") if quote else None),
            "quote_number": quote.get("quote_number") if quote else None,
            "quote_value": quote.get("public_grand_total") if quote else None,
            "created_by": requested_by,
            "created_by_name": requested_by_name or "Team member",
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        # Insert follow-up + update conversation
        try:
            if existing:
                followup_doc["created_at"] = existing.get("created_at", followup_doc["created_at"])
                followup_doc = scheduled_messages_collection.find_one_and_update(
                    {"_id": existing["_id"], "org_id": org_id, "status": "pending"},
                    {"$set": followup_doc},
                    return_document=ReturnDocument.AFTER,
                )
                if not followup_doc:
                    raise HTTPException(status_code=409, detail="The existing follow-up changed. Try sharing the quotation again.")
            else:
                result = scheduled_messages_collection.insert_one(followup_doc)
                followup_doc["_id"] = result.inserted_id
            conversation_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {
                    "status": "follow-up",
                }}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        # try:
        #     await broadcast_on_stats_update(org_id)
        # except Exception as e:
        #     logger.warning(f"Failed to broadcast dashboard stats: {str(e)}")
        return {"message": "Follow-up message scheduled successfully.", "followup": serialize_mongo(followup_doc)}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


def list_scheduled_followups(org_id: str, limit: int = 200) -> list[dict]:
    followups = list(
        db["scheduled_messages"].find({"org_id": org_id}).sort("created_at", -1).limit(limit)
    )
    conversation_ids = list({item.get("conversation_id") for item in followups if item.get("conversation_id")})
    conversations = {
        item["conversation_id"]: item
        for item in db[f"conversations_{org_id}"].find(
            {"conversation_id": {"$in": conversation_ids}},
            {"_id": 0, "conversation_id": 1, "customer_name": 1, "customer_id": 1, "platform": 1},
        )
    }
    latest_quotes = {}
    if conversation_ids:
        for quote in db[f"quotes_{org_id}"].aggregate([
            {"$match": {"org_id": org_id, "conversation_id": {"$in": conversation_ids}}},
            {"$sort": {"created_at": -1}},
            {"$group": {"_id": "$conversation_id", "quote": {"$first": "$ROOT"}}},
        ]):
            latest_quotes[quote["_id"]] = quote["quote"]

    enriched = []
    for followup in followups:
        conversation = conversations.get(followup.get("conversation_id"), {})
        quote = latest_quotes.get(followup.get("conversation_id"), {})
        customer = quote.get("customer") or {}
        enriched.append({
            **followup,
            "customer_name": conversation.get("customer_name") or customer.get("name") or "Customer",
            "customer_phone": customer.get("phone") or conversation.get("customer_id") or followup.get("receiver_id") or "",
            "platform": followup.get("platform") or conversation.get("platform") or "whatsapp",
            "quote_number": followup.get("quote_number") or quote.get("quote_number"),
            "quote_value": followup.get("quote_value") if followup.get("quote_value") is not None else quote.get("public_grand_total"),
            "taken_at": followup.get("fulfilled_at") or followup.get("sent_at") or followup.get("cancelled_at") or followup.get("aborted_at"),
        })
    return serialize_mongo(enriched)


def transition_scheduled_followup(
    org_id: str,
    followup_id: str,
    status: str,
    user_id: str,
    user_name: str,
) -> dict:
    try:
        object_id = ObjectId(followup_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Follow-up not found.")

    current = db["scheduled_messages"].find_one({"_id": object_id, "org_id": org_id})
    if not current:
        raise HTTPException(status_code=404, detail="Follow-up not found.")

    allowed_from = {"cancelled": {"pending", "failed"}, "fulfilled": {"pending", "sent", "failed"}}
    if current.get("status") == status:
        return serialize_mongo(current)
    if current.get("status") not in allowed_from[status]:
        raise HTTPException(status_code=409, detail="This follow-up is already closed and cannot be changed.")

    now = datetime.now(timezone.utc)
    updated = db["scheduled_messages"].find_one_and_update(
        {"_id": object_id, "org_id": org_id, "status": current["status"]},
        {"$set": {
            "status": status,
            f"{status}_at": now,
            "updated_at": now,
            "resolved_by": user_id,
            "resolved_by_name": user_name or "Team member",
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="The follow-up changed while you were updating it. Refresh and try again.")

    conversation_id = current.get("conversation_id")
    if conversation_id and not db["scheduled_messages"].find_one({
        "org_id": org_id,
        "conversation_id": conversation_id,
        "status": "pending",
    }):
        db[f"conversations_{org_id}"].update_one(
            {"conversation_id": conversation_id, "status": "follow-up"},
            {"$set": {"status": "open"}},
        )
    return serialize_mongo(updated)

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
        
        # try:
        #     await broadcast_on_stats_update(org_id)
        # except Exception as e:
        #     logger.warning(f"Failed to broadcast stats after abort: {str(e)}")

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
