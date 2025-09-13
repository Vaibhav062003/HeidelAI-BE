from database import get_mongo_db
from datetime import datetime, timezone

db = get_mongo_db()

curr_time = datetime.now(timezone.utc)

# conversation_collection.create_index("last_message_timestamp")

# print(conversation_collection.index_information())

conversation_collection = db["conversations_org_2zV2OOhodLocEnTybzR8t9MIYCE"]

projection = {
    "conversation_id": 1,
    "platform": 1,
    "customer_username": 1,
    "customer_name": 1,
    "last_message": 1,
    "last_message_timestamp": 1,
    "unread_count": 1,
    "is_ai_enabled": 1,
    "assigned_agent_id": 1,
    "priority": 1,
    "sentiment": 1,
    "query": 1,
    "_id": 0
}

raw_conversations = list(conversation_collection.find(
    {},
    projection,  
).sort("last_message_timestamp", -1))

def format_conversation(conv):
    timestamp = conv.get("last_message_timestamp")
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat()
    return {
        "id": conv.get("conversation_id"),
        "platform": conv.get("platform"),
        "customer_id": conv.get("customer_username"),
        "customer_name": conv.get("customer_name", ""),
        "last_message": conv.get("last_message", ""),
        "timestamp": timestamp,
        "unread_count": conv.get("unread_count", 0),
        "is_ai_enabled": conv.get("is_ai_enabled", True),
        "assigned_agent_id": conv.get("assigned_agent_id"),
        "priority": conv.get("priority", "medium"),
        "sentiment": conv.get("sentiment", "neutral"),
        "query": conv.get("query", ""),
    }

formatted_conversations = [format_conversation(conv) for conv in raw_conversations]

end_time = datetime.now(timezone.utc)

print(len(formatted_conversations))

print(f"Time taken: {(end_time - curr_time).total_seconds()} seconds")
