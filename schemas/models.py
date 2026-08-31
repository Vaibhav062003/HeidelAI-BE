# Pydantic
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Annotated, Optional, List, Literal, Dict, Any, Union
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
    tags: List[str] = Field(default_factory=list)
    labels: List[str] = Field(default_factory=list)
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

class UpdateLabelsRequest(BaseModel):
    conversation_id: str = Field(..., description="Unique conversation ID of the contact")
    labels: List[str] = Field(..., description="List of labels to assign to this contact")

class SendReactionRequest(BaseModel):
    message_id: str = Field(..., description="Message ID of the message to react to")
    emoji: str = Field(..., description="Emoji to react with")
    platform: str = Field(..., description="Platform of the message (e.g., whatsapp, instagram)")

# Products Service
class Product(BaseModel):
    product_name: str
    description: Optional[str] = None
    # ``price`` is retained for legacy display. Safety decisions use only the
    # provenance-bearing canonical fields below.
    price: Optional[float] = None
    cost_price: Optional[float] = Field(default=None, gt=0)
    approved_floor_price: Optional[float] = Field(default=None, gt=0)
    target_selling_price: Optional[float] = Field(default=None, gt=0)
    mrp: Optional[float] = Field(default=None, gt=0)
    currency: Literal["INR"] = "INR"
    selling_gst_inclusive: Optional[bool] = None
    cost_gst_inclusive: Optional[bool] = None
    price_source_sheet_id: Optional[str] = None
    price_verified_at: Optional[datetime] = None
    price_version: Optional[str] = None
    stock: Optional[int] = None
    gst_inclusive: bool = False
    gst_rate: float = Field(default=18, ge=0, le=100)
    product_image: Optional[str] = None
    image_url: Optional[str] = None
    model_no: Optional[str] = None
    search_text: Optional[str] = None
    source: Optional[str] = None  # "manual" | "csv_upload" | "google_sheet"
    source_sheet_id: Optional[str] = None
    source_sheet_ids: List[str] = Field(default_factory=list)
    updated_at: Optional[datetime] = None
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


# Product Sheet Sync Models
class ProductSheetConnection(BaseModel):
    org_id: str
    sheet_id: str
    sheet_name: str
    tab_name: str
    column_mapping: Dict[str, str]  # {product_field: sheet_column_name}
    product_type: str = "laptop"  # "laptop" | "component"
    last_synced_at: Optional[datetime] = None
    next_reconcile_at: Optional[datetime] = None
    reconcile_lease_id: Optional[str] = None
    reconcile_lease_until: Optional[datetime] = None
    last_sync_count: int = 0
    sync_errors: List[str] = Field(default_factory=list)
    status: str = "active"  # "active" | "syncing" | "error"
    connected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PreviewProductSheetRequest(BaseModel):
    sheet_id: str
    tab_name: Optional[str] = None

class ConnectProductSheetRequest(BaseModel):
    sheet_id: str
    sheet_name: str
    tab_name: str
    column_mapping: Dict[str, str]
    product_type: str = "laptop"  # "laptop" | "component"

class SyncProductSheetRequest(BaseModel):
    sheet_id: str


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
    plan_id: Literal["free", "growth_monthly_inr_3499", "growth_yearly_inr_37789", "growth_yearly_usd_456", "growth_monthly_usd_46", "enterprise", "custom"]

class InitPaymentRequest(BaseModel):
    phone_number: str
    
class InviteTeamMembersRequest(BaseModel):
    org_name: str
    member_emails: List[EmailStr]

class Template(BaseModel):
    name: str
    language: str
    category: str
    components: List[Dict[str, Any]]


# Interactive WhatsApp Message Models

class ListRow(BaseModel):
    id: str
    title: str = Field(..., max_length=24)
    description: Optional[str] = None

class ListSection(BaseModel):
    title: Optional[str] = None
    rows: List[ListRow]

class ReplyButton(BaseModel):
    id: str
    title: str = Field(..., max_length=20)

class ActionButton(BaseModel):
    type: Literal["reply"] = "reply"
    reply: ReplyButton

# Header fields for text, image, document, video
class MediaLink(BaseModel):
    link: str

class TextHeader(BaseModel):
    type: Literal["text"]
    text: str

class ImageHeader(BaseModel):
    type: Literal["image"]
    image: MediaLink

class DocumentHeader(BaseModel):
    type: Literal["document"]
    document: MediaLink

class VideoHeader(BaseModel):
    type: Literal["video"]
    video: MediaLink

# Pydantic checks the "type" field to validate the correct header format
WhatsAppInteractiveMessageHeader = Annotated[
    Union[TextHeader, ImageHeader, DocumentHeader, VideoHeader],
    Field(discriminator="type")
]

# 1. List Message Model
class InteractiveListMessage(BaseModel):
    type: Literal["list"]
    header_text: Optional[str] = None
    body_text: str = Field(..., max_length=1024)
    button_text: str
    sections: List[ListSection]
    footer_text: Optional[str] = "Powered by heidelai.com"

# 2. URL Button Message Model
class InteractiveUrlButtonMessage(BaseModel):
    type: Literal["cta_url"]
    header: Optional[WhatsAppInteractiveMessageHeader] = None
    body_text: str = Field(..., max_length=1024)
    footer_text: Optional[str] = "Powered by heidelai.com"
    url: str
    button_text: str

# 3. Reply Button Message Model
class InteractiveReplyButtonMessage(BaseModel):
    type: Literal["button"]
    header: Optional[WhatsAppInteractiveMessageHeader] = None
    body_text: str = Field(..., max_length=1024)
    footer_text: Optional[str] = "Powered by heidelai.com"
    buttons: List[ActionButton] = Field(..., max_length=3)

# TODO: More 2 are left such as Media Carousel and Product Carousel

WhatsAppInteractiveMessage = Annotated[
    Union[
        InteractiveListMessage,
        InteractiveUrlButtonMessage,
        InteractiveReplyButtonMessage,
        # other models will be added here
    ],
    Field(discriminator="type")
]


class BulkRecipient(BaseModel):
    phone_number: str = Field(..., pattern=r"^\d{7,15}$")  # E.164 digits only, 7-15 chars

class BulkMessageRequest(BaseModel):
    template_name: str = Field(..., min_length=1)       # Must be an APPROVED WhatsApp template
    language_code: str = Field(                         # e.g. "en", "en_US", "hi"
        ...,
        min_length=1,
        pattern=r"^[a-z]{2}(?:_[A-Z]{2})?$",
    )
    recipients: List[BulkRecipient] = Field(..., min_length=1)  # At least one recipient required
    components: Optional[List[Dict[str, Any]]] = None  # Same for all recipients (header, body, etc.)
    delay_seconds: float = Field(default=8.0, ge=8.0)  # Seconds to wait between each batch of sends (applied per batch, not per recipient)
