from database import get_mongo_db
from .base_platform_service import BasePlatformService
from services.wa_service import deactivate_whatsapp_connection, send_whatsapp_message

db = get_mongo_db()   

class WhatsAppService(BasePlatformService):

    async def send_message(self, sender_id: str, recipient_id: str, message: str, **kwargs) -> dict:
        
        payload = {
            "conversation_id": kwargs.get("conversation_id"),
            "content": message,
            "sender_id": sender_id
        }

        org_id = kwargs.get("org_id")

        return await send_whatsapp_message(
            payload,
            org_id,
            mode="reply",
            is_auto_followup=bool(kwargs.get("is_auto_followup")),
        )

    async def disconnect(self, org_id: str, phone_id: str) -> dict:
        return await deactivate_whatsapp_connection(org_id, phone_id)

# if __name__ == "__main__":
#     service = WhatsAppService()
