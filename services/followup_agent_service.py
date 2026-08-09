import asyncio

from database import get_mongo_db
from agents.followup_agent.follow_up_agent import generate_followup_message

db = get_mongo_db()

async def get_followup_message(org_id: str, conversation_id: str, context: str = "") -> dict:
    """
    Fetches the latest 30 messages for a given conversation_id, 
    generates a follow-up message, and updates the conversation document 
    with the follow-up message.

    Args:
        org_id (str): The organization ID.
        conversation_id (str): The unique ID of the conversation.
    """

    messages_collection = db[f"messages_{org_id}"]

    # Fetch messages for the given conversation_id, sorted by timestamp (ascending)
    messages = await asyncio.to_thread(
        lambda: list(messages_collection.find({"conversation_id": conversation_id}).sort("timestamp", -1).limit(30))
    )
    messages.reverse()


    # Combine message content into a single string
    conversation_text = "\n".join([f"{msg.get('role', 'unknown')}: {msg.get('content', '')}" for msg in messages])
    if context:
        conversation_text = f"{conversation_text}\nSystem context: {context}"

    # Generate the follow-up message
    followup_object = await asyncio.to_thread(generate_followup_message, conversation_text)

    if not followup_object or not messages:
        raise ValueError(f"Failed to generate follow-up message for conversation {conversation_id}")

    return followup_object


if __name__ == "__main__":
    pass
