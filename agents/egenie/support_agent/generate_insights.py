import os
from typing import List
from dotenv import load_dotenv
from agno.models.google import Gemini
from pydantic import BaseModel, Field
from agno.agent import Agent, RunResponse
from pymongo import MongoClient

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
        ..., description="Priority level of the customer: 'low', 'medium', or 'high'.",
        example="high"
    )
    query: str = Field(
        ..., description="The primary product mentioned in the conversation. This should be a concise phrase representing the latest product the user is inquiring about, ensuring it reflects the most relevant item if multiple products have been discussed. If no product is mentioned, use 'None'."
    )
    sentiment: str = Field(
        ..., description="The sentiment of the conversation: 'positive', 'neutral, or 'negative'.",
        example="positive"
    )
    suggestions: List[str] = Field(
        ..., description="A list of exactly 3 actionable suggestions based on the conversation. Each suggestion should be a concise bullet point aimed at improving response quality, sales effectiveness, or customer satisfaction."
    )
    categories: List[str] = Field(
        ...,
        description=(
            "A list of product categories the user is interested in. "
            "Allowed values (case-insensitive): "
            "['Custom Built Pc', 'Accessories', 'Gaming Laptop', 'Business Laptop']."
        ),
        example=["Gaming Laptop", "Accessories"]
    )

# Agent that uses structured outputs for conversation analysis
chat_analysis_agent = Agent(
    model=Gemini(id=LLM_MODEL),  
    description="You are a customer service assistant. Summarize conversations and assign a priority level.",
    response_model=ConversationSummary,
    instructions=[
        "Ensure the 'priority' field contains only one of the following values: 'low', 'medium', or 'high'.",
        "Extract the latest mentioned product as 'query', this will be used to search a vector db so generate the query according to the product.",
        "Summarize the chat in 2-5 bullet points, keeping it concise and relevant.",
        "Populate the 'categories' field with one or more relevant product categories the user is interested in. ",
        "Allowed values are exactly: 'custom_built_pc', 'accessories', 'gaming_laptop', 'business_laptop'. ",
        "If none apply, return an empty list."
    ],
)

# Function to analyze a conversation
def analyze_conversation(conversation: str) -> ConversationSummary:
    """
    Analyzes the given conversation to generate a summary and priority level.

    Args:
        conversation (str): The entire conversation as a single string.

    Returns:
        ConversationSummary: The summary and priority level.
    """
    response: RunResponse = chat_analysis_agent.run(conversation)
    print(type(response))
    if isinstance(response.content, ConversationSummary):
        return response.content
    else:
        print("Error: AI response is not in the expected format.")
        return None

def process_and_update_conversation(org_id: str, conversation_id: str):
    """
    Fetches the latest 30 messages for a given conversation_id, 
    analyzes the conversation, and updates the conversation document 
    with priority, summary, and query.

    Args:
        conversation_id (str): The unique ID of the conversation.
    """
    conversations_collection = db[f"conversations_{org_id}"]
    messages_collection = db[f"messages_{org_id}"]

    # Fetch messages for the given conversation_id, sorted by timestamp (descending)
    messages = list(messages_collection.find({"conversation_id": conversation_id}).sort("timestamp", -1).limit(30))

    # Reverse to maintain chronological order
    messages.reverse()

    # Combine message content into a single string
    conversation_text = "\n".join([msg["content"] for msg in messages])

    # Analyze the conversation
    analysis_result = analyze_conversation(conversation_text)

    if analysis_result:
        # Update the conversation document
        update_fields = {
            "priority": analysis_result.priority,
            "summary": analysis_result.summary,
            "query": analysis_result.query,
            "sentiment": analysis_result.sentiment,
            "suggestions": analysis_result.suggestions,
            "categories": analysis_result.categories
        }

        conversations_collection.update_one(
            {"id": conversation_id},
            {"$set": update_fields}
        )

        print(f"Updated conversation {conversation_id} with priority: {analysis_result.priority}, query: {analysis_result.query}")
    else:
        print(f"Failed to analyze conversation {conversation_id}")

if __name__ == "__main__":
    pass