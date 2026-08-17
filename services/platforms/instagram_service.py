from .base_platform_service import BasePlatformService
from services.ig_service import send_ig_message, revoke_instagram_access, archive_instagram_data, remove_instagram_connection

class InstagramService(BasePlatformService):
    
    async def send_message(self, sender_id: str, recipient_id: str, message: str, **kwargs) -> dict:
        response = await send_ig_message(
            id=recipient_id,
            message_text=message,
            instagram_id=sender_id,
            mode="reply",
            org_id=kwargs.get("org_id"),
            temp_id=kwargs.get("temp_id"),
            conversation_id=kwargs.get("conversation_id"),
            sender_name=kwargs.get("sender_name", "AI follow-up"),
            is_auto_followup=bool(kwargs.get("is_auto_followup")),
        )
        if not isinstance(response, dict) or response.get("error") or response.get("success") is False:
            raise RuntimeError(response.get("error", "Instagram rejected the follow-up") if isinstance(response, dict) else "Instagram rejected the follow-up")
        return response

    async def disconnect(self, org_id: str, connection_id: str) -> dict:
        await archive_instagram_data(org_id, connection_id)
        await revoke_instagram_access(org_id, connection_id)
        return await remove_instagram_connection(org_id, connection_id)
