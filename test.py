from database import get_mongo_db
from datetime import datetime, timezone

db = get_mongo_db()

# curr_time = datetime.now(timezone.utc)

# # conversation_collection.create_index("last_message_timestamp")

# # print(conversation_collection.index_information())

# conversation_collection = db["conversations_org_2zV2OOhodLocEnTybzR8t9MIYCE"]

# projection = {
#     "conversation_id": 1,
#     "platform": 1,
#     "customer_username": 1,
#     "customer_name": 1,
#     "last_message": 1,
#     "last_message_timestamp": 1,
#     "unread_count": 1,
#     "is_ai_enabled": 1,
#     "assigned_agent_id": 1,
#     "priority": 1,
#     "sentiment": 1,
#     "query": 1,
#     "_id": 0
# }

# raw_conversations = list(conversation_collection.find(
#     {},
#     projection,  
# ).sort("last_message_timestamp", -1))

# def format_conversation(conv):
#     timestamp = conv.get("last_message_timestamp")
#     if isinstance(timestamp, datetime):
#         timestamp = timestamp.isoformat()
#     return {
#         "id": conv.get("conversation_id"),
#         "platform": conv.get("platform"),
#         "customer_id": conv.get("customer_username"),
#         "customer_name": conv.get("customer_name", ""),
#         "last_message": conv.get("last_message", ""),
#         "timestamp": timestamp,
#         "unread_count": conv.get("unread_count", 0),
#         "is_ai_enabled": conv.get("is_ai_enabled", True),
#         "assigned_agent_id": conv.get("assigned_agent_id"),
#         "priority": conv.get("priority", "medium"),
#         "sentiment": conv.get("sentiment", "neutral"),
#         "query": conv.get("query", ""),
#     }

# formatted_conversations = [format_conversation(conv) for conv in raw_conversations]

# end_time = datetime.now(timezone.utc)

# print(len(formatted_conversations))

# print(f"Time taken: {(end_time - curr_time).total_seconds()} seconds")


user = {
    "org_id": "org_2zV2OOhodLocEnTybzR8t9MIYCE"
}

org_id = user.get("org_id")

# organization specific collection name for conversations
org_conversations_collection_name = f"conversations_{org_id}"

org_collection = db.organizations
org_data = org_collection.find_one({"org_id": org_id})

if not org_data:
    print("Organization not found")

allowed_platforms = []
if org_data.get("ig_id"):
    allowed_platforms.append("instagram")
if org_data.get("wa_id"):
    allowed_platforms.append("whatsapp")

query_filter = {"platform": {"$in": allowed_platforms}}

# Check if collection exists in database
if org_conversations_collection_name not in db.list_collection_names():
    pass

# Get the collection
org_conversations_collection = db[org_conversations_collection_name]

print("Query Filter:", query_filter)

# Fetch conversations and sort by last_message_timestamp
raw_conversations = list(org_conversations_collection.find(
    query_filter, 
    {"_id": 0}  # Exclude _id field
).sort("last_message_timestamp", -1))

print(len(raw_conversations))