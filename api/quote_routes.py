import uuid
import re
from datetime import date, datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ReturnDocument

from auth.auth_utils import admin_required, reverification_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.cloudinary_service import upload_media_to_cloudinary
from services.quote_service import build_quote_document
from services.utils import serialize_mongo
from services.work_team_service import list_work_teams, require_conversation_access
from services.entitlement_service import require_feature
from services.feature_flags import native_invoicing_enabled, tally_enabled
from services.native_invoice_service import invoice_provider
from services.iris_irp_service import save_taxpayer_credentials, taxpayer_credential_status
from services.india_tax import gst_state_name, is_valid_gstin


router = APIRouter(prefix="/api/quotes", tags=["Quotes"])
db = get_async_mongo_db()
QUOTE_CONTENT_FIELDS = (
    "conversation_id", "quote_number", "issued_on", "due_date", "valid_until",
    "seller", "customer", "items", "shipping", "discount", "notes", "terms",
    "public_options", "branding", "private_subtotal", "private_gst_total",
    "public_grand_total",
)


class QuoteParty(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    legal_name: str = Field(default="", max_length=120)
    trade_name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=100)
    gstin: str = Field(default="", max_length=30)
    address: str = Field(default="", max_length=500)
    location: str = Field(default="", max_length=100)
    pincode: str = Field(default="", pattern=r"^(?:|\d{6})$")
    payment_details: str = Field(default="", max_length=500)
    logo_url: str = Field(default="", max_length=2048)
    state_code: str = Field(default="", pattern=r"^(?:|\d{2})$")

    @field_validator("gstin")
    @classmethod
    def validate_gstin(cls, value: str) -> str:
        value = value.strip().upper()
        if value and not is_valid_gstin(value):
            raise ValueError("GSTIN checksum is invalid")
        return value

    @field_validator("state_code")
    @classmethod
    def validate_state_code(cls, value: str) -> str:
        if value:
            gst_state_name(value)
        return value

    @model_validator(mode="after")
    def validate_gstin_state(self):
        if self.gstin and self.state_code and self.gstin[:2] != self.state_code:
            raise ValueError("GSTIN and state code do not match")
        return self


class QuoteItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=750)
    category: str = Field(default="", max_length=40)
    sku: str = Field(default="", max_length=60)
    quantity: float = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(..., ge=0, le=999_999_999)
    gst_rate: float = Field(default=0, ge=0, le=100)
    gst_inclusive: bool = False
    image_url: str = Field(default="", max_length=2048)
    tally_master_id: str = Field(default="", max_length=120)
    tally_guid: str = Field(default="", max_length=120)
    tally_alter_id: str = Field(default="", max_length=120)
    tally_unit: str = Field(default="", max_length=30)


class QuotePublicOptions(BaseModel):
    show_unit_prices: bool = False
    show_line_totals: bool = False
    show_quantities: bool = True
    show_sku: bool = True
    show_gst_breakdown: bool = True
    show_item_images: bool = True


class QuoteComponentCategory(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=40)
    enabled: bool = True


