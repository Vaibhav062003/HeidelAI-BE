import os
from typing import List, Optional
from dotenv import load_dotenv
from agno.models.google import Gemini
from pydantic import BaseModel, Field
from agno.agent import Agent, RunResponse
from pymongo import MongoClient
from core.logger import get_logger

logger = get_logger(__name__)

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")

mongo_client = MongoClient(MONGODB_URI)
db = mongo_client['Cluster0']

class ConversationSummary(BaseModel):
    summary: List[str] = Field(
        ..., description="A concise summary of the conversation, limited to 2-5 bullet points."
    )
    priority: str = Field(
        ..., 
        description="Priority level of the customer: 'low', 'medium', or 'high'.",
        example="high"
    )
    sentiment: str = Field(
        ..., description="The sentiment of the conversation: 'positive', 'neutral', or 'negative'.",
        example="positive"
    )
    suggestions: List[str] = Field(
        ..., description=(
            "A list of exactly 3 actionable recommendations for the sales agent to improve this specific conversation. "
            "Each suggestion should be a direct, practical action the agent can take, such as asking a follow-up question, highlighting a product feature, or addressing customer concerns. "
            "Avoid generic advice or technical implementation details."
        )
    )


# Agent that uses structured outputs for conversation analysis
chat_analysis_agent = Agent(
    model=Gemini(id=LLM_MODEL),  
    description="You are a customer service assistant. Summarize conversations and assign a priority level.",
    response_model=ConversationSummary,
    instructions=[
        "Summarize the chat in 2-5 bullet points, keeping it concise and relevant.",
        "Ensure the 'priority' field contains only one of the following values: 'low', 'medium', or 'high' - case sensitive.",
        "The priority should reflect the urgency to buy from the customer, or if they show intent to purchase soon.",
        "Analyze the overall sentiment of the conversation and set the 'sentiment' field to 'positive', 'neutral', or 'negative' - case sensitive.",
        "Provide exactly 3 actionable suggestions in the 'suggestions' field, each as a concise bullet point."
    ],
)

# Function to analyze a conversation
async def analyze_conversation(conversation: str) -> Optional[ConversationSummary]:
    """
    Analyzes the given conversation to generate a summary and priority level.

    Args:
        conversation (str): The entire conversation as a single string.

    Returns:
        ConversationSummary: The summary and priority level.
    """

    response: RunResponse = await chat_analysis_agent.arun(conversation)
    # print(type(response))
    if isinstance(response.content, ConversationSummary):
        return response.content

    logger.error("LLM Error: " + str(response))
    raise RuntimeError("AI response is not in expected format")

if __name__ == "__main__":
    pass