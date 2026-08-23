import asyncio
import json

from database import get_async_mongo_db
from agents.followup_agent.follow_up_agent import analyze_followup_reply, generate_followup_message

db = get_async_mongo_db()

async def get_followup_message(org_id: str, conversation_id: str, context: str = "") -> dict:
    """
    Fetches the latest 30 messages for a given conversation_id, 
    generates a follow-up message, and updates the conversation document 
    with the follow-up message.

    Args:
        org_id (str): The organization ID.
        conversation_id (str): The unique ID of the conversation.
    """

    messages = await db[f"messages_{org_id}"].find(
        {"conversation_id": conversation_id},
        {"_id": 0, "role": 1, "content": 1, "timestamp": 1},
    ).sort("timestamp", -1).limit(30).to_list(length=30)
    messages.reverse()

    if not messages:
        raise ValueError(f"No messages found for conversation {conversation_id}")

    transcript = [
        {
            "role": str(message.get("role", "unknown")),
            "content": str(message.get("content", ""))[:2000],
        }
        for message in messages
    ]
    conversation_text = json.dumps(
        {"trusted_context": context[:500], "untrusted_transcript": transcript},
        ensure_ascii=False,
    )

    # Generate the follow-up message
    followup_object = await asyncio.to_thread(generate_followup_message, conversation_text)

    if not followup_object:
        raise ValueError(f"Failed to generate follow-up message for conversation {conversation_id}")

    timeline = followup_object.timeline_expression
    customer_text = "\n".join(item["content"] for item in transcript if item["role"] == "customer")
    if timeline and timeline.casefold() not in customer_text.casefold():
        followup_object.timeline_expression = None

    return followup_object


async def analyze_customer_followup_reply(content: str):
    analysis = await asyncio.to_thread(
        analyze_followup_reply,
        json.dumps({"untrusted_customer_reply": content[:2000]}, ensure_ascii=False),
    )
    timeline = analysis.timeline_expression
    if timeline and timeline.casefold() not in content.casefold():
        analysis.outcome = "other"
        analysis.timeline_expression = None
        analysis.reason = "The model did not return a verbatim customer timeline."
    return analysis


if __name__ == "__main__":
    pass
