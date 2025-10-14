# services/messaging/whatsapp_service.py

from datetime import datetime
from .base_platform_service import MessageSender, ConversationHandler
from services.wa_service import broadcast_whatsapp_message


class WhatsAppService(MessageSender, ConversationHandler):
    # PLATFORM = "whatsapp"

    async def send_message(self, recipient_id: str, message: str) -> None:
        print(f"Mock up WhatsApp send_message")
    #     """
    #     Send a WhatsApp message to the recipient.
    #     In production, this would call the WhatsApp Business API.
    #     """
    #     print(f"[WhatsApp] Sending message to {recipient_id}: {message}")
    #     # Example: await wa_client.send_message(recipient_id, message)

    async def create_or_update_conversation(
        self, org_id: str, recipient_id: str, sender_id: str, customer_name: str, last_message: str
    ) -> str:
        print(f"Mock up WhatsApp create_or_update_conversation")
        return "<conversation_id>"
    #     """
    #     Create or update a WhatsApp conversation.
    #     Returns the conversation_id.
    #     """
    #     conversation_id = f"whatsapp_{sender_id}"
    #     conversations_collection_name = f"conversations_{org_id}"
    #     conversations_collection = db[conversations_collection_name]

    #     existing_conversation = conversations_collection.find_one(
    #         {"conversation_id": conversation_id}
    #     )

    #     if existing_conversation:
    #         wa_conversation = {
    #             "id": conversation_id,
    #             "platform": self.PLATFORM,
    #             "conversation_id": conversation_id,
    #             "whatsapp_id": recipient_id,
    #             "customer_id": sender_id,
    #             "customer_name": customer_name,
    #             "last_message": last_message,
    #             "timestamp": datetime.now(),
    #             "last_message_timestamp": datetime.now(),
    #         }
    #     else:
    #         wa_conversation = {
    #             "id": conversation_id,
    #             "platform": self.PLATFORM,
    #             "conversation_id": conversation_id,
    #             "whatsapp_id": recipient_id,
    #             "customer_id": sender_id,
    #             "customer_name": customer_name,
    #             "last_message": last_message,
    #             "timestamp": datetime.now(),
    #             "last_message_timestamp": datetime.now(),
    #             "unread_count": 1,
    #             "is_ai_enabled": True,
    #         }

    #     conversations_collection.update_one(
    #         {"conversation_id": conversation_id},
    #         {"$set": wa_conversation},
    #         upsert=True,
    #     )

    #     # broadcast the updated conversation
    #     updated_conversation = conversations_collection.find_one(
    #         {"conversation_id": conversation_id}
    #     )
    #     await self.broadcast(recipient_id, updated_conversation)

    #     return conversation_id


    async def broadcast(self, recipient_id: str, conversation: dict) -> None:
        """
        Broadcast conversation updates to WhatsApp-specific connections.
        """
        print("mockup broadcast for WhatsApp")
        pass
        # if recipient_id in services.whatsapp_connections_map:
        #     broadcast_conversation = dict(conversation)
        #     if "last_message_timestamp" in broadcast_conversation:
        #         broadcast_conversation["last_message_timestamp"] = (
        #             broadcast_conversation["last_message_timestamp"].isoformat()
        #         )
        #     if "_id" in broadcast_conversation:
        #         broadcast_conversation["_id"] = str(broadcast_conversation["_id"])

        #     await broadcast_whatsapp_message(
        #         recipient_id,
        #         "conversation_updated",
        #         {"conversation": broadcast_conversation},
        #     )
