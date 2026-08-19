from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.tally_service import (
    authenticate_connector,
    claim_next_job,
    create_connector_key,
    create_tally_invoice_draft,
    create_payment_reminder,
    cancel_payment_reminder,
    finish_job,
    get_customer_mapping,
    get_integration,
    get_profit_loss_report,
    get_tally_command_job,
    link_tally_customer,
    list_bills,
    list_payment_reminders,
    list_tally_invoices,
    list_tally_ledgers,
    list_tally_stock_items,
    list_tally_units,
    normalized_settings,
    queue_invoice_pdf,
    queue_profit_loss_report,
    queue_tally_master,
    queue_tally_sync,
    review_tally_invoice_draft,
    retry_bill,
    reschedule_payment_reminder,
    save_integration,
    store_tally_pdf_artifact,
    submit_tally_invoice_draft,
    tally_overview,
)
from services.utils import serialize_mongo
from services.work_team_service import list_work_teams


router = APIRouter(tags=["Tally"])
db = get_async_mongo_db()


class TallySettingsPayload(BaseModel):
    company_name: str = Field(min_length=1, max_length=120)
    voucher_type: str = Field(default="Sales", min_length=1, max_length=80)
    sales_ledger: str = Field(default="Sales", min_length=1, max_length=120)
    stock_unit: str = Field(default="Nos", min_length=1, max_length=30)
    tax_mode: Literal["none", "cgst_sgst", "igst"] = "cgst_sgst"
    cgst_ledger: str = Field(default="CGST", max_length=120)
    sgst_ledger: str = Field(default="SGST", max_length=120)
    igst_ledger: str = Field(default="IGST", max_length=120)
    shipping_ledger: str = Field(default="", max_length=120)
    discount_ledger: str = Field(default="", max_length=120)
    reminder_days_after: list[int] = Field(default_factory=lambda: [0, 3, 7], max_length=10)
    reminders_enabled: bool = False
    reminder_time: str = Field(default="10:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    reminder_timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=80)
    reminder_template_name: str = Field(default="", max_length=512)
    reminder_template_language: str = Field(default="en", min_length=1, max_length=20)

    @field_validator(
        "company_name", "voucher_type", "sales_ledger", "stock_unit", "cgst_ledger",
        "sgst_ledger", "igst_ledger", "shipping_ledger", "discount_ledger", "reminder_timezone",
        "reminder_template_name", "reminder_template_language",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("reminder_days_after")
    @classmethod
    def validate_reminders(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 365 for day in value):
            raise ValueError("Reminder days must be between 0 and 365")
        return sorted(set(value))

    @field_validator("reminder_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Choose a valid reminder timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_reminder_setup(self):
        if self.reminders_enabled and not self.reminder_template_name:
            raise ValueError("Choose an approved WhatsApp payment template before enabling reminders")
        return self


class ConnectorResponsePayload(BaseModel):
    key: Literal["ledgers", "stock_items", "units", "invoices", "outstandings", "profit_loss", "master", "invoice"]
    xml: str = Field(max_length=10_000_000)


class ConnectorResultPayload(BaseModel):
    outcome: Literal["success", "failed", "unknown"]
    voucher_number: Optional[str] = Field(default=None, max_length=120)
    master_id: Optional[str] = Field(default=None, max_length=120)
    error: Optional[str] = Field(default=None, max_length=500)
    tally_response: Optional[str] = Field(default=None, max_length=4000)
    responses: list[ConnectorResponsePayload] = Field(default_factory=list, max_length=10)


class TallyMasterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    state: str = Field(default="", max_length=80)
    gstin: str = Field(default="", max_length=20)
    registration_type: str = Field(default="Regular", max_length=40)
    phone: str = Field(default="", max_length=30)
    email: str = Field(default="", max_length=200)
    address: str = Field(default="", max_length=500)
    gst_applicable: str = Field(default="Applicable", max_length=40)
    tax_type: str = Field(default="", max_length=10)
    parent: str = Field(default="", max_length=80)
    ledger_type: str = Field(default="", max_length=80)
    formal_name: str = Field(default="", max_length=80)
    uqc: str = Field(default="", max_length=20)
    model_number: str = Field(default="", max_length=120)
    unit: str = Field(default="Nos", max_length=30)
    hsn: str = Field(default="", max_length=30)
    standard_rate: float = Field(default=0, ge=0, le=1000000000)

    @field_validator("name")
    @classmethod
    def strip_master_name(cls, value: str) -> str:
        return value.strip()


class InvoiceDraftCreatePayload(BaseModel):
    source: Literal["command", "quote"]
    command: str = Field(default="", max_length=500)
    quote_id: str = Field(default="", max_length=120)


class InvoiceDraftItemPayload(BaseModel):
    master_id: str = Field(min_length=1, max_length=120)
    quantity: float = Field(gt=0, le=10000)
    rate: float = Field(gt=0, le=1000000000)
    gst_rate: float = Field(default=0, ge=0, le=100)
    gst_inclusive: bool = False


class InvoiceDraftReviewPayload(BaseModel):
    party_master_id: str = Field(min_length=1, max_length=120)
    sales_ledger: str = Field(min_length=1, max_length=120)
    tax_mode: Literal["none", "cgst_sgst", "igst"]
    items: list[InvoiceDraftItemPayload] = Field(min_length=1, max_length=100)


class ProfitLossPayload(BaseModel):
    from_date: date
    to_date: date


class CustomerLinkPayload(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)


class ReminderPayload(BaseModel):
    scheduled_at: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _require_accounting_access(user: CurrentUser) -> None:
    if user.role in {"admin", "org:admin"}:
        return
    teams = await list_work_teams(user.org_id)
    if not any(team.get("type") == "billing" and user.user_id in team.get("member_ids", []) for team in teams):
        raise HTTPException(status_code=403, detail="Tally accounting is available to admins and Billing team members")


@router.get("/api/integrations/tally")
@limiter.limit("60/minute")
async def get_tally_integration(request: Request, user: CurrentUser):
    integration = await get_integration(user.org_id)
    settings = normalized_settings(integration)
    last_seen = integration.get("last_seen_at")
    connected = bool(last_seen and _utc(last_seen) >= datetime.now(timezone.utc) - timedelta(seconds=90))
    capabilities = integration.get("connector_capabilities") or []
    connector_version = str(integration.get("connector_version") or "")
    connector_version_number = int(connector_version) if connector_version.isdigit() else 0
    return {
        "configured": bool(settings["company_name"].strip()),
        "connector_created": bool(integration.get("connector_key_hash")),
        "connected": connected,
        "last_seen_at": serialize_mongo(last_seen),
        "last_error": integration.get("last_error"),
        "connector_version": connector_version,
        "connector_capabilities": capabilities,
        "connector_update_required": connected and (
            connector_version_number < 3 or not {"reports", "pdf"}.issubset(set(capabilities))
        ),
        "can_edit": user.role in {"admin", "org:admin"},
        "settings": settings,
    }


@router.put("/api/integrations/tally")
@limiter.limit("10/minute")
async def put_tally_integration(request: Request, payload: TallySettingsPayload, user: CurrentUser):
    await admin_required(user)
    integration = await save_integration(user.org_id, user.user_id, payload.model_dump())
    return {"settings": normalized_settings(integration)}


@router.post("/api/integrations/tally/connector-key")
@limiter.limit("5/minute")
async def post_tally_connector_key(request: Request, user: CurrentUser):
    await admin_required(user)
    integration = await get_integration(user.org_id)
    if not normalized_settings(integration)["company_name"].strip():
        raise HTTPException(status_code=409, detail="Save the Tally company settings first")
    return {"connector_key": await create_connector_key(user.org_id, user.user_id)}


@router.delete("/api/integrations/tally/connector-key")
@limiter.limit("5/minute")
async def delete_tally_connector_key(request: Request, user: CurrentUser):
    await admin_required(user)
    await db.tally_integrations.update_one(
        {"org_id": user.org_id},
        {"$unset": {"connector_key_hash": "", "connector_created_at": "", "connector_created_by": ""},
         "$set": {"last_seen_at": None, "updated_at": datetime.now(timezone.utc)}},
    )
    return {"disconnected": True}


@router.post("/api/quotes/{quote_id}/tally", status_code=201)
@limiter.limit("20/minute")
async def post_quote_to_tally(request: Request, quote_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    quote = await db[f"quotes_{user.org_id}"].find_one(
        {"org_id": user.org_id, "quote_id": quote_id}, {"_id": 0, "conversation_id": 1}
    )
    if not quote:
        raise HTTPException(status_code=404, detail="Quotation not found")
    draft = await create_tally_invoice_draft(
        user.org_id, user.user_id, user.role, quote["conversation_id"], {"source": "quote", "quote_id": quote_id}
    )
    return {"draft": draft, "created": True, "review_required": True}


@router.get("/api/conversations/{conversation_id}/tally-bills")
@limiter.limit("60/minute")
async def get_tally_bills(request: Request, conversation_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    return {
        "bills": await list_bills(user.org_id, conversation_id, user.user_id, user.role),
        "customer_mapping": await get_customer_mapping(user.org_id, conversation_id),
    }


@router.post("/api/tally-bills/{job_id}/retry")
@limiter.limit("10/minute")
async def post_tally_retry(request: Request, job_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    return {"bill": await retry_bill(user.org_id, job_id, user.user_id, user.role)}


@router.get("/api/tally/overview")
@limiter.limit("60/minute")
async def get_tally_overview(request: Request, user: CurrentUser):
    await _require_accounting_access(user)
    return {"overview": await tally_overview(user.org_id, user.role in {"admin", "org:admin"})}


@router.post("/api/tally/sync", status_code=202)
@limiter.limit("10/minute")
async def post_tally_sync(request: Request, user: CurrentUser):
    await _require_accounting_access(user)
    job, created = await queue_tally_sync(user.org_id, user.user_id, force=True)
    return {"job": job, "created": created}


@router.get("/api/tally/ledgers")
@limiter.limit("60/minute")
async def get_tally_ledgers(request: Request, user: CurrentUser, kind: str = "", q: str = ""):
    await _require_accounting_access(user)
    return {"ledgers": await list_tally_ledgers(user.org_id, kind, q)}


@router.get("/api/tally/stock-items")
@limiter.limit("60/minute")
async def get_tally_stock_items(request: Request, user: CurrentUser, q: str = ""):
    await _require_accounting_access(user)
    return {"stock_items": await list_tally_stock_items(user.org_id, q)}


@router.get("/api/tally/invoices")
@limiter.limit("60/minute")
async def get_tally_invoices(
    request: Request, user: CurrentUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
    q: str = Query("", max_length=120), status: str = Query("", max_length=20),
    from_date: Optional[date] = Query(None), to_date: Optional[date] = Query(None),
):
    await _require_accounting_access(user)
    return await list_tally_invoices(
        user.org_id, page, page_size, q, status,
        from_date.isoformat() if from_date else "", to_date.isoformat() if to_date else "",
    )


@router.get("/api/tally/profit-loss")
@limiter.limit("60/minute")
async def get_tally_profit_loss(
    request: Request, user: CurrentUser, from_date: date = Query(...), to_date: date = Query(...),
):
    await admin_required(user)
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="Profit & Loss start date must be before the end date")
    return {"report": await get_profit_loss_report(user.org_id, from_date.isoformat(), to_date.isoformat())}


@router.post("/api/tally/profit-loss", status_code=202)
@limiter.limit("10/minute")
async def post_tally_profit_loss(request: Request, payload: ProfitLossPayload, user: CurrentUser):
    await admin_required(user)
    if payload.from_date > payload.to_date:
        raise HTTPException(status_code=422, detail="Profit & Loss start date must be before the end date")
    job, created = await queue_profit_loss_report(
        user.org_id, user.user_id, payload.from_date.isoformat(), payload.to_date.isoformat()
    )
    return {"job": job, "created": created}


@router.get("/api/tally/jobs/{job_id}")
@limiter.limit("60/minute")
async def get_tally_job(request: Request, job_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    return {"job": await get_tally_command_job(user.org_id, job_id)}


@router.post("/api/tally/invoices/{invoice_id}/pdf", status_code=202)
@limiter.limit("20/minute")
async def post_tally_invoice_pdf(request: Request, invoice_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    job, created = await queue_invoice_pdf(user.org_id, user.user_id, invoice_id)
    return {"job": job, "created": created}


@router.put("/api/tally/customers/{party_master_id}/link")
@limiter.limit("20/minute")
async def put_tally_customer_link(
    request: Request, party_master_id: str, payload: CustomerLinkPayload, user: CurrentUser,
):
    await _require_accounting_access(user)
    return {"link": await link_tally_customer(user.org_id, user.user_id, party_master_id, payload.conversation_id)}


@router.get("/api/tally/invoices/{invoice_id}/reminders")
@limiter.limit("60/minute")
async def get_invoice_reminders(request: Request, invoice_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    return {"reminders": await list_payment_reminders(user.org_id, invoice_id)}


@router.post("/api/tally/invoices/{invoice_id}/reminders", status_code=201)
@limiter.limit("20/minute")
async def post_invoice_reminder(request: Request, invoice_id: str, payload: ReminderPayload, user: CurrentUser):
    await _require_accounting_access(user)
    return {"reminder": await create_payment_reminder(
        user.org_id, user.user_id, user.full_name or "Team member", invoice_id, payload.scheduled_at
    )}


@router.patch("/api/tally/reminders/{reminder_id}")
@limiter.limit("20/minute")
async def patch_invoice_reminder(request: Request, reminder_id: str, payload: ReminderPayload, user: CurrentUser):
    await _require_accounting_access(user)
    return {"reminder": await reschedule_payment_reminder(user.org_id, reminder_id, user.user_id, payload.scheduled_at)}


@router.delete("/api/tally/reminders/{reminder_id}")
@limiter.limit("20/minute")
async def delete_invoice_reminder(request: Request, reminder_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    return {"reminder": await cancel_payment_reminder(user.org_id, reminder_id, user.user_id)}


@router.get("/api/tally/units")
@limiter.limit("60/minute")
async def get_tally_units(request: Request, user: CurrentUser):
    await _require_accounting_access(user)
    return {"units": await list_tally_units(user.org_id)}


@router.post("/api/tally/masters/{kind}", status_code=202)
@limiter.limit("20/minute")
async def post_tally_master(
    request: Request,
    kind: Literal["party", "sales", "tax", "charge", "unit", "stock_item"],
    payload: TallyMasterPayload,
    user: CurrentUser,
):
    await admin_required(user)
    return {"job": await queue_tally_master(user.org_id, user.user_id, kind, payload.model_dump())}


@router.put("/api/tally/masters/{kind}/{master_id}", status_code=202)
@limiter.limit("20/minute")
async def put_tally_master(
    request: Request,
    kind: Literal["party", "sales", "tax", "charge", "unit", "stock_item"],
    master_id: str,
    payload: TallyMasterPayload,
    user: CurrentUser,
):
    await admin_required(user)
    return {"job": await queue_tally_master(
        user.org_id, user.user_id, kind, payload.model_dump(), master_id=master_id
    )}


@router.post("/api/conversations/{conversation_id}/tally-invoice-drafts", status_code=201)
@limiter.limit("20/minute")
async def post_tally_invoice_draft(
    request: Request, conversation_id: str, payload: InvoiceDraftCreatePayload, user: CurrentUser,
):
    await _require_accounting_access(user)
    return {"draft": await create_tally_invoice_draft(
        user.org_id, user.user_id, user.role, conversation_id, payload.model_dump()
    )}


@router.patch("/api/tally-invoice-drafts/{draft_id}")
@limiter.limit("30/minute")
async def patch_tally_invoice_draft(
    request: Request, draft_id: str, payload: InvoiceDraftReviewPayload, user: CurrentUser,
):
    await _require_accounting_access(user)
    return {"draft": await review_tally_invoice_draft(
        user.org_id, user.user_id, user.role, draft_id, payload.model_dump()
    )}


@router.post("/api/tally-invoice-drafts/{draft_id}/submit", status_code=202)
@limiter.limit("20/minute")
async def post_tally_invoice_draft_submit(request: Request, draft_id: str, user: CurrentUser):
    await _require_accounting_access(user)
    job, created = await submit_tally_invoice_draft(
        user.org_id, user.user_id, user.full_name or "Team member", user.role, draft_id
    )
    return {"bill": job, "created": created}


@router.get("/api/tally-connector/jobs/next")
@limiter.limit("30/minute")
async def get_tally_connector_job(
    request: Request,
    connector_key: Optional[str] = Header(default=None, alias="X-Tally-Connector-Key"),
    connector_capabilities: str = Header(default="", alias="X-Tally-Connector-Capabilities"),
    connector_version: str = Header(default="", alias="X-Tally-Connector-Version"),
):
    integration = await authenticate_connector(connector_key, connector_capabilities, connector_version)
    return {"job": await claim_next_job(integration)}


@router.post("/api/tally-connector/jobs/{job_id}/result")
@limiter.limit("30/minute")
async def post_tally_connector_result(
    request: Request,
    job_id: str,
    payload: ConnectorResultPayload,
    connector_key: Optional[str] = Header(default=None, alias="X-Tally-Connector-Key"),
    claim_id: Optional[str] = Header(default=None, alias="X-Tally-Claim-Id"),
    connector_capabilities: str = Header(default="", alias="X-Tally-Connector-Capabilities"),
    connector_version: str = Header(default="", alias="X-Tally-Connector-Version"),
):
    integration = await authenticate_connector(connector_key, connector_capabilities, connector_version)
    return {"result": await finish_job(integration, job_id, claim_id, payload.model_dump())}


@router.post("/api/tally-connector/jobs/{job_id}/artifact")
@limiter.limit("20/minute")
async def post_tally_connector_artifact(
    request: Request,
    job_id: str,
    connector_key: Optional[str] = Header(default=None, alias="X-Tally-Connector-Key"),
    claim_id: Optional[str] = Header(default=None, alias="X-Tally-Claim-Id"),
    connector_capabilities: str = Header(default="", alias="X-Tally-Connector-Capabilities"),
    connector_version: str = Header(default="", alias="X-Tally-Connector-Version"),
):
    integration = await authenticate_connector(connector_key, connector_capabilities, connector_version)
    if not claim_id:
        raise HTTPException(status_code=409, detail="Missing Tally claim")
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/pdf":
        raise HTTPException(status_code=415, detail="Tally artifacts must be PDF")
    return {"pdf": await store_tally_pdf_artifact(integration["org_id"], job_id, claim_id, await request.body())}
