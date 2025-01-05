from datetime import datetime
from typing import List
from rich.pretty import pprint
from pydantic import BaseModel, Field
from phi.agent import Agent, RunResponse
from phi.model.google import Gemini
import json
from pydantic import ValidationError
from dotenv import load_dotenv
import os

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

class ConversationSummary(BaseModel):
    summary: List[str] = Field(
        ..., description="A concise summary of the conversation, limited to 2-8 bullet points."
    )
    priority: str = Field(
        ..., description="Priority level of the customer: 'low', 'medium', or 'high'."
    )


# Agent that uses structured outputs for conversation analysis
chat_analysis_agent = Agent(
    model=Gemini(id="gemini-1.5-flash"),  
    description="You are a customer service assistant. Summarize conversations and assign a priority level.",
    response_model=ConversationSummary,
    # structured_outputs=True,
    instructions=[
        # "Provide the output strictly in JSON format.",
        "Ensure the 'priority' field contains only one of the following values: 'low', 'medium', or 'high'.",
        # "Do not include any additional text or formatting outside of the JSON structure."
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
    # Check if the response is valid and contains a parsed content
    if isinstance(response.content, ConversationSummary):
        return response.content
    else:
        print("Error: AI response is not in the expected format.")
        return None


# Example conversation
example_conversation = """
Customer: laptops
AI: Sure! Are you looking for a specific brand or have any price range in mind?
Customer: I need a mid-range laptop under $1500 with good graphics performance.
AI: I recommend the XYZ Gaming Pro Laptop with RTX 3060, 16GB RAM, and a 512GB SSD. It's $1400.
Customer: That sounds great. Is it in stock, and what about the warranty?
AI: Yes, it is in stock, and it comes with a 1-year warranty.
Customer: Awesome, I'll think about it and get back to you.
"""

# Analyze the conversation
result = analyze_conversation(example_conversation)
pprint(result.summary)
pprint(result.priority)


# def assign_priority(messages):
#     pass

# def generate_summary(messages):
#     pass

# def run_analysis(chat_id, chat):
#     """
#     Runs the priority tagging and summary generation for the chat.
#     """
#     # Perform priority tagging and summary generation
#     priority = assign_priority(chat["messages"])
#     summary = generate_summary(chat["messages"])

#     # Update MongoDB with priority, summary, and reset counter
#     db.chats.update_one(
#         {"_id": chat_id},
#         {
#             "$set": {
#                 "priority": priority,
#                 "ai_summary": summary,
#                 "message_counter": 0,  # Reset the counter
#                 "last_tagged": datetime.now()
#             }
#         }
#     )

# def add_message(chat_id, role, content):
#     """
#     Adds a message to the conversation and triggers analysis if counter reaches threshold.
#     """
#     # Fetch the chat document
#     chat = db.chats.find_one({"_id": chat_id})
#     if not chat:
#         raise ValueError("Chat not found!")

#     # Add the new message
#     db.chats.update_one(
#         {"_id": chat_id},
#         {"$push": {"messages": {"role": role, "content": content}}}
#     )

#     # Increment the message counter
#     new_counter = chat.get("message_counter", 0) + 1
#     db.chats.update_one({"_id": chat_id}, {"$set": {"message_counter": new_counter}})

#     # Check if counter reached threshold
#     if new_counter >= 10:
#         run_analysis(chat_id, chat)
