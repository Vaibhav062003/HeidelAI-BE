import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from services import shopify_growth as growth

router = APIRouter(tags=["Commerce growth"])


def workspace_scope(request: Request, user: CurrentUser):
    expected = request.headers.get("x-heidel-organization")
    if expected and expected != user.org_id:
        raise HTTPException(409, "Your workspace changed. Open this screen again before saving")
    return None


async def workspace_admin(request: Request, user: CurrentUser):
    workspace_scope(request, user)
    await admin_required(user)


class Audience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["whatsapp"] = "whatsapp"
    segment: Literal["first_time", "repeat", "lapsed"] = "first_time"
    lapsed_days: int = Field(default=30, ge=7, le=365)


class Campaign(Audience):
    name: str = Field(min_length=1, max_length=100)
    scheduled_for: datetime
    template_name: str = Field(default="", max_length=512)
    template_language: str = Field(default="en", max_length=20)
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("scheduled_for")
    @classmethod
    def future_date(cls, value):
        if value.tzinfo is None or not datetime.now(timezone.utc) - timedelta(seconds=10) <= value <= datetime.now(timezone.utc) + timedelta(days=90):
            raise ValueError("Choose a date within the next 90 days, including its time zone")
        return value

    @field_validator("name")
    @classmethod
    def plain_line(cls, value):
        if "\n" in value or "\r" in value:
            raise ValueError("Use a single line")
        return value.strip()

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value):
        if not value:
            raise ValueError("Enter a campaign name")
        return value


