import base64
import binascii
from datetime import date
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from auth.auth_utils import reverification_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.entitlement_service import feature_access, require_feature
from services.feature_flags import native_invoicing_enabled
from services.india_tax import gst_state_name, is_valid_gstin
from services.native_invoice_service import (
    create_invoice,
    duplicate_invoice,
    get_invoice,
    export_invoices_csv,
    export_invoices_json,
    export_invoices_xlsx,
    invoice_audit,
    invoice_pdf_link,
    invoice_provider,
    invoice_report,
    invoice_summary,
    invoice_view,
    issue_invoice,
    list_invoices,
    record_payment,
    render_invoice_preview,
    render_invoice_receipt,
    render_invoice_pdf,
    invoice_receipt_link,
    reverse_payment,
    set_invoice_archived,
    set_invoice_closed,
    update_invoice,
    void_invoice,
)
from services.native_invoice_adjustment_service import (
    adjustment_pdf_link,
    adjustment_view,
    create_adjustment,
    get_adjustment,
    issue_adjustment,
    list_adjustments,
    record_refund,
    render_adjustment_pdf,
    reverse_refund,
    update_adjustment,
    void_adjustment,
)
from services.native_invoice_asset_service import (
    delete_invoice_attachment,
    list_invoice_attachment_metadata,
    store_invoice_attachment,
)
from services.native_invoice_assets import ATTACHMENT_MAX_BYTES
from services.native_invoice_master_service import (
    customer_presets,
    item_presets,
)
from services.work_team_service import list_work_teams


router = APIRouter(prefix="/api/native-invoices", tags=["Native invoices"])


class InvoiceParty(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    gstin: str = Field(default="", max_length=15)
    address: str = Field(min_length=1, max_length=500)
    state_code: str = Field(pattern=r"^\d{2}$")
    legal_name: str = Field(default="", max_length=120)
    trade_name: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=120)
    pincode: str = Field(default="", pattern=r"^(?:|\d{6})$")
    payment_details: str = Field(default="", max_length=500)
    phone: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=100)
    country_code: Literal["IN"] = "IN"

    @field_validator("gstin")
    @classmethod
    def validate_gstin(cls, value: str) -> str:
        value = value.upper()
        if value and not is_valid_gstin(value):
            raise ValueError("GSTIN checksum is invalid")
        return value

    @field_validator("state_code")
    @classmethod
    def validate_state_code(cls, value: str) -> str:
        gst_state_name(value)
        return value


class InvoiceItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    hsn_sac: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9.-]+$")
    quantity: Decimal = Field(gt=0, le=9999, decimal_places=4)
    uqc: str = Field(min_length=1, max_length=10)
    unit_price_paise: int = Field(ge=0, le=999_999_999_00)
    gst_rate_bps: int = Field(default=0, ge=0, le=10_000)
    gst_inclusive: bool = False
    supply_type: Literal["goods", "services"] = "goods"
    line_kind: Literal["item", "shipping", "surcharge"] = "item"

    @model_validator(mode="after")
    def validate_classification_code(self):
        if not self.hsn_sac.isdigit():
            raise ValueError("HSN/SAC must contain digits only")
        if self.supply_type == "goods" and len(self.hsn_sac) not in {4, 6, 8}:
            raise ValueError("Goods require a 4, 6, or 8 digit HSN code")
        if self.supply_type == "services" and (len(self.hsn_sac) != 6 or not self.hsn_sac.startswith("99")):
            raise ValueError("Services require a 6 digit SAC code beginning with 99")
        return self


class InvoiceDraftPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    invoice_date: date
    due_date: Optional[date] = None
    seller: InvoiceParty
    customer: InvoiceParty
    place_of_supply_state_code: str = Field(pattern=r"^\d{2}$")
    shipping_address: str = Field(default="", max_length=500)
    shipping_state_code: Optional[str] = Field(default=None, pattern=r"^\d{2}$")
    authorized_signatory: str = Field(min_length=1, max_length=120)
    reverse_charge: bool = False
    currency: Literal["INR"] = "INR"
    supply_category: Literal["domestic"] = "domestic"
    items: list[InvoiceItem] = Field(min_length=1, max_length=100)
    discount_paise: int = Field(default=0, ge=0, le=999_999_999_00)
    discount: Optional["InvoiceDiscount"] = None
    notes: str = Field(default="", max_length=1200)
    terms: list[str] = Field(default_factory=list, max_length=20)
    document_references: list["DocumentReference"] = Field(default_factory=list, max_length=20)
    source_quote_id: Optional[str] = Field(default=None, max_length=100)
    source_quote_revision: Optional[int] = Field(default=None, ge=1)
    conversation_id: Optional[str] = Field(default=None, max_length=200)
    presentation: Optional["InvoicePresentation"] = None
    signature_image: Optional["SignatureImage"] = None
    logo_image: Optional["LogoImage"] = None
    payment_profile: Optional["PaymentProfile"] = None

    @field_validator("place_of_supply_state_code")
    @classmethod
    def validate_place_of_supply(cls, value: str) -> str:
        gst_state_name(value)
        return value

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if any(len(value) > 500 for value in cleaned):
            raise ValueError("Each invoice term must be 500 characters or fewer")
        return cleaned

    @model_validator(mode="after")
    def validate_document(self):
        if not self.seller.gstin:
            raise ValueError("Supplier GSTIN is required for a GST tax invoice")
        if self.seller.gstin[:2] != self.seller.state_code:
            raise ValueError("Supplier GSTIN and state code do not match")
        if self.customer.gstin and self.customer.gstin[:2] != self.customer.state_code:
            raise ValueError("Customer GSTIN and state code do not match")
        if self.shipping_address:
            self.shipping_state_code = self.shipping_state_code or self.customer.state_code
            gst_state_name(self.shipping_state_code)
        if self.due_date and self.due_date < self.invoice_date:
            raise ValueError("Due date cannot be before the invoice date")
        if bool(self.source_quote_id) != bool(self.source_quote_revision):
            raise ValueError("Source quote ID and revision must be supplied together")
        if self.discount:
            if self.discount.kind == "fixed":
                amount = int(self.discount.amount_paise or 0)
                if self.discount_paise not in {0, amount}:
                    raise ValueError("Legacy and structured fixed discounts do not match")
                self.discount_paise = amount
            elif self.discount_paise:
                raise ValueError("Percentage discount cannot be combined with legacy discount_paise")
        return self


PRESENTATION_COLUMNS = {"description", "hsn_sac", "quantity", "unit_rate", "gst_rate", "tax", "total"}
PRESENTATION_LABELS = {
    "title", "supplier", "recipient", "delivery_address", "items", "items_continued", "gst_summary", "totals",
    "notes", "terms", "authorization", "verification", "attachments", "page", "invoice_number", "invoice_date",
    "due_date", "place_of_supply", "reverse_charge", "hsn_sac", "quantity", "unit_rate", "taxable", "gst_rate",
    "tax", "line_total", "subtotal", "discount", "taxable_value", "invoice_total", "authorised_signatory",
}


class InvoicePresentation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    page_format: Literal["A4", "LETTER"] = "A4"
    template_id: Literal["classic", "compact", "branded"] = "classic"
    orientation: Literal["portrait", "landscape"] = "portrait"
    logo_size: Literal["small", "medium", "large"] = "medium"
    accent_color: str = Field(default="#2D5267", pattern=r"^#[0-9A-Fa-f]{6}$")
    labels: dict[str, str] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=lambda: [
        "description", "hsn_sac", "quantity", "unit_rate", "gst_rate", "tax", "total",
    ])

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) - PRESENTATION_LABELS:
            raise ValueError("Presentation contains an unsupported label")
        if any(not label or len(label) > 40 or any(ord(character) < 32 or ord(character) == 127 for character in label) for label in value.values()):
            raise ValueError("Presentation labels must contain 1-40 characters")
        return value

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: list[str]) -> list[str]:
        if len(value) != len(PRESENTATION_COLUMNS) or set(value) != PRESENTATION_COLUMNS:
            raise ValueError("Presentation columns must include every statutory invoice column exactly once")
        return value


