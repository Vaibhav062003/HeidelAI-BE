from database import get_mongo_db
from agents.followup_agent.follow_up_agent import generate_followup_message

db = get_mongo_db()

def get_followup_message(org_id: str, conversation_id: str) -> dict:
    """
    Fetches the latest 30 messages for a given conversation_id, 
    generates a follow-up message, and updates the conversation document 
    with the follow-up message.

    Args:
        org_id (str): The organization ID.
        conversation_id (str): The unique ID of the conversation.
    """

    messages_collection = db[f"messages_{org_id}"]

    # Fetch messages for the given conversation_id, sorted by timestamp (descending)
    messages = list(messages_collection.find({"conversation_id": conversation_id}).sort("timestamp", -1).limit(30))

    # Reverse to maintain chronological order
    messages.reverse()

    # Combine message content into a single string
    conversation_text = "\n".join([f"{msg["role"]}: {msg["content"]}" for msg in messages])

    # Generate the follow-up message
    followup_object = generate_followup_message(conversation_text)

    if messages:
        # Update the conversation document with the follow-up message
        return followup_object
    else:
        print(f"Failed to generate follow-up message for conversation {conversation_id}")


if __name__ == "__main__":
    pass