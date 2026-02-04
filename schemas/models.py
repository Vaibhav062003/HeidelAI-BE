# Pydantic
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional, List, Literal, Dict, Any
from enum import Enum
from datetime import datetime, timezone

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

class PlatformDisconnectRequest(BaseModel):
    user_id: str


class WhatsappAuthRequest(BaseModel):
    code: str
    redirect_uri: Optional[str] = None
    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    business_id: Optional[str] = None
    page_ids: Optional[List[str]] = None
    catalog_ids: Optional[List[str]] = None
    dataset_ids: Optional[List[str]] = None
    instagram_account_ids: Optional[List[str]] = None

class ChatbotPromptUpdate(BaseModel):
    chatbot_prompt: str = Field(..., min_length=10, max_length=5000)

class ChatAnalysis(BaseModel):
    conversation_id: str
    summary: List[str] = Field(default_factory=list)
    priority: Optional[Literal["low", "medium", "high"]] = None
    sentiment: Optional[Literal["positive", "neutral", "negative"]] = None
    suggestions: List[str] = Field(default_factory=list)
    last_analyzed: Optional[datetime] = None

class UnarchiveRequest(BaseModel):
    platform_id: str

class PrivateNoteRequest(BaseModel):
    note: str
    posted_by: str
    platform: str
    user_id: str

    @field_validator("note")
    def validate_note(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Note cannot be empty")
        if len(v) > 1000:
            raise ValueError("Note cannot exceed 1000 characters")
        return v
    
class UpdateCategoriesRequest(BaseModel):
    conversation_id: str = Field(..., description="Unique conversation ID of the contact")
    categories: List[str] = Field(..., description="List of categories to assign to this contact")

class SendReactionRequest(BaseModel):
    message_id: str = Field(..., description="Message ID of the message to react to")
    emoji: str = Field(..., description="Emoji to react with")
    platform: str = Field(..., description="Platform of the message (e.g., whatsapp, instagram)")

# Products Service
class Product(BaseModel):
    product_name: str
    description: Optional[str] = None
    price: float = None
    stock: Optional[int] = None
    product_image: Optional[str] = None
    image_url: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    org_id: Optional[str] = None

    def to_mongo(self):
        """
        Convert Pydantic model to a dict ready for MongoDB insertion.
        """
        data = self.dict()
        # Remove None values if you want a clean DB
        cleaned = {k: v for k, v in data.items() if v is not None}
        return cleaned
    
class CannedResponse(BaseModel):
    shortcut: str
    response: str

class UpdateCannedResponseRequest(BaseModel):
    response: str

class CannedResponseOut(BaseModel):
    id: str
    org_id: str
    shortcut: str
    response: str
    updated_at: datetime

class SubscriptionRequest(BaseModel):
    plan_id: Literal["free", "growth", "enterprise"]
    amount: float
    customer_id: str
    customer_phone: str
    customer_email: str
    customer_full_name: str

class InitSignupRequest(BaseModel):
    first_name: str
    last_name: Optional[str] = None
    email: EmailStr
    provider: str = "email"
    clerk_signup_id: str
    clerk_user_id: Optional[str] = None

    @model_validator(mode='after')
    def require_last_name_for_email(self):
        # Enforce last_name if provider is email
        if self.provider == "email" and not self.last_name:
            raise ValueError("Last name is required when signing up with email")
        return self
    
class SetPasswordRequest(BaseModel):
    clerk_user_id: Optional[str] = None

class VerifyOtpRequest(BaseModel):
    clerk_user_id: str
class SelectPlanRequest(BaseModel):
    plan_id: Literal["free", "growth_monthly", "growth_yearly", "enterprise", "custom"]

class InitPaymentRequest(BaseModel):
    phone_number: str
    
class InviteTeamMembersRequest(BaseModel):
    org_name: str
    member_emails: List[EmailStr]