class SignatureImage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    filename: str = Field(min_length=1, max_length=80)
    content_type: Literal["image/png", "image/jpeg"]
    byte_size: int = Field(gt=0, le=96 * 1024)
    data_base64: str = Field(min_length=4, max_length=132_000)

    @model_validator(mode="after")
    def validate_encoded_bytes(self):
        try:
            decoded = base64.b64decode(self.data_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Signature image is not valid base64") from exc
        if len(decoded) != self.byte_size:
            raise ValueError("Signature image byte_size does not match its data")
        return self


class LogoImage(SignatureImage):
    byte_size: int = Field(gt=0, le=64 * 1024)
    data_base64: str = Field(min_length=4, max_length=90_000)


class PaymentProfile(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    account_name: str = Field(default="", max_length=120)
    bank_name: str = Field(default="", max_length=120)
    account_number: str = Field(default="", max_length=34, pattern=r"^[A-Za-z0-9 -]*$")
    ifsc: str = Field(default="", pattern=r"^(?:|[A-Z]{4}0[A-Z0-9]{6})$")
    upi_id: str = Field(default="", max_length=100, pattern=r"^(?:|[A-Za-z0-9._-]{2,64}@[A-Za-z0-9.-]{2,32})$")
    qr_enabled: bool = False

    @field_validator("ifsc")
    @classmethod
    def normalize_ifsc(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_payment_profile(self):
        if self.qr_enabled and not self.upi_id:
            raise ValueError("UPI ID is required when the payment QR is enabled")
        if bool(self.account_number) != bool(self.ifsc):
            raise ValueError("Bank account number and IFSC must be supplied together")
        return self


class InvoiceDiscount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fixed", "percentage"]
    amount_paise: Optional[int] = Field(default=None, ge=0, le=999_999_999_00)
    percentage_bps: Optional[int] = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def validate_discount(self):
        if self.kind == "fixed" and (self.amount_paise is None or self.percentage_bps is not None):
            raise ValueError("Fixed discount requires amount_paise only")
        if self.kind == "percentage" and (self.percentage_bps is None or self.amount_paise is not None):
            raise ValueError("Percentage discount requires percentage_bps only")
        return self


class DocumentReference(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kind: Literal["purchase_order", "delivery_note", "contract", "other"]
    reference: str = Field(min_length=1, max_length=120)
    date: Optional[date] = None
    note: str = Field(default="", max_length=240)


InvoiceDraftPayload.model_rebuild()


class InvoiceUpdatePayload(InvoiceDraftPayload):
    expected_revision: int = Field(ge=1)


class RevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class PaymentPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    amount_paise: int = Field(gt=0, le=999_999_999_00)
    paid_on: date
    method: Literal["bank_transfer", "upi", "cash", "cheque", "card", "other", "unspecified"] = "unspecified"
    reference: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def validate_reference(self):
        if self.method in {"bank_transfer", "upi", "cheque", "card"} and not self.reference:
            raise ValueError("Payment reference is required for this payment method")
        return self


class ReasonPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class VoidPayload(ReasonPayload):
    expected_revision: int = Field(ge=1)


class TransitionPayload(VoidPayload):
    pass


class AdjustmentItem(InvoiceItem):
    source_item_id: str = Field(min_length=1, max_length=80)


class AdjustmentDraftPayload(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    kind: Literal["credit_note", "debit_note"]
    note_date: date
    reason: str = Field(min_length=1, max_length=500)
    items: list[AdjustmentItem] = Field(min_length=1, max_length=100)
    discount_paise: int = Field(default=0, ge=0, le=999_999_999_00)


class AdjustmentUpdatePayload(AdjustmentDraftPayload):
    expected_revision: int = Field(ge=1)


class RefundPayload(PaymentPayload):
    credit_note_id: str = Field(min_length=1, max_length=80)
    refunded_on: date
    paid_on: Optional[date] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def remove_payment_date(self):
        return self


async def _require_native_access(user: CurrentUser, *, write: bool = False) -> None:
    if not native_invoicing_enabled(user.org_id):
        raise HTTPException(status_code=404, detail="Native invoicing is not enabled")
    access = feature_access(user.org_id, "NATIVE_INVOICING")
    if access["state"] == "locked" or (write and access["state"] != "active"):
        require_feature(user.org_id, "NATIVE_INVOICING")
    if user.role in {"admin", "org:admin"}:
        return
    teams = await list_work_teams(user.org_id)
    if not any(team.get("type") == "billing" and user.user_id in team.get("member_ids", []) for team in teams):
        raise HTTPException(status_code=403, detail="Native invoicing is available to admins and Billing team members")


async def _require_native_invoice_provider(user: CurrentUser) -> None:
    if await invoice_provider(user.org_id) != "native":
        raise HTTPException(status_code=409, detail="This organization uses Tally as its invoice provider")


def _require_admin_reverification(user: CurrentUser):
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="This action requires an organization administrator")
    return reverification_required(user)


def _idempotency(value: str) -> str:
    value = value.strip()
    if not 8 <= len(value) <= 200:
        raise HTTPException(status_code=422, detail="A valid idempotency key is required")
    return value


@router.get("")
@limiter.limit("60/minute")
async def get_native_invoices(
    request: Request,
    user: CurrentUser,
    search: str = Query(default="", max_length=100),
    lifecycle: str = Query(default="", pattern=r"^(|draft|issued|void)$"),
    payment_state: str = Query(default="", pattern=r"^(|unpaid|partially_paid|paid|overdue|credit_balance)$"),
    archived: str = Query(default="active", pattern=r"^(active|archived|all)$"),
    limit: int = Query(default=100, ge=1, le=100),
    cursor: str = Query(default="", max_length=1000),
    sort: str = Query(default="updated_at", pattern=r"^(updated_at|invoice_date|number|customer|total)$"),
    direction: str = Query(default="desc", pattern=r"^(asc|desc)$"),
):
    await _require_native_access(user)
    return await list_invoices(
        user.org_id, search=search, lifecycle=lifecycle, payment_state=payment_state, archived=archived,
        limit=limit, cursor=cursor, sort=sort, direction=direction,
    )


@router.get("/summary")
@limiter.limit("60/minute")
async def get_native_invoice_summary(request: Request, user: CurrentUser):
    await _require_native_access(user)
    return {"summary": await invoice_summary(user.org_id)}


@router.get("/reports")
@limiter.limit("30/minute")
async def get_native_invoice_report(
    request: Request,
    user: CurrentUser,
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
):
    await _require_native_access(user)
    return {"report": await invoice_report(user.org_id, from_date, to_date)}


@router.get("/export.csv")
@limiter.limit("10/minute")
async def get_native_invoice_csv_export(
    request: Request,
    user: CurrentUser,
    from_date: Optional[date] = Query(default=None),
    to_date: Optional[date] = Query(default=None),
):
    await _require_native_access(user)
    data, filename = await export_invoices_csv(user.org_id, from_date, to_date)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _export_response(data: bytes, filename: str, media_type: str) -> Response:
    return Response(content=data, media_type=media_type, headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/export.json")
@limiter.limit("10/minute")
async def get_native_invoice_json_export(
    request: Request, user: CurrentUser,
    from_date: Optional[date] = Query(default=None), to_date: Optional[date] = Query(default=None),
):
    await _require_native_access(user)
    return await _export_response(*(await export_invoices_json(user.org_id, from_date, to_date)), "application/json")


@router.get("/export.xlsx")
@limiter.limit("10/minute")
async def get_native_invoice_xlsx_export(
    request: Request, user: CurrentUser,
    from_date: Optional[date] = Query(default=None), to_date: Optional[date] = Query(default=None),
):
    await _require_native_access(user)
    return await _export_response(
        *(await export_invoices_xlsx(user.org_id, from_date, to_date)),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/presets/customers")
@limiter.limit("30/minute")
async def get_native_invoice_customer_presets(
    request: Request, user: CurrentUser,
    search: str = Query(default="", max_length=100), limit: int = Query(default=100, ge=1, le=100),
):
    await _require_native_access(user)
    return {"customers": await customer_presets(user.org_id, search, limit)}


@router.get("/presets/items")
@limiter.limit("30/minute")
async def get_native_invoice_item_presets(
    request: Request, user: CurrentUser,
    search: str = Query(default="", max_length=100), limit: int = Query(default=100, ge=1, le=100),
):
    await _require_native_access(user)
    return {"items": await item_presets(user.org_id, search, limit)}


@router.post("/preview")
@limiter.limit("20/minute")
async def post_native_invoice_preview(
    request: Request, payload: InvoiceDraftPayload, user: CurrentUser,
    invoice_id: str = Query(default="", max_length=80),
    expected_revision: Optional[int] = Query(default=None, ge=1),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    data = await render_invoice_preview(
        user.org_id,
        payload.model_dump(mode="json", exclude_unset=True),
        invoice_id=invoice_id,
        expected_revision=expected_revision,
    )
    return Response(content=data, media_type="application/pdf", headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": 'inline; filename="invoice-preview.pdf"',
        "X-Content-Type-Options": "nosniff",
    })


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def post_native_invoice(
    request: Request,
    payload: InvoiceDraftPayload,
    user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    return {"invoice": await create_invoice(user.org_id, user.user_id, user.full_name or "Team member", payload.model_dump(mode="json"), _idempotency(idempotency_key), user.role)}


@router.get("/{invoice_id}")
@limiter.limit("60/minute")
async def get_native_invoice(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user)
    return {"invoice": invoice_view(await get_invoice(user.org_id, invoice_id))}


@router.get("/{invoice_id}/audit")
@limiter.limit("30/minute")
async def get_native_invoice_audit(
    request: Request,
    invoice_id: str,
    user: CurrentUser,
    limit: int = Query(default=200, ge=1, le=500),
):
    await _require_native_access(user)
    return {"audit": await invoice_audit(user.org_id, invoice_id, limit)}


@router.put("/{invoice_id}")
@limiter.limit("30/minute")
async def put_native_invoice(request: Request, invoice_id: str, payload: InvoiceUpdatePayload, user: CurrentUser):
    await _require_native_access(user, write=True)
    values = payload.model_dump(mode="json", exclude_unset=True)
    expected_revision = values.pop("expected_revision")
    return {"invoice": await update_invoice(user.org_id, invoice_id, user.user_id, values, expected_revision, user.full_name or "Team member", user.role)}


@router.post("/{invoice_id}/duplicate", status_code=201)
@limiter.limit("20/minute")
async def post_native_invoice_duplicate(
    request: Request, invoice_id: str, payload: RevisionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    return {"invoice": await duplicate_invoice(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, _idempotency(idempotency_key),
        user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/archive")
@limiter.limit("20/minute")
async def post_native_invoice_archive(
    request: Request, invoice_id: str, payload: TransitionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    return {"invoice": await set_invoice_archived(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, True, _idempotency(idempotency_key),
        payload.reason, user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/unarchive")
@limiter.limit("20/minute")
async def post_native_invoice_unarchive(
    request: Request, invoice_id: str, payload: TransitionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    return {"invoice": await set_invoice_archived(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, False, _idempotency(idempotency_key),
        payload.reason, user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/close")
@limiter.limit("20/minute")
async def post_native_invoice_close(
    request: Request, invoice_id: str, payload: TransitionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"invoice": await set_invoice_closed(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, True, _idempotency(idempotency_key),
        payload.reason, user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/reopen")
@limiter.limit("20/minute")
async def post_native_invoice_reopen(
    request: Request, invoice_id: str, payload: TransitionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"invoice": await set_invoice_closed(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, False, _idempotency(idempotency_key),
        payload.reason, user.full_name or "Team member", user.role,
    )}


@router.get("/{invoice_id}/attachments")
@limiter.limit("60/minute")
async def get_native_invoice_attachments(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user)
    return {"attachments": await list_invoice_attachment_metadata(user.org_id, invoice_id)}


@router.post("/{invoice_id}/attachments", status_code=201)
@limiter.limit("12/minute")
async def post_native_invoice_attachment(
    request: Request,
    invoice_id: str,
    user: CurrentUser,
    expected_revision: int = Form(ge=1),
    file: UploadFile = File(),
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    try:
        data = await file.read(ATTACHMENT_MAX_BYTES + 1)
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Invoice attachment exceeds 3 MiB")
        return await store_invoice_attachment(
            user.org_id, invoice_id, user.user_id, expected_revision=expected_revision,
            idempotency_key=_idempotency(idempotency_key), filename=file.filename or "attachment",
            content_type=file.content_type or "application/octet-stream", data=data,
        )
    finally:
        await file.close()


@router.delete("/{invoice_id}/attachments/{attachment_id}")
@limiter.limit("12/minute")
async def delete_native_invoice_attachment(
    request: Request,
    invoice_id: str,
    attachment_id: str,
    user: CurrentUser,
    expected_revision: int = Query(ge=1),
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    return await delete_invoice_attachment(
        user.org_id, invoice_id, attachment_id, user.user_id, expected_revision=expected_revision,
        idempotency_key=_idempotency(idempotency_key),
    )


@router.get("/{invoice_id}/adjustments")
@limiter.limit("60/minute")
async def get_native_invoice_adjustments(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user)
    return {"adjustments": await list_adjustments(user.org_id, invoice_id)}


@router.post("/{invoice_id}/adjustments", status_code=201)
@limiter.limit("20/minute")
async def post_native_invoice_adjustment(
    request: Request, invoice_id: str, payload: AdjustmentDraftPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    return {"adjustment": await create_adjustment(
        user.org_id, invoice_id, user.user_id, payload.model_dump(mode="json"), _idempotency(idempotency_key),
        user.full_name or "Team member", user.role,
    )}


@router.get("/adjustments/{adjustment_id}")
@limiter.limit("60/minute")
async def get_native_invoice_adjustment(request: Request, adjustment_id: str, user: CurrentUser):
    await _require_native_access(user)
    return {"adjustment": adjustment_view(await get_adjustment(user.org_id, adjustment_id))}


@router.put("/adjustments/{adjustment_id}")
@limiter.limit("20/minute")
async def put_native_invoice_adjustment(
    request: Request, adjustment_id: str, payload: AdjustmentUpdatePayload, user: CurrentUser,
):
    await _require_native_access(user, write=True)
    values = payload.model_dump(mode="json")
    expected_revision = values.pop("expected_revision")
    return {"adjustment": await update_adjustment(
        user.org_id, adjustment_id, user.user_id, values, expected_revision,
        user.full_name or "Team member", user.role,
    )}


@router.post("/adjustments/{adjustment_id}/pdf/render")
@limiter.limit("10/minute")
async def post_native_invoice_adjustment_pdf_render(
    request: Request, adjustment_id: str, user: CurrentUser,
):
    await _require_native_access(user, write=True)
    try:
        stored = await render_adjustment_pdf(user.org_id, adjustment_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Credit/debit note PDF generation is temporarily unavailable") from exc
    return {"pdf": stored}


@router.get("/adjustments/{adjustment_id}/pdf-link")
@limiter.limit("30/minute")
async def get_native_invoice_adjustment_pdf_link(
    request: Request, adjustment_id: str, response: Response, user: CurrentUser,
):
    await _require_native_access(user)
    response.headers["Cache-Control"] = "private, no-store"
    return await adjustment_pdf_link(user.org_id, adjustment_id)


@router.post("/adjustments/{adjustment_id}/issue")
@limiter.limit("20/minute")
async def post_native_invoice_adjustment_issue(
    request: Request, adjustment_id: str, payload: RevisionPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    return {"adjustment": await issue_adjustment(
        user.org_id, adjustment_id, user.user_id, payload.expected_revision, _idempotency(idempotency_key),
        user.full_name or "Team member", user.role,
    )}


@router.post("/adjustments/{adjustment_id}/void")
@limiter.limit("20/minute")
async def post_native_invoice_adjustment_void(
    request: Request, adjustment_id: str, payload: VoidPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"adjustment": await void_adjustment(
        user.org_id, adjustment_id, user.user_id, payload.expected_revision, payload.reason,
        _idempotency(idempotency_key), user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/refunds")
@limiter.limit("20/minute")
async def post_native_invoice_refund(
    request: Request, invoice_id: str, payload: RefundPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    values = payload.model_dump(mode="json", exclude={"paid_on"}, exclude_none=True)
    return {"invoice": await record_refund(
        user.org_id, invoice_id, user.user_id, values, _idempotency(idempotency_key),
        user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/refunds/{refund_id}/reverse")
@limiter.limit("20/minute")
async def post_native_invoice_refund_reversal(
    request: Request,
    invoice_id: str,
    refund_id: str,
    payload: ReasonPayload,
    user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"invoice": await reverse_refund(
        user.org_id,
        invoice_id,
        refund_id,
        user.user_id,
        payload.reason,
        _idempotency(idempotency_key),
        user.full_name or "Team member",
        user.role,
    )}


@router.post("/{invoice_id}/issue")
@limiter.limit("20/minute")
async def post_native_invoice_issue(
    request: Request,
    invoice_id: str,
    payload: RevisionPayload,
    user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    await _require_native_invoice_provider(user)
    return {"invoice": await issue_invoice(user.org_id, invoice_id, user.user_id, payload.expected_revision, _idempotency(idempotency_key), user.full_name or "Team member", user.role)}


@router.post("/{invoice_id}/payments")
@limiter.limit("30/minute")
async def post_native_invoice_payment(
    request: Request,
    invoice_id: str,
    payload: PaymentPayload,
    user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    return {"invoice": await record_payment(user.org_id, invoice_id, user.user_id, payload.model_dump(mode="json"), _idempotency(idempotency_key), user.full_name or "Team member", user.role)}


@router.post("/{invoice_id}/payments/{payment_id}/reverse")
@limiter.limit("20/minute")
async def post_native_invoice_payment_reversal(
    request: Request, invoice_id: str, payment_id: str, payload: ReasonPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"invoice": await reverse_payment(
        user.org_id, invoice_id, payment_id, user.user_id, payload.reason, _idempotency(idempotency_key),
        user.full_name or "Team member", user.role,
    )}


@router.post("/{invoice_id}/void")
@limiter.limit("20/minute")
async def post_native_invoice_void(
    request: Request, invoice_id: str, payload: VoidPayload, user: CurrentUser,
    idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    await _require_native_access(user, write=True)
    if response := _require_admin_reverification(user):
        return response
    return {"invoice": await void_invoice(
        user.org_id, invoice_id, user.user_id, payload.expected_revision, payload.reason,
        _idempotency(idempotency_key), user.full_name or "Team member", user.role,
    )}


@router.put("/{invoice_id}/pdf")
@limiter.limit("10/minute")
async def put_native_invoice_pdf(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user, write=True)
    try:
        stored = await render_invoice_pdf(user.org_id, invoice_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Invoice PDF generation is temporarily unavailable") from exc
    return {"pdf": stored}


@router.post("/{invoice_id}/pdf/render")
@limiter.limit("10/minute")
async def post_native_invoice_pdf_render(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user, write=True)
    try:
        stored = await render_invoice_pdf(user.org_id, invoice_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Invoice PDF generation is temporarily unavailable") from exc
    return {"pdf": stored}


@router.get("/{invoice_id}/pdf-link")
@limiter.limit("30/minute")
async def get_native_invoice_pdf_link(request: Request, invoice_id: str, response: Response, user: CurrentUser):
    await _require_native_access(user)
    response.headers["Cache-Control"] = "private, no-store"
    return await invoice_pdf_link(user.org_id, invoice_id)


@router.post("/{invoice_id}/receipt/render")
@limiter.limit("10/minute")
async def post_native_invoice_receipt_render(request: Request, invoice_id: str, user: CurrentUser):
    await _require_native_access(user, write=True)
    try:
        return {"receipt": await render_invoice_receipt(user.org_id, invoice_id, user.user_id)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Payment receipt generation is temporarily unavailable") from exc


@router.get("/{invoice_id}/receipt-link")
@limiter.limit("30/minute")
async def get_native_invoice_receipt_link(request: Request, invoice_id: str, response: Response, user: CurrentUser):
    await _require_native_access(user)
    response.headers["Cache-Control"] = "private, no-store"
    return await invoice_receipt_link(user.org_id, invoice_id)
