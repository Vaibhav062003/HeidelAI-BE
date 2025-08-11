# Pydantic
from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum
from datetime import datetime

class MessageRole(str, Enum):
    CUSTOMER = "customer"
    AI = "ai"
    AGENT = "agent"
    SYSTEM = "system"

class Conversation(BaseModel):
    id: str
    platform: str
    customer_id: str
    customer_name: str
    last_message: str
    timestamp: datetime
    unread_count: int = 0
    avatar_url: Optional[str] = None
    is_ai_enabled: bool = True
    assigned_agent_id: Optional[str] = None
    priority : Optional[str] = None

class Message(BaseModel):
    user_id: str
    message: str


class ChatMessage(BaseModel):
    user_id: str
    message: str
    message_id: Optional[str] = None


class InstagramAuthRequest(BaseModel):
    code: str
    redirect_uri: Optional[str] = "https://www.heidelai.com/auth/instagram/callback"

class InstagramDisconnectRequest(BaseModel):
    user_id: str


class WhatsappAuthRequest(BaseModel):
    code: str
    redirect_uri: str

class ChatbotPromptUpdate(BaseModel):
    chatbot_prompt: str = Field(..., min_length=10, max_length=5000)
