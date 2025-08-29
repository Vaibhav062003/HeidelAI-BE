from abc import ABC, abstractmethod

class MessageSender(ABC):
    @abstractmethod
    async def send_message(self, recipient_id: str, message: str) -> None:
        pass


class ConversationHandler(ABC):
    @abstractmethod
    async def create_or_update_conversation(self, org_id: str, **kwargs) -> str:
        pass

    @abstractmethod
    async def broadcast_update(self, recipient_id: str, conversation: dict) -> None:
        pass
