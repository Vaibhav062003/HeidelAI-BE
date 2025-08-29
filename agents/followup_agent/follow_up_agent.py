from agno.agent import Agent, RunResponse
from agno.models.google import Gemini
from pydantic import BaseModel, Field

from config.settings import LLM_MODEL
from database import get_mongo_db

db = get_mongo_db()

class FollowUpAgentResponse(BaseModel):
    followup: str = Field(
        ...,
        description=(
            "A polite, professional, and context-aware follow-up message to re-engage the customer "
            "who hasn't replied. Keep it short (1-2 sentences), friendly, professional, and encourage a reply."
        )
    )
    delay: str = Field(
        ...,
        description=(
            "How long to wait before sending the follow-up message. "
            "Must be one of: '12 hours', '1 day', '2 days', '3 days', '5 days', '1 week', '2 weeks'. "
            "If the customer clearly mentioned a timeframe (e.g., 'I'll check tomorrow'), choose the closest match. "
            "If no timeframe is mentioned, default to '2 days'."
        )
    )


follow_up_agent = Agent(
    model=Gemini(id=LLM_MODEL),
    description=(
        "AI assistant that creates polite and professional follow-up messages for sales agents "
        "based on past conversations with customers who haven't replied."
    ),
    response_model=FollowUpAgentResponse,
    instructions=[
        "Review the conversation history between the sales agent and the customer.",
        "Understand the customer's last message, intent, and any pending actions.",
        "Draft one concise follow-up message from the agent to the customer.",
        "This message will be sent to the user directly without any modification or review, so don't add any placeholders.",
        "Keep it friendly, professional, and encourage a reply without being pushy.",
        "Decide how long to wait before sending the follow-up message:",
        " - If the customer mentioned a timeframe (e.g., 'I'll reply tomorrow'), pick the closest matching option.",
        " - If no timeframe is mentioned, use the default: '2 days'.",
        "Only choose from these fixed options: '12 hours', '1 day', '2 days', '3 days', '5 days', '1 week', '2 weeks'.",
        "Do not invent custom durations.",
        "Always return both the follow-up message and the chosen delay."
    ],
)


def generate_followup_message(conversation: str) -> str:
    """
    Generates a follow-up message using the follow_up_agent.

    Args:
        conversation (str): The entire conversation as a single string.

    Returns:
        FollowUpAgentResponse: An object containing the follow-up message and delay.
    """
    response: RunResponse = follow_up_agent.run(conversation)
    if isinstance(response.content, FollowUpAgentResponse):
        return response.content
    else:
        print("Error: AI response is not in the expected format.")
        return None
