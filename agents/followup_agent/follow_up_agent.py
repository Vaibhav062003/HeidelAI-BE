from agno.agent import Agent, RunResponse
from agno.models.google import Gemini
from pydantic import BaseModel, Field

from config.settings import LLM_MODEL
from database import get_mongo_db

db = get_mongo_db()

class FollowUpAgent(BaseModel):
    followup: str = Field(
        ...,
        description="A polite, professional, and context-aware follow-up message to re-engage the customer who hasn't replied. 1-2 sentences only."
    )

follow_up_agent = Agent(
    model=Gemini(id=LLM_MODEL),
    description=(
        "AI assistant that creates polite and professional follow-up messages for sales agents "
        "based on past conversations with customers who haven't replied."
    ),
    response_model=FollowUpAgent,
    instructions=[
        "Review the conversation history between the sales agent and the customer.",
        "Understand the customer's last message, intent, and any pending actions.",
        "Draft one concise follow-up message from the agent to the customer.",
        "Keep it friendly, professional, and encourage a reply without being pushy.",
        "Do not include the full chat history—just generate the follow-up."
    ],
)


def generate_followup_message(conversation: str) -> str:
    """
    Generates a follow-up message using the follow_up_agent.

    Args:
        conversation (str): The entire conversation as a single string.

    Returns:
        str: The generated follow-up message.
    """
    response: RunResponse = follow_up_agent.run(conversation)
    if isinstance(response.content, FollowUpAgent):
        return response.content.followup
    else:
        print("Error: AI response is not in the expected format.")
        return None


def get_followup(org_id: str, conversation_id: str) -> dict:
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
    followup_message = generate_followup_message(conversation_text)

    if messages:
        # Update the conversation document with the follow-up message
        return {
            "followup": followup_message,
        }
    else:
        print(f"Failed to generate follow-up message for conversation {conversation_id}")

