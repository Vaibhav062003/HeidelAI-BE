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
        tags=[],
        labels=[],
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

        # Fetch existing tags and labels from org metadata
        available_tags = await contacts_service.get_tags_from_org_metadata(org_id)
        available_labels = await contacts_service.get_labels_from_org_metadata(org_id)
        # logger.info(f"Available tags: {available_tags}, Available labels: {available_labels}")

        # Call LLM to analyze (constrained to existing tags/labels)
        analysis_result = await analyze_conversation(conversation_text, available_tags, available_labels)
        logger.info(f"Analysis result: {analysis_result}")

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

            # Filter tags/labels to only include values from org metadata
            if available_tags:
                analysis_result.tags = [t for t in analysis_result.tags if t in available_tags]
            else:
                analysis_result.tags = []

            if available_labels:
                analysis_result.labels = [l for l in analysis_result.labels if l in available_labels]
            else:
                analysis_result.labels = []

            # Update Contact (Categories & Labels)
            contacts_collection = db[f"contacts_{org_id}"]
            # logger.info(f"contact before update: {contacts_collection.find_one({'conversation_id': conversation_id}).get('categories')}")
            try:
                result = contacts_collection.update_one(
                    {"conversation_id": conversation_id},
                    {
                        "$addToSet": {
                            "categories": {"$each": analysis_result.tags},
                            "labels": {"$each": analysis_result.labels}
                        }
                    }
                )
                # logger.info(f"contact after update: {contacts_collection.find_one({'conversation_id': conversation_id}).get('categories')}")
                
                if result.matched_count == 0:
                    logger.warning(f"No contact found for conversation_id: {conversation_id}")
            except PyMongoError as e:
                logger.error(f"Failed to update contact categories/labels for {conversation_id}: {e}")
                raise
            
            # Update result_data to return
            result_data.summary = analysis_result.summary
            result_data.priority = analysis_result.priority
            result_data.sentiment = analysis_result.sentiment
            result_data.suggestions = analysis_result.suggestions
            result_data.last_analyzed = timestamp
            result_data.tags = analysis_result.tags
            result_data.labels = analysis_result.labels

            return result_data

    except Exception as e:
        logger.error(f"LLM analysis failed for {conversation_id}: {e}")
        return None

if __name__ == "__main__":
    pass