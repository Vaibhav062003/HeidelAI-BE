import os
from typing import List, Optional
from dotenv import load_dotenv
from agno.models.moonshot import MoonShot
from config.settings import MOONSHOT_API_KEY, MOONSHOT_ENDPOINT, MOONSHOT_DEPLOYMENT_NAME
from pydantic import BaseModel, Field
from agno.agent import Agent, RunResponse
from loguru import logger
import json

load_dotenv()

# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# LLM_MODEL = os.getenv("LLM_MODEL")

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
    tags: List[str] = Field(
        default_factory=list, description="A list of 1-2 tags selected from the provided available tags list. Only use tags from the provided list. Return empty list if no available tags are provided.",
        example=["product_inquiry", "macbook"]
    )
    labels: List[str] = Field(
        default_factory=list, description="A list of 1-2 labels selected from the provided available labels list. Only use labels from the provided list. Return empty list if no available labels are provided.",
        example=["Leads"]
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
    model=MoonShot(
        id=MOONSHOT_DEPLOYMENT_NAME or "kimi-k2-thinking",
        api_key=MOONSHOT_API_KEY,
        base_url=MOONSHOT_ENDPOINT,
    ),
    description="You are a customer service assistant. Summarize conversations and assign a priority level and relevant tags/labels.",
    output_schema=ConversationSummary,
    instructions=[
        "Summarize the chat in 2-5 bullet points, keeping it concise and relevant.",
        "Ensure the 'priority' field contains only one of the following values: 'low', 'medium', or 'high' - case sensitive.",
        "The priority should reflect the urgency to buy from the customer, or if they show intent to purchase soon.",
        "Analyze the overall sentiment of the conversation and set the 'sentiment' field to 'positive', 'neutral', or 'negative' - case sensitive.",
        "Provide exactly 3 actionable suggestions in the 'suggestions' field, each as a concise bullet point.",
        "For tags and labels: You MUST ONLY select from the available tags/labels list provided in the conversation input. Do NOT create or invent new tags or labels.",
        "Pick 1-2 tags and 1-2 labels that best match the conversation from the provided lists. if there is no match found in the provided list, strictly return empty array for tags and labels.",
        "For labels: analyze the conversation's context and intent, then select the single best-matching label from the provided available labels list. Do NOT use any label that is not in the provided list. If no label in the provided list fits, return an empty array.",    ],
)

# Function to analyze a conversation
async def analyze_conversation(conversation: str, available_tags: list = None, available_labels: list = None) -> Optional[ConversationSummary]:
    """
    Analyzes the given conversation to generate a summary and priority level.

    Args:
        conversation (str): The entire conversation as a single string.
        available_tags (list): Predefined tags from org metadata. LLM must pick only from these.
        available_labels (list): Predefined labels from org metadata. LLM must pick only from these.

    Returns:
        ConversationSummary: The summary and priority level.
    """

    prompt = json.dumps(
        {
            "conversation": conversation,
            "available_tags": available_tags or [],
            "available_labels": available_labels or [],
        },
        separators=(",", ":"),
    )

    response: RunResponse = await chat_analysis_agent.arun(prompt)
    if isinstance(response.content, ConversationSummary):
        return response.content

    logger.error("LLM Error: " + str(response))
    raise RuntimeError("AI response is not in expected format")

if __name__ == "__main__":
    pass