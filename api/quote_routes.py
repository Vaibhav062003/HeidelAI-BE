import uuid
from datetime import date, datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.cloudinary_service import upload_media_to_cloudinary
from services.quote_math import quote_line_amounts
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


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
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=100)
    gstin: str = Field(default="", max_length=30)
    address: str = Field(default="", max_length=500)
    payment_details: str = Field(default="", max_length=500)
    logo_url: str = Field(default="", max_length=2048)


class QuoteItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=80)
    description: str = Field(..., min_length=1, max_length=240)
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
    id: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=40)
    enabled: bool = True


class QuoteDesign(BaseModel):
    name: str = Field(default="Custom", min_length=1, max_length=40)
    headerColor: str = Field(default="#111315", pattern=r"^#[0-9A-Fa-f]{6}$")
    accentColor: str = Field(default="#F7AD0A", pattern=r"^#[0-9A-Fa-f]{6}$")
    pageColor: str = Field(default="#FBFAF7", pattern=r"^#[0-9A-Fa-f]{6}$")
    textColor: str = Field(default="#0E1113", pattern=r"^#[0-9A-Fa-f]{6}$")
    typography: Literal["modern", "editorial"] = "modern"
    fontScale: float = Field(default=1, ge=0.9, le=1.15)
    density: Literal["compact", "comfortable"] = "compact"
    headerStyle: Literal["solid", "light"] = "solid"
    tableStyle: Literal["striped", "ruled"] = "striped"
    totalStyle: Literal["filled", "outline"] = "filled"
    showCustomerBlock: bool = True
    showMetadata: bool = True
    showSellerDetails: bool = True
    showPaymentDetails: bool = True
    showNotes: bool = True
    showTerms: bool = True
    showPageNumbers: bool = True


class QuoteBranding(BaseModel):
    document_title: str = Field(default="Quotation", min_length=1, max_length=60)
    build_title: str = Field(default="Performance Workstation", max_length=80)
    build_label: str = Field(default="Custom PC", max_length=30)
    build_promise: str = Field(default="Manufacturer warranty / GST invoice", max_length=100)
    theme: Literal["slate", "emerald", "blue", "burgundy"] = "slate"
    cover_image_url: str = Field(default="", max_length=2048)
    footer_text: str = Field(default="Thank you for your business.", max_length=240)
    component_categories: list[QuoteComponentCategory] = Field(default_factory=list, max_length=20)
    design: Optional[QuoteDesign] = None


class QuoteSettingsPayload(BaseModel):
    logo_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    seller: Optional[QuoteParty] = None


class QuotePayload(BaseModel):
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
    data = payload.model_dump(mode="json")
    stored_items = []
    subtotal = 0.0
    gst_total = 0.0
    for item in data["items"]:
        unit_price = item.pop("unit_price")
        base, gst, line_total = quote_line_amounts(
            item["quantity"], unit_price, item["gst_rate"], item["gst_inclusive"]
        )
        subtotal += base
        gst_total += gst
        stored_items.append({
            **item,
            "private_unit_price": round(unit_price, 2),
            "private_line_subtotal": base,
            "public_line_total": line_total,
        })
    subtotal = round(subtotal, 2)
    gst_total = round(gst_total, 2)
    discount = min(data["discount"], subtotal + gst_total + data["shipping"])
    return {
        **data,
        "org_id": user.org_id,
        "items": stored_items,
        "private_subtotal": subtotal,
        "private_gst_total": gst_total,
        "discount": round(discount, 2),
        "public_grand_total": round(subtotal + gst_total + data["shipping"] - discount, 2),
        "updated_at": datetime.now(timezone.utc),
        "updated_by": user.user_id,
        "updated_by_name": user.full_name or "Team member",
    }


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
    conversation_id: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(10, ge=1, le=50),
):
    await _require_conversation(user, conversation_id)
    quotes = await _collection(user.org_id).find(
        {"org_id": user.org_id, "conversation_id": conversation_id},
        {"revisions": 0},
    ).sort("created_at", -1).to_list(length=limit)
    return {"quotes": serialize_mongo(quotes)}


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def create_quote(request: Request, payload: QuotePayload, user: CurrentUser):
    await _require_conversation(user, payload.conversation_id)
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
    return {"quote": serialize_mongo(document)}


@router.get("/settings")
@limiter.limit("60/minute")
async def get_quote_settings(request: Request, user: CurrentUser):
    settings = await _settings_collection(user.org_id).find_one({"_id": "branding"})
    organization = await db.organizations.find_one({"org_id": user.org_id}, {"org_name": 1})
    seller = (settings or {}).get("seller") or {}
    return {
        "settings": {
            "logo_url": (settings or {}).get("logo_url", ""),
            "seller": {
                "name": seller.get("name") or (organization or {}).get("org_name") or "Organization",
                "phone": seller.get("phone", ""),
                "email": seller.get("email", ""),
                "gstin": seller.get("gstin", ""),
                "address": seller.get("address", ""),
                "payment_details": seller.get("payment_details", ""),
            },
            "can_edit": user.role in {"admin", "org:admin"},
        }
    }


@router.put("/settings")
@limiter.limit("10/minute")
async def update_quote_settings(request: Request, payload: QuoteSettingsPayload, user: CurrentUser):
    await admin_required(user)
    updates = {}
    if payload.logo_url is not None:
        logo_url = urlparse(payload.logo_url)
        if logo_url.scheme != "https" or not logo_url.netloc:
            raise HTTPException(status_code=422, detail="The organisation logo must use a secure URL")
        updates["logo_url"] = payload.logo_url
    if payload.seller is not None:
        updates["seller"] = payload.seller.model_dump()
    if not updates:
        raise HTTPException(status_code=422, detail="Add a logo or seller profile to update")
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
    return {"settings": {"logo_url": settings.get("logo_url", ""), "seller": settings.get("seller", {}), "can_edit": True}}


@router.put("/{quote_id}")
@limiter.limit("20/minute")
async def update_quote(request: Request, quote_id: str, payload: QuotePayload, user: CurrentUser):
    await _require_conversation(user, payload.conversation_id)
    collection = _collection(user.org_id)
    current = await collection.find_one({"org_id": user.org_id, "quote_id": quote_id})
    if not current:
        raise HTTPException(status_code=404, detail="Quote not found")
    if current.get("conversation_id") != payload.conversation_id:
        raise HTTPException(status_code=409, detail="A quote cannot be moved to another conversation")
    replacement = _document(payload, user)
    if _quote_content(current) == _quote_content(replacement):
        current.pop("revisions", None)
        return {"quote": serialize_mongo(current)}
    version_filter = {"_id": current["_id"], "org_id": user.org_id}
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