class QuoteSectionLayout(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: Literal[
        "customer", "metadata", "build", "items", "totals", "seller",
        "payment", "notes", "terms",
    ]
    label: str = Field(min_length=1, max_length=40)
    enabled: bool = True


class QuoteColumnLayout(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: Literal["category", "description", "sku", "quantity", "rate", "gst", "total"]
    label: str = Field(min_length=1, max_length=30)
    enabled: bool = True


def _default_quote_sections() -> list[QuoteSectionLayout]:
    return [QuoteSectionLayout(id=id, label=label) for id, label in (
        ("customer", "Customer"), ("metadata", "Quote details"), ("build", "Build summary"),
        ("items", "Items"), ("totals", "Totals"), ("seller", "Seller details"),
        ("payment", "Payment details"), ("notes", "Build and delivery notes"),
        ("terms", "Terms and conditions"),
    )]


def _default_quote_columns() -> list[QuoteColumnLayout]:
    return [QuoteColumnLayout(id=id, label=label, enabled=enabled) for id, label, enabled in (
        ("category", "Component", True), ("description", "Specification", True),
        ("sku", "SKU", True), ("quantity", "Qty", True), ("rate", "Rate", False),
        ("gst", "GST", True), ("total", "Total", False),
    )]


class QuoteTextLabels(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    issued: str = Field(default="Issued", min_length=1, max_length=30)
    paymentDue: str = Field(default="Payment due", min_length=1, max_length=30)
    validUntil: str = Field(default="Valid until", min_length=1, max_length=30)
    components: str = Field(default="Components", min_length=1, max_length=30)
    totalInvestment: str = Field(default="Total investment", min_length=1, max_length=40)
    taxIncluded: str = Field(default="Inclusive of applicable GST", min_length=1, max_length=60)
    subtotal: str = Field(default="Subtotal", min_length=1, max_length=30)
    gst: str = Field(default="GST", min_length=1, max_length=30)
    shipping: str = Field(default="Shipping", min_length=1, max_length=30)
    discount: str = Field(default="Discount", min_length=1, max_length=30)


class QuoteDesign(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(default="Signature", min_length=1, max_length=40)
    headerColor: str = Field(default="#344B5F", pattern=r"^#[0-9A-Fa-f]{6}$")
    accentColor: str = Field(default="#F36B3D", pattern=r"^#[0-9A-Fa-f]{6}$")
    pageColor: str = Field(default="#FBFBF8", pattern=r"^#[0-9A-Fa-f]{6}$")
    textColor: str = Field(default="#2A241D", pattern=r"^#[0-9A-Fa-f]{6}$")
    typography: Literal["modern", "editorial"] = "modern"
    fontScale: float = Field(default=1, ge=0.9, le=1.15)
    density: Literal["compact", "comfortable"] = "compact"
    headerStyle: Literal["solid", "light"] = "light"
    tableStyle: Literal["striped", "ruled"] = "ruled"
    totalStyle: Literal["filled", "outline"] = "filled"
    showCustomerBlock: bool = True
    showMetadata: bool = True
    showSellerDetails: bool = True
    showPaymentDetails: bool = True
    showNotes: bool = True
    showTerms: bool = True
    showPageNumbers: bool = True
    labels: QuoteTextLabels = Field(default_factory=QuoteTextLabels)
    sections: list[QuoteSectionLayout] = Field(default_factory=_default_quote_sections, min_length=1, max_length=9)
    columns: list[QuoteColumnLayout] = Field(default_factory=_default_quote_columns, min_length=1, max_length=7)

    @model_validator(mode="after")
    def validate_layout(self):
        for name, entries in (("sections", self.sections), ("columns", self.columns)):
            ids = [entry.id for entry in entries]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Quote {name} cannot contain duplicates")
            if not any(entry.enabled for entry in entries):
                raise ValueError(f"At least one quote {name[:-1]} must be visible")
        return self


class QuoteBranding(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    document_title: str = Field(default="Quotation", min_length=1, max_length=60)
    build_title: str = Field(default="Performance Workstation", max_length=80)
    build_label: str = Field(default="Custom PC", max_length=30)
    build_promise: str = Field(default="Manufacturer warranty / GST invoice", max_length=100)
    theme: Literal["slate", "emerald", "blue", "burgundy"] = "slate"
    cover_image_url: str = Field(default="", max_length=2048)
    footer_text: str = Field(default="Thank you for the opportunity to build this with you.", max_length=240)
    component_categories: list[QuoteComponentCategory] = Field(default_factory=list, max_length=20)
    design: Optional[QuoteDesign] = None


class QuoteBrandingDefaults(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    document_title: str = Field(default="Quotation", min_length=1, max_length=60)
    build_title: str = Field(default="Performance Workstation", min_length=1, max_length=80)
    build_label: str = Field(default="Custom PC", min_length=1, max_length=30)
    build_promise: str = Field(default="Manufacturer warranty / GST invoice", max_length=100)
    footer_text: str = Field(default="Thank you for your business.", max_length=240)
    component_categories: list[QuoteComponentCategory] = Field(default_factory=list, max_length=20)


class EInvoiceApplicabilityInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: Literal["unassessed", "not_required", "required"]
    gstin: str = Field(default="", max_length=15, pattern=r"^(?:|\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])$")
    reporting_window_days: Optional[Literal[30]] = None
    b2c_dynamic_qr_required: Optional[bool] = None
    not_required_reason: Optional[Literal["below_threshold", "notified_exemption"]] = None

    @field_validator("gstin", mode="before")
    @classmethod
    def normalize_gstin(cls, value: str) -> str:
        return str(value or "").strip().upper()

    @model_validator(mode="after")
    def validate_assessment(self):
        if self.status != "unassessed" and not self.gstin:
            raise ValueError("GSTIN is required when e-invoice applicability is assessed")
        if self.gstin and not is_valid_gstin(self.gstin):
            raise ValueError("GSTIN checksum is invalid")
        if self.status == "required" and self.b2c_dynamic_qr_required is None:
            raise ValueError("Record whether B2C dynamic QR requirements apply")
        if self.status == "not_required" and self.not_required_reason is None:
            raise ValueError("Record why e-invoicing is not required")
        if self.status != "required":
            self.b2c_dynamic_qr_required = False
        if self.status != "not_required":
            self.not_required_reason = None
        return self


class NumberingSeriesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    series: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9-]+$")
    prefix: str = Field(max_length=8, pattern=r"^[A-Za-z0-9/.-]*$")
    suffix: str = Field(default="", max_length=4, pattern=r"^[A-Za-z0-9/.-]*$")
    leading_zeros: int = Field(ge=1, le=8)

    @model_validator(mode="after")
    def validate_gst_number_length(self):
        if len(self.prefix) + 6 + self.leading_zeros + len(self.suffix) > 16:
            raise ValueError("Formatted GST document numbers must be no more than 16 characters")
        self.series = self.series.upper()
        self.prefix = self.prefix.upper()
        self.suffix = self.suffix.upper()
        return self


class InvoiceNumberingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice: Optional[NumberingSeriesInput] = None
    credit_note: Optional[NumberingSeriesInput] = None
    debit_note: Optional[NumberingSeriesInput] = None


class InvoiceControlsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed_through: Optional[date] = None
    numbering: Optional[InvoiceNumberingInput] = None

    @field_validator("closed_through")
    @classmethod
    def validate_closed_through(cls, value: Optional[date]) -> Optional[date]:
        if value and value > date.today():
            raise ValueError("Closed-through date cannot be in the future")
        return value


INVOICE_CONTROL_DEFAULTS = {
    "closed_through": None,
    "numbering": {
        "invoice": {"series": "INV", "prefix": "INV/", "suffix": "", "leading_zeros": 6},
        "credit_note": {"series": "CN", "prefix": "CN/", "suffix": "", "leading_zeros": 6},
        "debit_note": {"series": "DN", "prefix": "DN/", "suffix": "", "leading_zeros": 6},
    },
}


def _invoice_controls_view(value: dict | None) -> dict:
    value = value or {}
    numbering = value.get("numbering") or {}
    return {
        "closed_through": value.get("closed_through"),
        "numbering": {
            kind: {**defaults, **(numbering.get(kind) or {})}
            for kind, defaults in INVOICE_CONTROL_DEFAULTS["numbering"].items()
        },
    }


class IrisCredentialsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    gstin: str = Field(max_length=15, pattern=r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
    username: str = Field(min_length=1, max_length=15)
    password: str = Field(min_length=8, max_length=16)

    @field_validator("gstin", mode="before")
    @classmethod
    def normalize_gstin(cls, value: str) -> str:
        return str(value or "").strip().upper()


class QuoteSettingsPayload(BaseModel):
    logo_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    seller: Optional[QuoteParty] = None
    pdf_design: Optional[QuoteDesign] = None
    branding_defaults: Optional[QuoteBrandingDefaults] = None
    invoice_provider: Optional[Literal["native", "tally"]] = None
    e_invoice_applicability: Optional[EInvoiceApplicabilityInput] = None
    invoice_controls: Optional[InvoiceControlsInput] = None


class QuotePayload(BaseModel):
    expected_revision: Optional[int] = Field(default=None, ge=1)
    case_id: Optional[str] = Field(default=None, max_length=100)
    purchase_version: Optional[int] = Field(default=None, ge=0)
    conversation_id: str = Field(..., min_length=1, max_length=200)
    quote_number: str = Field(..., min_length=1, max_length=60)
    issued_on: date
    due_date: Optional[date] = None
    valid_until: Optional[date] = None
    seller: QuoteParty
    customer: QuoteParty
    items: list[QuoteItem] = Field(..., min_length=1, max_length=50)
    shipping: float = Field(default=0, ge=0, le=999_999_999)
    discount: float = Field(default=0, ge=0, le=999_999_999)
    notes: str = Field(default="", max_length=1200)
    terms: list[str] = Field(default_factory=list, max_length=20)
    public_options: QuotePublicOptions = Field(default_factory=QuotePublicOptions)
    branding: QuoteBranding = Field(default_factory=QuoteBranding)


def _collection(org_id: str):
    return db[f"quotes_{org_id}"]


def _settings_collection(org_id: str):
    return db[f"quote_settings_{org_id}"]


def _document(payload: QuotePayload, user: CurrentUser) -> dict:
    return build_quote_document(
        {key: value for key, value in payload.model_dump(mode="json").items() if key != "expected_revision" and (key not in {"case_id", "purchase_version"} or value is not None)}, org_id=user.org_id,
        actor_id=user.user_id, actor_name=user.full_name or "Team member",
    )


async def _require_conversation(user: CurrentUser, conversation_id: str) -> None:
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)


def _quote_content(document: dict) -> dict:
    return {key: document.get(key) for key in QUOTE_CONTENT_FIELDS}


def _revision_snapshot(document: dict) -> dict:
    return {
        **_quote_content(document),
        "revision_number": int(document.get("revision_number") or 1),
        "saved_at": document.get("updated_at") or document.get("created_at"),
        "saved_by": document.get("updated_by") or document.get("created_by"),
        "saved_by_name": document.get("updated_by_name") or document.get("created_by_name") or "Team member",
    }


@router.get("")
@limiter.limit("60/minute")
async def list_quotes(
    request: Request,
    user: CurrentUser,
    conversation_id: Optional[str] = Query(default=None, min_length=1, max_length=200),
    search: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    query: dict = {"org_id": user.org_id}
    if conversation_id:
        await _require_conversation(user, conversation_id)
        query["conversation_id"] = conversation_id
    elif user.role not in {"admin", "org:admin"}:
        teams = await list_work_teams(user.org_id)
        if teams and not any(team.get("type") in {"sales", "billing"} and user.user_id in team.get("member_ids", []) for team in teams):
            query["created_by"] = user.user_id
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"quote_number": {"$regex": escaped, "$options": "i"}},
            {"customer.name": {"$regex": escaped, "$options": "i"}},
            {"customer.phone": {"$regex": escaped, "$options": "i"}},
        ]
    rows = await _collection(user.org_id).find(
        query,
        {"revisions": 0},
    ).sort("created_at", -1).skip((page - 1) * limit).to_list(length=limit + 1)
    return {"quotes": serialize_mongo(rows[:limit]), "page": page, "has_more": len(rows) > limit}


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def create_quote(request: Request, payload: QuotePayload, user: CurrentUser):
    require_feature(user.org_id, "OFFERS")
    await _require_conversation(user, payload.conversation_id)
    if await _collection(user.org_id).find_one({"org_id": user.org_id, "quote_number": payload.quote_number}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="Quote number is already in use")
    if payload.case_id:
        from services.sales_workflow_service import require_enabled
        from services.sales_case_service import get_active_sales_case
        await require_enabled(user.org_id)
        case = await get_active_sales_case(user.org_id, payload.conversation_id)
        if not case or case["case_id"] != payload.case_id or case["version"] != payload.purchase_version:
            raise HTTPException(409, "Purchase changed. Prepare the quote again")
    now = datetime.now(timezone.utc)
    document = {
        **_document(payload, user),
        "quote_id": str(uuid.uuid4()),
        "created_at": now,
        "created_by": user.user_id,
        "created_by_name": user.full_name or "Team member",
        "revision_number": 1,
        "revision_count": 0,
    }
    await _collection(user.org_id).insert_one(document)
    if payload.case_id:
        from services.sales_workflow_service import _collection as cases, publish, event
        updated = await cases(user.org_id).find_one_and_update(
            {"case_id": payload.case_id, "version": payload.purchase_version, "status": "active"},
            {"$set": {"quote_id": document["quote_id"], "quoted_version": payload.purchase_version}}, return_document=ReturnDocument.AFTER)
        if not updated:
            raise HTTPException(409, "Purchase changed while saving. Reopen the purchase before sharing this quote")
        await event(updated, "preparation", document["quote_id"], user.user_id)
        await publish(updated)
    return {"quote": serialize_mongo(document)}


@router.get("/settings")
@limiter.limit("60/minute")
async def get_quote_settings(request: Request, user: CurrentUser):
    settings = await _settings_collection(user.org_id).find_one({"_id": "branding"})
    organization = await db.organizations.find_one({"org_id": user.org_id}, {"org_name": 1})
    seller = (settings or {}).get("seller") or {}
    iris_status = await taxpayer_credential_status(user.org_id, seller.get("gstin", ""))
    return {
        "settings": {
            "logo_url": (settings or {}).get("logo_url", ""),
            "seller": {
                "name": seller.get("name") or (organization or {}).get("org_name") or "Organization",
                "legal_name": seller.get("legal_name", ""),
                "trade_name": seller.get("trade_name", ""),
                "phone": seller.get("phone", ""),
                "email": seller.get("email", ""),
                "gstin": seller.get("gstin", ""),
                "address": seller.get("address", ""),
                "location": seller.get("location", ""),
                "pincode": seller.get("pincode", ""),
                "payment_details": seller.get("payment_details", ""),
                "state_code": seller.get("state_code", ""),
            },
            "pdf_design": (settings or {}).get("pdf_design") or QuoteDesign().model_dump(),
            "branding_defaults": (settings or {}).get("branding_defaults") or QuoteBrandingDefaults().model_dump(),
            "can_edit": user.role in {"admin", "org:admin"},
            "invoice_provider": await invoice_provider(user.org_id),
            "e_invoice_applicability": (settings or {}).get("e_invoice_applicability") or {
                "status": "unassessed",
                "gstin": seller.get("gstin", ""),
                "reporting_window_days": None,
                "b2c_dynamic_qr_required": False,
                "not_required_reason": None,
            },
            "invoice_controls": _invoice_controls_view((settings or {}).get("invoice_controls")),
            "iris_irp": iris_status,
        }
    }


@router.put("/settings")
@limiter.limit("10/minute")
async def update_quote_settings(request: Request, payload: QuoteSettingsPayload, user: CurrentUser):
    require_feature(user.org_id, "OFFERS")
    await admin_required(user)
    if (
        payload.e_invoice_applicability is not None or payload.invoice_controls is not None
        or payload.invoice_provider is not None or payload.seller is not None
    ) and (response := reverification_required(user)):
        return response
    current = await _settings_collection(user.org_id).find_one({"_id": "branding"}) or {}
    updates = {}
    if payload.logo_url is not None:
        logo_url = urlparse(payload.logo_url)
        if logo_url.scheme != "https" or not logo_url.netloc:
            raise HTTPException(status_code=422, detail="The organisation logo must use a secure URL")
        updates["logo_url"] = payload.logo_url
    if payload.seller is not None:
        updates["seller"] = {**(current.get("seller") or {}), **payload.seller.model_dump(exclude_unset=True)}
    if payload.pdf_design is not None:
        updates["pdf_design"] = payload.pdf_design.model_dump()
    if payload.branding_defaults is not None:
        updates["branding_defaults"] = payload.branding_defaults.model_dump()
    if payload.invoice_provider is not None:
        if payload.invoice_provider == "native" and not native_invoicing_enabled(user.org_id):
            raise HTTPException(status_code=403, detail="Native invoicing is disabled on this deployment")
        if payload.invoice_provider == "tally":
            if not tally_enabled(user.org_id):
                raise HTTPException(status_code=403, detail="Tally is disabled for this organization")
            if not await db.tally_integrations.find_one({"org_id": user.org_id}, {"_id": 1}):
                raise HTTPException(status_code=409, detail="Connect Tally before selecting it as the invoice provider")
        updates["invoice_provider"] = payload.invoice_provider
    effective_seller = updates.get("seller") or current.get("seller") or {}
    previous_gstin = str((current.get("seller") or {}).get("gstin") or "").upper()
    effective_gstin = str(effective_seller.get("gstin") or "").upper()
    if payload.e_invoice_applicability is not None:
        assessment = payload.e_invoice_applicability.model_dump(mode="json")
        if assessment["status"] != "unassessed" and assessment["gstin"] != effective_gstin:
            raise HTTPException(status_code=422, detail="E-invoice assessment GSTIN must match the seller GSTIN")
        assessment.update({"assessed_at": datetime.now(timezone.utc), "assessed_by": user.user_id})
        updates["e_invoice_applicability"] = assessment
    elif payload.seller is not None and effective_gstin != previous_gstin:
        updates["e_invoice_applicability"] = {
            "status": "unassessed",
            "gstin": effective_gstin,
            "reporting_window_days": None,
            "b2c_dynamic_qr_required": False,
            "not_required_reason": None,
            "assessed_at": None,
            "assessed_by": None,
        }
    if payload.invoice_controls is not None:
        current_controls = current.get("invoice_controls") or {}
        changes = payload.invoice_controls.model_dump(mode="json", exclude_unset=True)
        merged = {**current_controls, **{key: value for key, value in changes.items() if key != "numbering"}}
        if "numbering" in changes and changes["numbering"] is not None:
            merged_numbering = {**(current_controls.get("numbering") or {})}
            merged_numbering.update({key: value for key, value in changes["numbering"].items() if value is not None})
            merged["numbering"] = merged_numbering
        updates["invoice_controls"] = merged
    if not updates:
        raise HTTPException(status_code=422, detail="Add a quote or invoice setting to update")
    settings = await _settings_collection(user.org_id).find_one_and_update(
        {"_id": "branding"},
        {"$set": {
            "org_id": user.org_id,
            **updates,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": user.user_id,
        }},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return {"settings": {
        "logo_url": settings.get("logo_url", ""),
        "seller": settings.get("seller", {}),
        "pdf_design": settings.get("pdf_design") or QuoteDesign().model_dump(),
        "branding_defaults": settings.get("branding_defaults") or QuoteBrandingDefaults().model_dump(),
        "can_edit": True,
        "invoice_provider": await invoice_provider(user.org_id),
        "e_invoice_applicability": settings.get("e_invoice_applicability") or {
            "status": "unassessed", "gstin": (settings.get("seller") or {}).get("gstin", ""), "reporting_window_days": None,
            "b2c_dynamic_qr_required": False, "not_required_reason": None,
        },
        "invoice_controls": _invoice_controls_view(settings.get("invoice_controls")),
    }}


@router.put("/settings/iris-credentials")
@limiter.limit("5/minute")
async def update_iris_credentials(request: Request, payload: IrisCredentialsInput, user: CurrentUser):
    require_feature(user.org_id, "OFFERS")
    await admin_required(user)
    if response := reverification_required(user):
        return response
    if not native_invoicing_enabled(user.org_id):
        raise HTTPException(status_code=403, detail="Native invoicing is disabled on this deployment")
    settings = await _settings_collection(user.org_id).find_one({"_id": "branding"}) or {}
    seller_gstin = str((settings.get("seller") or {}).get("gstin") or "").upper()
    if payload.gstin.upper() != seller_gstin:
        raise HTTPException(status_code=422, detail="IRP credential GSTIN must match the configured seller GSTIN")
    status = await save_taxpayer_credentials(
        user.org_id,
        payload.gstin,
        payload.username,
        payload.password,
        user.user_id,
    )
    return {"iris_irp": status}


@router.put("/{quote_id}")
@limiter.limit("20/minute")
async def update_quote(request: Request, quote_id: str, payload: QuotePayload, user: CurrentUser):
    require_feature(user.org_id, "OFFERS")
    await _require_conversation(user, payload.conversation_id)
    collection = _collection(user.org_id)
    current = await collection.find_one({"org_id": user.org_id, "quote_id": quote_id})
    if not current:
        raise HTTPException(status_code=404, detail="Quote not found")
    if current.get("conversation_id") != payload.conversation_id:
        raise HTTPException(status_code=409, detail="A quote cannot be moved to another conversation")
    if payload.case_id and payload.case_id != current.get("case_id"):
        raise HTTPException(409, "A quote cannot be moved to another purchase")
    if payload.expected_revision is not None and payload.expected_revision != int(current.get("revision_number") or 1):
        raise HTTPException(status_code=409, detail="This quote was edited by someone else. Reload before saving.")
    if await collection.find_one({"org_id": user.org_id, "quote_number": payload.quote_number, "quote_id": {"$ne": quote_id}}, {"_id": 1}):
        raise HTTPException(status_code=409, detail="Quote number is already in use")
    replacement = _document(payload, user)
    if _quote_content(current) == _quote_content(replacement):
        current.pop("revisions", None)
        return {"quote": serialize_mongo(current)}
    version_filter = {"_id": current["_id"], "org_id": user.org_id}
    if payload.expected_revision is not None:
        version_filter["revision_number"] = payload.expected_revision
    version_filter["updated_at"] = current["updated_at"] if current.get("updated_at") else {"$exists": False}
    updated = await collection.find_one_and_update(
        version_filter,
        {
            "$set": {
                **replacement,
                "revision_number": int(current.get("revision_number") or 1) + 1,
                "revision_count": int(current.get("revision_count") or 0) + 1,
            },
            "$push": {"revisions": _revision_snapshot(current)},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This quote was edited by someone else. Reload before saving.")
    updated.pop("revisions", None)
    return {"quote": serialize_mongo(updated)}


@router.get("/{quote_id}/revisions")
@limiter.limit("60/minute")
async def get_quote_revisions(request: Request, quote_id: str, user: CurrentUser):
    quote = await _collection(user.org_id).find_one(
        {"org_id": user.org_id, "quote_id": quote_id},
        {"_id": 0, "quote_id": 1, "conversation_id": 1, "revision_number": 1, "revisions": 1},
    )
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    await _require_conversation(user, quote["conversation_id"])
    return {
        "quote_id": quote_id,
        "current_revision": int(quote.get("revision_number") or 1),
        "revisions": serialize_mongo(list(reversed(quote.get("revisions") or []))),
    }


@router.post("/assets")
@limiter.limit("20/minute")
async def upload_quote_asset(request: Request, user: CurrentUser, file: UploadFile = File(...)):
    require_feature(user.org_id, "OFFERS")
    allowed = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
    if file.content_type not in allowed or not file.size or file.size > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Use a JPG or PNG image up to 5 MB")
    signature = await file.read(8)
    await file.seek(0)
    if not signature.startswith(allowed[file.content_type]):
        raise HTTPException(status_code=400, detail="The uploaded image is invalid")
    result = await upload_media_to_cloudinary(file=file, platform="quote", org_id=user.org_id)
    if not result or not result.get("file_url"):
        raise HTTPException(status_code=503, detail="Image upload is temporarily unavailable")
    return {"asset": result}
