from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from services import commerce_knowledge as knowledge
from services import shopify_growth as growth

router = APIRouter(tags=["Commerce growth"])


async def workspace_admin(request: Request, user: CurrentUser):
    expected = request.headers.get("x-heidel-organization")
    if expected and expected != user.org_id:
        raise HTTPException(409, "Your workspace changed. Open this screen again before saving")
    await admin_required(user)


class Audience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["whatsapp", "email"] = "whatsapp"
    segment: Literal["first_time", "repeat", "lapsed"] = "first_time"
    lapsed_days: int = Field(default=30, ge=7, le=365)


class Campaign(Audience):
    name: str = Field(min_length=1, max_length=100)
    scheduled_for: datetime
    template_name: str = Field(default="", max_length=512)
    template_language: str = Field(default="en", max_length=20)
    email_subject: str = Field(default="", max_length=200)
    email_body: str = Field(default="", max_length=20_000)
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("scheduled_for")
    @classmethod
    def future_date(cls, value):
        if value.tzinfo is None or not datetime.now(timezone.utc) - timedelta(seconds=10) <= value <= datetime.now(timezone.utc) + timedelta(days=90):
            raise ValueError("Choose a date within the next 90 days, including its time zone")
        return value

    @field_validator("name", "email_subject")
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
    email_recovery_enabled: bool = False
    email_subject: str = Field(default="Your cart is waiting", min_length=1, max_length=200)
    email_body: str = Field(default="You left something in your cart. Continue shopping using the link below.", min_length=1, max_length=20_000)

    @field_validator("email_subject")
    @classmethod
    def single_line(cls, value):
        if "\r" in value or "\n" in value:
            raise ValueError("Use a single line for the subject")
        return value


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["faq", "website"]
    content: str = Field(min_length=1, max_length=100_000)

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value):
        if not value.strip():
            raise ValueError("Enter a source title")
        return value.strip()


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


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
    if payload.channel == "email" and (not payload.email_subject.strip() or not payload.email_body.strip()):
        raise HTTPException(422, "Enter an email subject and message")
    values = payload.model_dump(exclude={"request_id"})
    return await growth.create_campaign(user.org_id, values, payload.request_id)


@router.delete("/api/commerce/shopify/campaigns/{identifier}", status_code=200)
async def cancel(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    await growth.cancel_campaign(user.org_id, identifier)
    return {"status": "cancelled"}


@router.get("/api/ai/knowledge")
async def get_knowledge(request: Request, user: CurrentUser):
    await workspace_admin(request, user)
    return await knowledge.configuration(user.org_id)


@router.put("/api/ai/knowledge")
async def enable_knowledge(request: Request, payload: KnowledgeSettings, user: CurrentUser):
    await workspace_admin(request, user)
    await growth.connected(user.org_id)
    if payload.enabled and not (await knowledge.configuration(user.org_id))["provider_ready"]:
        raise HTTPException(409, "Configure the Google AI API key before enabling buying answers")
    await growth.service().db.commerce_knowledge_settings.update_one({"_id": user.org_id}, {"$set": {"enabled": payload.enabled}}, upsert=True)
    return await knowledge.configuration(user.org_id)


@router.post("/api/ai/knowledge/sources", status_code=201)
@limiter.limit("10/minute")
async def add_source(request: Request, payload: Source, user: CurrentUser):
    await workspace_admin(request, user)
    from services.entitlement_service import require_feature
    require_feature(user.org_id, "AI")
    try:
        text = await knowledge.import_website(payload.content) if payload.kind == "website" else knowledge.extract_text(payload.content.encode(), "faq.txt")
    except knowledge.httpx.HTTPError:
        raise HTTPException(422, "Website could not be reached securely; upload its content instead") from None
    return await knowledge.save_source(user.org_id, payload.title, text, payload.kind, payload.content if payload.kind == "website" else None)


async def bounded_body(request, limit):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, "Payload exceeds the upload limit")
    return bytes(raw)


@router.post("/api/ai/knowledge/document", status_code=201)
@limiter.limit("10/minute")
async def document(request: Request, user: CurrentUser, filename: str = Query(min_length=1, max_length=200)):
    await workspace_admin(request, user)
    from services.entitlement_service import require_feature
    require_feature(user.org_id, "AI")
    raw = await bounded_body(request, 1_000_000)
    return await knowledge.save_source(user.org_id, filename, knowledge.extract_text(raw, filename), "document")


@router.delete("/api/ai/knowledge/sources/{identifier}")
async def remove_source(request: Request, identifier: str, user: CurrentUser):
    await workspace_admin(request, user)
    await growth.service().db.commerce_knowledge_sources.delete_one({"org_id": user.org_id, "source_id": identifier})
    return await knowledge.configuration(user.org_id)


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
    return {channel: await growth.customer_consent(connection["org_id"], customer, channel) for channel in ("email", "whatsapp")}


@router.get("/api/commerce/email/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_form(token: str):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
        raise HTTPException(404, "Link not found")
    return HTMLResponse('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Unsubscribe</title><main><h1>Email preferences</h1><p>Stop marketing emails from this store.</p><form method="post"><button type="submit">Unsubscribe</button></form></main></html>',
                        headers={"Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


@router.post("/api/commerce/email/unsubscribe/{token}", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def unsubscribe(request: Request, token: str):
    await growth.unsubscribe_email(token)
    return HTMLResponse("<!doctype html><html lang='en'><title>Unsubscribed</title><main><h1>You're unsubscribed</h1><p>You will no longer receive marketing emails from this store.</p></main></html>", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
