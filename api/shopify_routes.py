from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from services.shopify_service import (
    commerce_summary,
    complete_oauth,
    conversation_activity,
    create_default_template,
    create_oauth_url,
    disconnect_shopify,
    get_connection,
    list_compatible_templates,
    list_records,
    process_webhook,
    request_sync,
    update_settings,
)


router = APIRouter(tags=["Shopify commerce"])
MAX_WEBHOOK_BYTES = 2_000_000


class OAuthStartPayload(BaseModel):
    shop: str = Field(min_length=15, max_length=255)


class ShopifySettingsPayload(BaseModel):
    whatsapp_id: str = Field(default="", max_length=200)
    template_name: str = Field(default="", max_length=512)
    template_language: str = Field(default="en", min_length=1, max_length=20)
    reminder_delay_minutes: int = 30
    automation_enabled: bool = False

    @field_validator("whatsapp_id", "template_name", "template_language")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class TemplateCreatePayload(BaseModel):
    whatsapp_id: str = Field(min_length=1, max_length=200)
    language: str = Field(default="en", min_length=1, max_length=20)


@router.get("/api/integrations/shopify")
async def shopify_integration(user: CurrentUser):
    return await get_connection(user.org_id)


@router.post("/api/integrations/shopify/oauth/start")
async def shopify_oauth_start(payload: OAuthStartPayload, user: CurrentUser):
    await admin_required(user)
    return {"authorization_url": await create_oauth_url(user.org_id, user.user_id, payload.shop)}


@router.get("/api/integrations/shopify/oauth/callback", response_class=HTMLResponse)
async def shopify_oauth_callback(request: Request):
    params = {key: value for key, value in request.query_params.multi_items()}
    await complete_oauth(params)
    # No organization, token, or store data is exposed to the opener.
    html = """<!doctype html><html><body><p>Shopify connected. You may close this window.</p>
<script>if(window.opener){window.opener.postMessage({type:'shopify-connected'},'*')}window.close()</script>
</body></html>"""
    return HTMLResponse(html, headers={"Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'none'; frame-ancestors 'none'", "Cache-Control": "no-store"})


@router.get("/api/integrations/shopify/templates")
async def shopify_templates(user: CurrentUser, whatsapp_id: str = Query(default="", max_length=200)):
    return {"templates": await list_compatible_templates(user.org_id, whatsapp_id or None)}


@router.post("/api/integrations/shopify/template")
async def shopify_create_template(payload: TemplateCreatePayload, user: CurrentUser):
    await admin_required(user)
    return await create_default_template(user.org_id, payload.whatsapp_id, payload.language)


@router.put("/api/integrations/shopify/settings")
async def shopify_settings(payload: ShopifySettingsPayload, user: CurrentUser):
    await admin_required(user)
    return await update_settings(user.org_id, payload.model_dump())


@router.post("/api/integrations/shopify/sync", status_code=202)
async def shopify_sync(user: CurrentUser):
    await admin_required(user)
    await request_sync(user.org_id)
    return {"status": "queued"}


@router.delete("/api/integrations/shopify", status_code=204)
async def shopify_disconnect(user: CurrentUser):
    await admin_required(user)
    await disconnect_shopify(user.org_id)
    return Response(status_code=204)


@router.post("/api/webhooks/shopify")
async def shopify_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(default=""),
    x_shopify_shop_domain: str = Header(default=""),
    x_shopify_webhook_id: str = Header(default=""),
    x_shopify_topic: str = Header(default=""),
    x_shopify_triggered_at: str = Header(default=""),
):
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Shopify webhook is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Shopify webhook is too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    status = await process_webhook(raw, {
        "x-shopify-hmac-sha256": x_shopify_hmac_sha256,
        "x-shopify-shop-domain": x_shopify_shop_domain,
        "x-shopify-webhook-id": x_shopify_webhook_id,
        "x-shopify-topic": x_shopify_topic,
        "x-shopify-triggered-at": x_shopify_triggered_at,
    })
    return {"status": status}


@router.get("/api/commerce/shopify/summary")
async def shopify_summary(user: CurrentUser):
    return await commerce_summary(user.org_id)


@router.get("/api/commerce/shopify/records")
async def shopify_records(
    user: CurrentUser,
    type: Literal["checkout", "order"] = Query(default="checkout"),
    status: str = Query(default="", max_length=80),
    search: str = Query(default="", max_length=80),
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=30, ge=1, le=50),
):
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="Start date must be before end date")
    return await list_records(user.org_id, type, status=status, search=search, start=start, end=end, cursor=cursor, limit=limit)


@router.get("/api/conversations/{conversation_id}/shopify-activity")
async def shopify_conversation_activity(conversation_id: str, user: CurrentUser):
    if len(conversation_id) > 240:
        raise HTTPException(status_code=422, detail="Invalid conversation ID")
    return await conversation_activity(user.org_id, conversation_id)
