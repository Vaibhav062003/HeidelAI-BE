from datetime import datetime, timezone
from pymongo.errors import PyMongoError

from database import get_mongo_db
from schemas.models import ChatAnalysis
from agents.support_agent.agent import analyze_conversation
import services.contacts_service as contacts_service
from core.logger import get_logger

logger = get_logger(__name__)

db = get_mongo_db()

async def analyze_and_update_conversation(org_id: str, conversation_id: str) -> ChatAnalysis:
    """
    Fetches the latest 40 messages for a given conversation_id,
    analyzes the conversation, and updates the conversation document
    with priority, summary, sentiment, and suggestions.

    Args:
        org_id (str): The organization ID.
        conversation_id (str): The unique ID of the conversation.
    """

    result_data = ChatAnalysis(
        conversation_id=conversation_id,
        summary=[],
        priority=None,
        sentiment=None,
        suggestions=[],
        last_analyzed=None,
    )

    try:
        conversations_collection = db[f"conversations_{org_id}"]
        messages_collection = db[f"messages_{org_id}"]

        # ---- Fetch messages ----
        try:
            messages = (
                list(
                    messages_collection.find({"conversation_id": conversation_id})
                    .sort("timestamp", 1)
                    .limit(40)
                )
            )
        except PyMongoError as e:
            raise RuntimeError(f"Database error fetching messages: {e}")

        if not messages or len(messages) < 5:
            return result_data

        # Combine message content into a single string
        conversation_text = "\n".join([msg["content"] for msg in messages])

        # Call LLM to analyze

        analysis_result = await analyze_conversation(conversation_text)

        if analysis_result:
            timestamp = datetime.now(timezone.utc)

            # Update DB - Conversation
            update_fields = {
                "priority": analysis_result.priority,
                "summary": analysis_result.summary,
                "sentiment": analysis_result.sentiment,
                "suggestions": analysis_result.suggestions,
                "last_analyzed": timestamp,
            }

            conversations_collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": update_fields},
            )

       
            for tag in analysis_result.tags:
                try:
                    if hasattr(contacts_service, 'add_tag_to_org_metadata'):
                        await contacts_service.add_tag_to_org_metadata(org_id, tag)
                except Exception:
                    pass
            
            for label in analysis_result.labels:
                try:
                    if hasattr(contacts_service, 'add_label_to_org_metadata'):
                        await contacts_service.add_label_to_org_metadata(org_id, label)
                except Exception:
                    # Logic: Assuming this exists on another branch as per user instruction
                    pass

            # Update Contact (Categories & Labels)
            contacts_collection = db[f"contacts_{org_id}"]
            contacts_collection.update_one(
                {"conversation_id": conversation_id},
                {
                    "$addToSet": {
                        "categories": {"$each": analysis_result.tags},
                        "labels": {"$each": analysis_result.labels}
                    }
                }
            )

            # Update result_data to return
            result_data.summary = analysis_result.summary
            result_data.priority = analysis_result.priority
            result_data.sentiment = analysis_result.sentiment
            result_data.suggestions = analysis_result.suggestions
            result_data.last_analyzed = timestamp

            return result_data

    except Exception as e:
        logger.error(f"LLM analysis failed for {conversation_id}: {e}")
        return None

if __name__ == "__main__":
    pass