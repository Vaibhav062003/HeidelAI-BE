from .base_platform_service import BasePlatformService
from services.ig_service import send_ig_message, revoke_instagram_access, archive_instagram_data, remove_instagram_connection

class InstagramService(BasePlatformService):
    
    async def send_message(self, sender_id: str, recipient_id: str, message: str, **kwargs) -> dict:
        return await send_ig_message(id=recipient_id, message_text=message,instagram_id=sender_id)

    async def disconnect(self, org_id: str, connection_id: str) -> dict:
        await archive_instagram_data(org_id, connection_id)
        await revoke_instagram_access(org_id, connection_id)
        return await remove_instagram_connection(org_id, connection_id)
