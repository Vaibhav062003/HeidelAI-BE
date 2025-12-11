from abc import ABC, abstractmethod

class BasePlatformService(ABC):
    
    @abstractmethod
    async def send_message(self, sender_id: str, recipient_id: str, message: str, **kwargs) -> dict:
        """Sends a message. kwargs handles platform-specific context."""
        pass

    @abstractmethod
    async def disconnect(self, org_id: str, connection_id: str) -> dict:
        """Disconnects platform."""
        pass