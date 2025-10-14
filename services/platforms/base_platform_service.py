from abc import ABC, abstractmethod

class MessageSender(ABC):
    @abstractmethod
    async def send_message(self, sender_id: str, recipient_id: str, message: str) -> None:
        pass


class ConversationHandler(ABC):
    @abstractmethod
    async def create_or_update_conversation(self, org_id: str) -> str:
        pass

    @abstractmethod
    async def broadcast_update(self, recipient_id: str, conversation: dict) -> None:
        pass

class DisconnectHandler(ABC):
    @abstractmethod
    async def archive_data(self, org_id: str, connection_id: str, **kwargs) -> None:
        """Archive all data for the platform (messages, conversations, etc.)"""
        pass

    @abstractmethod
    async def remove_connection(self, org_id: str, connection_id: str) -> dict:
        """Remove platform connection and clear linked org data"""
        pass

    @abstractmethod
    async def disconnect(self, org_id: str, connection_id: str) -> dict:
        """Disconnect the platform from the org"""
        pass