class Rate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(ge=0, le=10000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attribution_days: int = Field(default=7, ge=1, le=30)
    message_rate: Rate | None = None


@router.get("/api/commerce/shopify/sales")
async def sales(user: CurrentUser, days: int = Query(default=30, ge=1, le=90)):
    return await growth.sales_report(user.org_id, days)


@router.get("/api/commerce/shopify/growth-settings")
async def get_settings(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return await growth.settings(user.org_id)


@router.put("/api/commerce/shopify/growth-settings")
async def put_settings(request: Request, payload: Settings, user: CurrentUser):
    await workspace_admin(request, user)
    await growth.connected(user.org_id)
    return await growth.save_settings(user.org_id, payload.model_dump(mode="json"))


@router.get("/api/commerce/shopify/campaigns")
async def campaigns(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return {"items": await growth.list_campaigns(user.org_id)}


@router.get("/api/commerce/shopify/campaign-templates")
async def templates(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return {"items": await growth.campaign_templates(user.org_id)}


@router.post("/api/commerce/shopify/campaigns/preview")
@limiter.limit("10/minute")
async def preview(request: Request, payload: Audience, user: CurrentUser):
    await workspace_admin(request, user)
    return await growth.audience(user.org_id, payload.model_dump())


@router.post("/api/commerce/shopify/campaigns", status_code=201)
@limiter.limit("10/minute")
async def schedule(request: Request, payload: Campaign, user: CurrentUser):
    await workspace_admin(request, user)
    values = payload.model_dump(exclude={"request_id"})
    return await growth.create_campaign(user.org_id, values, payload.request_id)


@router.delete("/api/commerce/shopify/campaigns/{identifier}", status_code=200)
async def cancel(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    await growth.cancel_campaign(user.org_id, identifier)
    return {"status": "cancelled"}


async def bounded_body(request, limit):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "Payload exceeds the upload limit")
    return bytes(raw)


@router.post("/api/shopify/storefront/consent")
@limiter.limit("30/minute")
async def consent(request: Request):
    if request.headers.get("x-heidel-cart") != "1" or len(request.url.query) > 4096:
        raise HTTPException(403, "Use the store consent form")
    return await growth.capture_marketing_consent(request.url.query, await bounded_body(request, 1024))


@router.get("/api/shopify/storefront/consent")
@limiter.limit("30/minute")
async def consent_preferences(request: Request):
    if len(request.url.query) > 4096:
        raise HTTPException(413, "Consent query is too large")
    from services.shopify_storefront_cart import authenticated_customer
    s = growth.service()
    shop, customer_id = authenticated_customer(request.url.query, b"", s.SHOPIFY_CLIENT_SECRET, s.utcnow().timestamp())
    connection = await s.db.shopify_connections.find_one({"shop": shop, "status": "connected"})
    if not connection:
        raise HTTPException(409, "Store integration disabled")
    customer = (await s._graphql(connection, growth.CUSTOMER_QUERY, {"id": customer_id})).get("customer")
    if not customer:
        raise HTTPException(401, "Customer unavailable")
    return {"whatsapp": await growth.customer_consent(connection["org_id"], customer, "whatsapp")}

# Owner reports use Shopify calendar dates; exports preserve the same definitions.
from datetime import date
from fastapi.responses import RedirectResponse, Response
from services import shopify_reporting as reporting, shopify_retargeting as retargeting


class RetargetingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    read_enabled: bool = True
    unconfirmed_enabled: bool = True
    read_delay_minutes: int = Field(default=5, ge=1, le=10080)
    unconfirmed_delay_minutes: int = Field(default=30, ge=1, le=10080)
    read_template_name: str = Field(default="", max_length=512)
    read_template_language: str = Field(default="en_US", max_length=20)
    unconfirmed_template_name: str = Field(default="", max_length=512)
    unconfirmed_template_language: str = Field(default="en_US", max_length=20)


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    holdout_percent: int = Field(default=10, ge=5, le=50)
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("Enter a single-line experiment name")
        return value


class PixelEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    installation_token: str = Field(min_length=32, max_length=100)
    event_id: str = Field(min_length=1, max_length=100)
    client_id: str = Field(min_length=1, max_length=100)
    event_name: Literal["page_viewed", "product_viewed", "product_added_to_cart", "cart_viewed", "checkout_started", "checkout_completed"]
    touch_token: str | None = Field(default=None, max_length=100)
    variant_id: str | None = Field(default=None, max_length=100, pattern=r"^gid://shopify/ProductVariant/\d+$")


@router.get("/api/commerce/shopify/reports")
async def reports(request: Request, user: CurrentUser, start: date | None = None, end: date | None = None, days: int = Query(default=30, ge=1, le=366), compare: bool = True):
    workspace_scope(request, user)
    return await reporting.report(user.org_id, start, end, days, compare)


@router.get("/api/commerce/shopify/reports/export")
@limiter.limit("10/minute")
async def export_report(request: Request, user: CurrentUser, view: Literal["summary", "sources", "recovery", "branches", "blockers", "orders", "campaigns", "customers", "retention", "products", "trends"] = "orders", start: date | None = None, end: date | None = None, days: int = Query(default=30, ge=1, le=366)):
    await workspace_admin(request, user)
    data = await reporting.report(user.org_id, start, end, days, False)
    return Response(reporting.csv_report(data, view), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="shopify-{view}.csv"', "Cache-Control": "no-store"})


@router.get("/api/commerce/shopify/retargeting")
async def retarget_config(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return {"settings": await retargeting.settings(user.org_id), "jobs": await retargeting.jobs(user.org_id), "templates": await retargeting.compatible_templates(user.org_id)}


@router.put("/api/commerce/shopify/retargeting")
async def save_retargeting(request: Request, payload: RetargetingSettings, user: CurrentUser):
    await workspace_admin(request, user)
    return await retargeting.save_settings(user.org_id, payload.model_dump())


@router.post("/api/commerce/shopify/retargeting/preview")
async def preview_retargeting(request: Request, payload: RetargetingSettings, user: CurrentUser):
    await workspace_admin(request, user)
    now = growth.service().utcnow()
    return {"read_due_at": now + timedelta(minutes=payload.read_delay_minutes), "unconfirmed_due_at": now + timedelta(minutes=payload.unconfirmed_delay_minutes), "max_followups": 1, "expires_after_days": 7, "content": "Cart reminder or purchase help only. Promotions need separate marketing consent."}


@router.delete("/api/commerce/shopify/retargeting/{identifier}")
async def cancel_retargeting(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    result = await growth.service().db.commerce_retargeting_jobs.update_one({"_id": identifier, "org_id": user.org_id, "status": {"$in": ["observing", "pending", "sending"]}, "provider_started_at": {"$exists": False}}, {"$set": {"status": "cancelled", "reason": "merchant_cancelled", "updated_at": growth.service().utcnow()}, "$unset": {"claim_token": "", "claim_expires_at": ""}})
    if not result.modified_count:
        raise HTTPException(409, "This follow-up is already submitted or finished")
    return {"status": "cancelled"}


@router.get("/api/commerce/shopify/experiments")
async def list_experiments(request: Request, user: CurrentUser):
    workspace_scope(request, user)
    return {"items": await reporting.experiments(user.org_id)}


@router.post("/api/commerce/shopify/experiments", status_code=201)
@limiter.limit("5/minute")
async def start_experiment(request: Request, payload: Experiment, user: CurrentUser):
    await workspace_admin(request, user)
    return await reporting.create_experiment(user.org_id, payload.name, payload.holdout_percent, payload.request_id)


@router.delete("/api/commerce/shopify/experiments/{identifier}")
async def stop_experiment(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    result = await growth.service().db.commerce_experiments.update_one({"org_id": user.org_id, "experiment_id": identifier, "status": "running"}, {"$set": {"status": "stopped", "stopped_at": growth.service().utcnow()}})
    if not result.modified_count:
        raise HTTPException(409, "Experiment already stopped or unavailable")
    return {"status": "stopped"}


@router.get("/api/commerce/shopify/weekly")
async def weekly(request: Request, user: CurrentUser):
    workspace_scope(request, user)
    rows = await growth.service().db.commerce_weekly_reports.find({"org_id": user.org_id}).sort("week_ending", -1).limit(52).to_list(52)
    return {"items": [{"report_id": row["_id"], "week_ending": row["week_ending"], "created_at": row["created_at"], "summary": row["snapshot"]["summary"], "comparison": row["snapshot"].get("comparison"), "actions": row["snapshot"]["actions"]} for row in rows]}


@router.get("/api/commerce/shopify/weekly/{identifier}")
async def weekly_detail(request: Request, identifier: str, user: CurrentUser):
    workspace_scope(request, user)
    row = await growth.service().db.commerce_weekly_reports.find_one({"_id": identifier, "org_id": user.org_id})
    if not row:
        raise HTTPException(404, "Weekly report unavailable")
    return row["snapshot"]


@router.get("/api/commerce/shopify/weekly/{identifier}/export")
async def weekly_export(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    data = await weekly_detail(request, identifier, user)
    return Response(reporting.csv_report(data, "orders"), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="shopify-weekly-orders.csv"', "Cache-Control": "no-store"})


@router.api_route("/api/commerce/shopify/link/{token}", methods=["GET", "HEAD"])
@limiter.limit("120/minute")
async def recovery_link(request: Request, token: str):
    if not 20 <= len(token) <= 100:
        raise HTTPException(404, "Recovery link unavailable")
    destination = await reporting.resolve_link(token, request.headers.get("user-agent", ""), request.method == "HEAD")
    return RedirectResponse(destination, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/api/commerce/shopify/pixel")
@limiter.limit("120/minute")
async def pixel_event(request: Request):
    raw = await bounded_body(request, 4096)
    try:
        payload = PixelEvent.model_validate_json(raw)
    except ValueError:
        raise HTTPException(422, "Invalid pixel event") from None
    return Response(content=json.dumps(await reporting.capture_pixel(payload.model_dump())), media_type="application/json", headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


@router.post("/api/commerce/shopify/pixel/install")
async def install_pixel(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return await reporting.install_pixel(user.org_id)


@router.post("/api/commerce/shopify/pixel/connect")
async def connect_pixel(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    connection = await growth.connected(user.org_id)
    return {"authorization_url": await growth.service().create_oauth_url(user.org_id, user.user_id, connection["shop"], analytics=True)}
