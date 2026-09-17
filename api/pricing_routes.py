import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from config import settings
from core.limiter import limiter
from database import get_async_mongo_db
from services.pricing_decision_service import (
    evaluate_human_pricing,
    get_pricing_settings,
    update_pricing_settings,
)
from services.pricing_policy import PriceSnapshot
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


router = APIRouter(tags=["Pricing intelligence"])


class PricingSettingsPayload(BaseModel):
    mode: Literal["off", "shadow", "review"]


class PricingEvaluationPayload(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    temp_id: str = Field(min_length=1, max_length=160)
    content: str = Field(default="", max_length=10_000)
    context: dict | None = None


class MinimumUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str = Field(min_length=1, max_length=240)
    expected_version: str = Field(min_length=1, max_length=240)
    request_id: str = Field(min_length=1, max_length=120)
    minimum: str = Field(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,2})?$")


class MinimumBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MinimumUpdate] = Field(min_length=1, max_length=50)


class MinimumReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "reject", "verify"]


class MinimumLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skus: list[str] = Field(min_length=1, max_length=50)


@router.post("/api/pricing/daily-prices/resolve")
@limiter.limit("10/minute")
async def resolve_minimum_products(request: Request, payload: MinimumLookup, user: CurrentUser):
    from services.product_request_service import catalogue_org
    if any(not value.strip() or len(value) > 240 for value in payload.skus):
        raise HTTPException(422, "Each SKU must contain between 1 and 240 characters")
    catalog = await catalogue_org(user.org_id)
    patterns = [re.compile("^" + re.escape(value.strip()) + "$", re.I) for value in payload.skus]
    rows = await get_async_mongo_db()[f"products_{catalog}"].find(
        {"$or": [{key: {"$in": patterns}} for key in ("unique_identifier", "model_no", "sku")]},
        {"unique_identifier": 1, "model_no": 1, "sku": 1, "price_version": 1},
    ).limit(101).to_list(101)
    if len(rows) > 100:
        raise HTTPException(409, "Catalogue identifiers are ambiguous; resolve duplicate SKUs first")
    return serialize_mongo({"items": rows})


@router.get("/api/pricing/daily-prices")
@limiter.limit("30/minute")
async def daily_prices(request: Request, user: CurrentUser, q: str = Query("", max_length=120),
                       page: int = Query(1, ge=1, le=10000), requested_only: bool = False):
    from services.daily_pricing_service import daily_prices as load
    return serialize_mongo(await load(user, q, page, requested_only))


@router.post("/api/pricing/daily-prices")
@limiter.limit("10/minute")
async def update_minimums(request: Request, payload: MinimumBatch, user: CurrentUser):
    from services.daily_pricing_service import request_minimum
    if len({row.product_id for row in payload.items}) != len(payload.items):
        raise HTTPException(422, "A product can appear only once in a batch")
    results = []
    for row in payload.items:
        try:
            results.append({"product_id": row.product_id, **await request_minimum(user, row.model_dump())})
        except (HTTPException, ValueError) as exc:
            results.append({"product_id": row.product_id, "status": "failed", "error": str(exc.detail if isinstance(exc, HTTPException) else exc)})
    return serialize_mongo({"results": results})


@router.post("/api/pricing/daily-prices/changes/{change_id}")
@limiter.limit("10/minute")
async def review_minimum(request: Request, change_id: str, payload: MinimumReview, user: CurrentUser):
    from services.daily_pricing_service import resolve_minimum
    return serialize_mongo(await resolve_minimum(user, change_id, payload.action))


@router.get("/api/pricing/daily-prices/{product_id}/history")
@limiter.limit("30/minute")
async def minimum_history(request: Request, product_id: str, user: CurrentUser):
    from services.b2b_pricing_service import product_history
    return serialize_mongo(await product_history(user, product_id))


@router.get("/api/pricing/settings")
@limiter.limit("60/minute")
async def pricing_settings(request: Request, user: CurrentUser):
    return serialize_mongo({"settings": await get_pricing_settings(user.org_id)})


@router.put("/api/pricing/settings")
@limiter.limit("10/minute")
async def set_pricing_settings(request: Request, payload: PricingSettingsPayload, user: CurrentUser):
    await admin_required(user)
    try:
        updated = await update_pricing_settings(user.org_id, payload.mode, user.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return serialize_mongo({"settings": updated})


@router.post("/api/pricing/evaluate")
@limiter.limit("30/minute")
async def evaluate_pricing(request: Request, payload: PricingEvaluationPayload, user: CurrentUser):
    await require_conversation_access(
        user.org_id, payload.conversation_id, user.user_id, user.role,
    )
    try:
        decision = await evaluate_human_pricing(
            org_id=user.org_id,
            user_id=user.user_id,
            conversation_id=payload.conversation_id,
            temp_id=payload.temp_id,
            content=payload.content,
            context=payload.context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return serialize_mongo({"decision": decision})


@router.get("/api/pricing/catalog-health")
@limiter.limit("30/minute")
async def pricing_catalog_health(request: Request, user: CurrentUser):
    db = get_async_mongo_db()
    organization = await db.organizations.find_one(
        {"org_id": user.org_id}, {"sales_catalogue_org_id": 1},
    ) or {}
    configured_catalogue = str(organization.get("sales_catalogue_org_id") or "")
    catalogue_org_id = (
        configured_catalogue
        if re.fullmatch(r"[A-Za-z0-9_-]+", configured_catalogue)
        else user.org_id
    )
    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=settings.PRICING_FLOOR_MAX_AGE_MINUTES)
    health = {
        "total_products": 0,
        "healthy_products": 0,
        "unverified_products": 0,
        "stale_products": 0,
        "invalid_price_order_products": 0,
    }
    async for product in db[f"products_{catalogue_org_id}"].find({}):
        health["total_products"] += 1
        try:
            snapshot = PriceSnapshot.from_product(product)
            errors = snapshot.invariant_errors()
            stale = not snapshot.is_fresh(now=now, max_age=max_age)
            if "target_below_floor" in errors:
                health["invalid_price_order_products"] += 1
            if stale:
                health["stale_products"] += 1
            if errors or stale:
                health["unverified_products"] += 1
            else:
                health["healthy_products"] += 1
        except (TypeError, ValueError):
            health["unverified_products"] += 1
    health["healthy_percent"] = round(
        health["healthy_products"] * 100 / health["total_products"], 1,
    ) if health["total_products"] else 0
    health["max_age_minutes"] = int(max_age.total_seconds() / 60)
    return {"catalog_org_id": catalogue_org_id, "catalog": health}


@router.get("/api/internal/pricing/health")
async def internal_pricing_health(
    org_id: str = Query(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$"),
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
):
    expected = str(settings.X_INTERNAL_SECRET or "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=401, detail="Unauthorized internal access")
    latest = await get_async_mongo_db()[f"pricing_decisions_{org_id}"].find_one(
        {}, {"_id": 0, "org_id": 0}, sort=[("created_at", -1)],
    )
    return serialize_mongo({
        "settings": await get_pricing_settings(org_id),
        "latest_decision": latest,
    })
