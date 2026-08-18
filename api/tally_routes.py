from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.tally_service import (
    authenticate_connector,
    claim_next_job,
    create_connector_key,
    finish_job,
    get_integration,
    list_bills,
    normalized_settings,
    queue_bill,
    retry_bill,
    save_integration,
)
from services.utils import serialize_mongo


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

    @field_validator(
        "company_name", "voucher_type", "sales_ledger", "stock_unit", "cgst_ledger",
        "sgst_ledger", "igst_ledger", "shipping_ledger", "discount_ledger",
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


class QueueBillPayload(BaseModel):
    party_ledger: str = Field(min_length=1, max_length=120)
    tax_mode: Literal["none", "cgst_sgst", "igst"]

    @field_validator("party_ledger")
    @classmethod
    def strip_party(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Tally party ledger is required")
        return value


class ConnectorResultPayload(BaseModel):
    outcome: Literal["success", "failed", "unknown"]
    voucher_number: Optional[str] = Field(default=None, max_length=120)
    master_id: Optional[str] = Field(default=None, max_length=120)
    error: Optional[str] = Field(default=None, max_length=500)
    tally_response: Optional[str] = Field(default=None, max_length=4000)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.get("/api/integrations/tally")
@limiter.limit("60/minute")
async def get_tally_integration(request: Request, user: CurrentUser):
    integration = await get_integration(user.org_id)
    settings = normalized_settings(integration)
    last_seen = integration.get("last_seen_at")
    connected = bool(last_seen and _utc(last_seen) >= datetime.now(timezone.utc) - timedelta(seconds=90))
    return {
        "configured": bool(settings["company_name"].strip()),
        "connector_created": bool(integration.get("connector_key_hash")),
        "connected": connected,
        "last_seen_at": serialize_mongo(last_seen),
        "last_error": integration.get("last_error"),
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
async def post_quote_to_tally(request: Request, quote_id: str, payload: QueueBillPayload, user: CurrentUser):
    job, created = await queue_bill(
        user.org_id, user.user_id, user.full_name or "Team member", user.role, quote_id, payload.model_dump()
    )
    return {"bill": job, "created": created}


@router.get("/api/conversations/{conversation_id}/tally-bills")
@limiter.limit("60/minute")
async def get_tally_bills(request: Request, conversation_id: str, user: CurrentUser):
    return {"bills": await list_bills(user.org_id, conversation_id, user.user_id, user.role)}


@router.post("/api/tally-bills/{job_id}/retry")
@limiter.limit("10/minute")
async def post_tally_retry(request: Request, job_id: str, user: CurrentUser):
    return {"bill": await retry_bill(user.org_id, job_id, user.user_id, user.role)}


@router.get("/api/tally-connector/jobs/next")
@limiter.limit("30/minute")
async def get_tally_connector_job(
    request: Request,
    connector_key: Optional[str] = Header(default=None, alias="X-Tally-Connector-Key"),
):
    integration = await authenticate_connector(connector_key)
    return {"job": await claim_next_job(integration)}


@router.post("/api/tally-connector/jobs/{job_id}/result")
@limiter.limit("30/minute")
async def post_tally_connector_result(
    request: Request,
    job_id: str,
    payload: ConnectorResultPayload,
    connector_key: Optional[str] = Header(default=None, alias="X-Tally-Connector-Key"),
    claim_id: Optional[str] = Header(default=None, alias="X-Tally-Claim-Id"),
):
    integration = await authenticate_connector(connector_key)
    return {"bill": await finish_job(integration, job_id, claim_id, payload.model_dump())}
