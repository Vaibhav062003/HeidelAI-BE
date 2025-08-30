# === Third-party Modules ===
import dateparser
from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends

# === Internal Modules ===
from database import get_mongo_db
from services.followup_agent_service import get_followup_message
from schemas.models import ChatbotPromptUpdate
from auth.dependencies import get_current_user
from services.ai_chatbot import update_prompt_in_cache

db = get_mongo_db()

router = APIRouter(prefix="/api/ai", tags=["AI Routes"])

@router.get("/product-recommendations/{conversation_id}")
async def get_product_recommendations(conversation_id: str, user=Depends(get_current_user)):
    """
    This endpoint retrieves product recommendations for a specific conversation.
    """
    try:
        org_id = user.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID missing in user context")

        conversation_col_name = f"conversations_{org_id}"
        conversations_collection = db[conversation_col_name]

        try:
            conversation = conversations_collection.find_one({"id": conversation_id})
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if conversation:
            products = conversation.get("product_recommendations")
            if products:
                return {"status": "success", "data": products}
            return {"status": "error", "message": "No product recommendations found"}

        return {"status": "error", "message": "No conversation found"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")


@router.put("/chatbot-prompt")
def update_chatbot_prompt(payload: ChatbotPromptUpdate, user=Depends(get_current_user)):
    """
    Update the chatbot prompt for the authenticated user's organization.
    """
    try:
        org_id = user.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found in user context.")

        try:
            result = db.organizations.update_one(
                {"org_id": org_id},
                {"$set": {"chatbot_prompt": payload.chatbot_prompt}}
            )
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Organization not found in MongoDB.")

        try:
            update_prompt_in_cache(org_id, payload.chatbot_prompt)
        except Exception as e:
            # Cache update failure should not break the API
            raise HTTPException(status_code=500, detail=f"Failed to update cache: {str(e)}")

        return {"status": "success", "message": "Chatbot prompt updated successfully."}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

@router.post("/generate-reminder/{conversation_id}")
async def generate_reminder(conversation_id: str, user=Depends(get_current_user)):
    """
    Generate and store a reminder message for a given conversation.
    """
    try:
        org_id = user.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found.")

        scheduled_messages_collection = db["scheduled_messages"]

        platform = conversation_id.split("_")[0]

        conversation_collection = db[f"conversations_{org_id}"]

        conversation = conversation_collection.find_one({"id": conversation_id}, {"customer_id": 1})

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found.")

        receiver_id = conversation.get("customer_id")
        sender_id = conversation.get("instagram_id")

        # Running through LLM reminder agent
        try:
            reminder_result = await get_followup_message(org_id, conversation_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Reminder generation failed: {str(e)}")

        # Converting delay (natural language) → ISO datetime
        try:
            send_time = dateparser.parse(
                reminder_result.delay,
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

        iso_time = send_time.isoformat()

        reminder_doc = {
            "org_id": org_id,
            "platform": platform,
            "conversation_id": conversation_id,
            "followup_text": reminder_result.followup,
            "delay": reminder_result.delay,
            "scheduled_time": iso_time,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }

        # Store reminder
        try:
            scheduled_messages_collection.insert_one(reminder_doc)
        except PyMongoError as e:
            raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

        return {"status": "success", "data": reminder_doc}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

# @router.post("/process-scheduled-messages")
# async def process_scheduled_messages():
#     """
#     Process scheduled messages that are due and trigger send function.
#     Optimized with indexed query + bulk update.
#     """
#     now = datetime.now(timezone.utc)

#     # Query only pending messages that are due
#     cursor = db.scheduled_messages.find(
#         {"send_time": {"$lte": now}, "status": "pending"}
#     ).sort("send_time", 1)

#     updates = []
#     processed = []
#     failed = []

#     # Collect update operations
#     async for doc in cursor:
#         try:
#             # Trigger send function
#             await services.send_message(
#                 org_id=doc["org_id"],
#                 conversation_id=doc["conversation_id"],
#                 message=doc["message"]
#             )
#             # Prepare update for sent status
#             updates.append(
#                 UpdateOne({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": now}})
#             )
#             processed.append(str(doc["_id"]))
#         except Exception as e:
#             updates.append(
#                 UpdateOne({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e)}})
#             )
#             failed.append(str(doc["_id"]))

#     # Execute all updates in one batch
#     if updates:
#         db.scheduled_messages.bulk_write(updates)

#     return {
#         "status": "success",
#         "processed": processed,
#         "failed": failed,
#         "count": len(processed) + len(failed)
#     }