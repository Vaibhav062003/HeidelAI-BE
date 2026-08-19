import os
import json
from typing import Optional, Literal
from dotenv import load_dotenv
from agno.models.azure import AzureOpenAI
from config.settings import AZURE_MODEL
from pydantic import BaseModel, Field
from agno.agent import Agent, RunResponse
from loguru import logger

load_dotenv()

class PurchaseIntentResult(BaseModel):
    has_intent: bool = Field(..., description="Whether the customer expressed intent to purchase at a specific time")
    product_name: Optional[str] = Field(None, description="Extracted product name (None if no intent)")
    tentative_date: Optional[str] = Field(None, description="ISO 8601 date string extracted from conversation (None if no intent)")
    confidence: Literal["low", "medium", "high"] = Field(..., description="Confidence level")
    reasoning: str = Field(..., description="Brief explanation of why intent was/wasn't detected")

# Agent that extracts purchase intent using structured outputs
intent_extraction_agent = Agent(
    model=AzureOpenAI(id=AZURE_MODEL),
    description="You are an AI agent specialized in extracting purchase intent from customer conversations.",
    response_model=PurchaseIntentResult,
    instructions=[
        "Analyze the conversation for purchase intent with a specific timeline.",
        "Extract the product name from what was discussed/shared in the chat.",
        "Parse relative dates ('next Friday', 'tomorrow', 'in 2 weeks') into ISO 8601 format.",
        "Parse same-day times ('at 5pm', 'by evening') into ISO 8601 datetime format.",
        "Only set has_intent=True if the customer mentions BOTH a product AND a time/date.",
        "Return has_intent=False if the date is vague ('someday', 'maybe later').",
        "The current date/time will be provided in the prompt for relative date resolution.",
    ],
)

async def extract_purchase_intent(conversation_text: str, current_datetime_iso: str) -> Optional[PurchaseIntentResult]:
    """
    Analyzes the conversation to extract purchase intent and related details.

    Args:
        conversation_text (str): The conversation to analyze.
        current_datetime_iso (str): The current date and time in ISO 8601 format.

    Returns:
        Optional[PurchaseIntentResult]: The structured purchase intent result, or None if error.
    """
    prompt = json.dumps(
        {
            "conversation_text": conversation_text,
            "current_datetime_iso": current_datetime_iso,
        },
        separators=(",", ":"),
    )

    try:
        response: RunResponse = await intent_extraction_agent.arun(prompt)
        if isinstance(response.content, PurchaseIntentResult):
            return response.content

        logger.error("LLM Error: AI response is not in expected format. Response: " + str(response))
        return None
    except Exception as e:
        logger.error(f"Error extracting purchase intent: {e}")
        return None

if __name__ == "__main__":
    pass
