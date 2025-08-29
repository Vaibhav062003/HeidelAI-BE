from datetime import datetime
from core.services import services
from .base import MessageSender, ConversationHandler
from database import get_mongo_db
from services.ig_service import broadcast_instagram_message, send_ig_message

db = get_mongo_db()

class InstagramMessageSender(MessageSender):
    async def send_message(self, recipient_id: str, message: str) -> None:

        await send_ig_message(recipient_id, message)


class InstagramConversationHandler(ConversationHandler):
    async def create_or_update_conversation(self, org_id: str, **kwargs) -> str:
        recipient_id = kwargs["recipient_id"]
        sender_id = kwargs["sender_id"]
        username = kwargs["username"]
        customer_name = kwargs["customer_name"]
        last_message = kwargs["last_message"]

        conversation_id = f"instagram_{username}"

        conversations_collection_name = f"conversations_{org_id}"
        conversations_collection = db[conversations_collection_name]
        
        if(conversations_collection_name in db.list_collection_names()):
            print(f"Conversations collection exists: {conversations_collection_name}")
        else:
            print(f"Conversations collection does not exist: {conversations_collection_name}")

        existing = conversations_collection.find_one({"conversation_id": conversation_id})

        conversation = {
            "id": conversation_id,
            "platform": "instagram",
            "conversation_id": conversation_id,
            "instagram_id": recipient_id,
            "customer_id": sender_id,
            "customer_name": customer_name,
            "customer_username": username,
            "last_message": last_message,
            "timestamp": datetime.now(),
            "last_message_timestamp": datetime.now(),
            "unread_count": existing.get("unread_count", 1) if existing else 1,
            "is_ai_enabled": existing.get("is_ai_enabled", True) if existing else True,
        }

        conversations_collection.update_one(
            {"conversation_id": conversation_id},
            {"$set": conversation},
            upsert=True
        )

        print(f"Conversation {conversation_id} updated:")

        return conversation

    async def broadcast_update(self, recipient_id: str, conversation: dict) -> None:
        if recipient_id in services.instagram_connections_map:
            if "last_message_timestamp" in conversation:
                conversation["last_message_timestamp"] = conversation["last_message_timestamp"].isoformat()
            if "_id" in conversation:
                conversation["_id"] = str(conversation["_id"])

            await broadcast_instagram_message(recipient_id, "conversation_updated", {
                "conversation": conversation
            })


class InstagramService:
    def __init__(self):
        self.sender = InstagramMessageSender()
        self.conversations = InstagramConversationHandler()

    async def send_message(self, recipient_id: str, message: str):
        return await self.sender.send_message(recipient_id, message)

    async def handle_conversation(self, org_id: str, **kwargs):
        conv_id = await self.conversations.create_or_update_conversation(org_id, **kwargs)
        updated = db[f"conversations_{org_id}"].find_one({"conversation_id": conv_id})
        await self.conversations.broadcast_update(kwargs["recipient_id"], dict(updated))
        return conv_id

    async def handle_conversation(self, org_id: str, **kwargs):
        conversation = await self.conversations.create_or_update_conversation(org_id, **kwargs)
        await self.conversations.broadcast_update(kwargs["recipient_id"], dict(conversation))
        return conversation["conversation_id"]