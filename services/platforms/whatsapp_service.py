from datetime import datetime
import asyncio
from database import get_mongo_db
from .base_platform_service import BasePlatformService
from services.wa_service import send_whatsapp_message, revoke_whatsapp_access, archive_whatsapp_data, remove_whatsapp_connection
from config.settings import ORG_ID

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

    async def disconnect(self, org_id: str, connection_id: str) -> dict:
        # await archive_whatsapp_data(org_id, connection_id)
        await revoke_whatsapp_access(org_id, connection_id)
        return await remove_whatsapp_connection(org_id, connection_id)

# if __name__ == "__main__":
#     service = WhatsAppService()
