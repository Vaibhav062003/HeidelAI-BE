import re
import asyncio
from typing import Literal
from bson import ObjectId

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.utils import serialize_mongo
from services.work_team_service import conversation_access_condition
from services.product_request_policy import available_stock
from services.product_request_service import (
    catalogue_org, review_request, restock_preview, prepare_template, queue_restock,
    retry_restock,
)

router = APIRouter(prefix="/api/product-requests", tags=["Product requests"])


class Review(BaseModel):
    expected_revision: int = Field(ge=0)
    verification_status: Literal["confirmed", "needs_review", "rejected"]
    product_name: str | None = Field(None, min_length=1, max_length=240)
    product_id: str | None = Field(None, min_length=1, max_length=120)
    status: Literal["requested", "dismissed", "fulfilled"] = "requested"
    consent_note: str | None = Field(None, max_length=1000)


class Audience(BaseModel):
    request_ids: list[str] = Field(min_length=1, max_length=100)


class SendAudience(Audience):
    revisions: dict[str, int] = Field(max_length=100)


class Template(BaseModel):
    phone_number_id: str = Field(min_length=1, max_length=128, pattern=r"^\d+$")


def admin(user):
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(403, "Only an administrator can create templates or send a broadcast")


@router.get("")
@limiter.limit("30/minute")
async def list_requests(request: Request, user: CurrentUser,
    q: str = Query("", max_length=120), status: Literal["requested", "dismissed", "fulfilled", "all"] = "requested",
    phone_number_id: str = Query("", max_length=128), product_id: str = Query("", max_length=120),
    review: Literal["all", "unmatched", "matched"] = "all", page: int = Query(1, ge=1, le=10000),
    verification: Literal["confirmed", "needs_review", "rejected", "all"] = "confirmed"):
    db = get_async_mongo_db()
    match = {"org_id": user.org_id, "archived_at": {"$exists": False}}
    if status != "all":
        match["status"] = status
    if phone_number_id:
        match["business_phone_id"] = phone_number_id
    if product_id:
        match["product_id"] = product_id
    if review != "all":
        match.setdefault("$and", []).append({"product_id": {"$exists": review == "matched"}})
    if q.strip():
        match["$or"] = [{field: {"$regex": re.escape(q.strip()), "$options": "i"}}
            for field in ("product_name", "catalogue_name", "customer_name", "customer_phone")]
    access = await conversation_access_condition(user.org_id, user.user_id, user.role)
    pipeline = [{"$match": match}, {"$addFields": {
        "verification_status": {"$ifNull": ["$verification_status", {"$cond": ["$reviewed_by", "confirmed", "needs_review"]}]},
        "revision": {"$ifNull": ["$revision", 0]},
    }}]
    if verification == "needs_review":
        pipeline.append({"$match": {"$or": [{"verification_status": "needs_review"}, {"review_hold": True}]}})
    elif verification != "all":
        pipeline.append({"$match": {"verification_status": verification, "review_hold": {"$ne": True}}})
    pipeline.append({"$sort": {"last_asked_at": -1}})
    # Access is evaluated against current conversation teams, not a stale copied owner.
    pipeline += [{"$lookup": {"from": f"conversations_{user.org_id}", "let": {"cid": "$conversation_id"},
        "pipeline": [{"$match": {"$and": [{"$expr": {"$eq": ["$conversation_id", "$$cid"]}}, access]}}, {"$project": {"_id": 1}}],
        "as": "accessible"}}, {"$match": {"accessible.0": {"$exists": True}}},
        {"$facet": {"items": [{"$skip": (page - 1) * 50}, {"$limit": 50}, {"$project": {"accessible": 0}}], "total": [{"$count": "count"}]}}]
    result = await db.product_requests.aggregate(pipeline).to_list(1)
    data = result[0]
    # Resolve the selected catalogue once and batch only this page's exact IDs.
    selected_catalogue = await catalogue_org(user.org_id)
    ids = {row.get("product_id") for row in data["items"] if row.get("product_id") and row.get("catalogue_org_id") == selected_catalogue}
    lookup_ids = list(ids) + [ObjectId(value) for value in ids if ObjectId.is_valid(value)]
    products = await db[f"products_{selected_catalogue}"].find({"_id": {"$in": lookup_ids}}).to_list(100)
    from services.stock_service import effective_stock_product
    limit = asyncio.Semaphore(5)
    async def resolve(product):
        async with limit:
            return await effective_stock_product(selected_catalogue, product, database=db)
    products = await asyncio.gather(*(resolve(product) for product in products))
    products_by_id = {str(product["_id"]): product for product in products}
    message_ids = [row["notification_message_id"] for row in data["items"] if row.get("notification_message_id")]
    messages = await db[f"messages_{user.org_id}"].find(
        {"message_id": {"$in": message_ids}}, {"message_id": 1, "status": 1, "delivery_error": 1},
    ).to_list(50)
    delivery = {message["message_id"]: message for message in messages}
    for row in data["items"]:
        row["in_stock"] = row.get("catalogue_org_id") == selected_catalogue and available_stock(products_by_id.get(row.get("product_id")))
        row["stock_error"] = (products_by_id.get(row.get("product_id")) or {}).get("stock_resolution_error")
        row["delivery_status"] = delivery.get(row.get("notification_message_id"), {}).get("status")
    return serialize_mongo({"items": data["items"], "total": (data["total"] or [{"count": 0}])[0]["count"], "page": page})


@router.patch("/{request_id}")
@limiter.limit("60/minute")
async def review(request: Request, request_id: str, body: Review, user: CurrentUser):
    return await review_request(user, request_id, body)


@router.post("/restock/preview")
@limiter.limit("10/minute")
async def preview(request: Request, body: Audience, user: CurrentUser):
    admin(user)
    return {"recipients": await restock_preview(user, body.request_ids)}


@router.post("/restock/template")
@limiter.limit("5/minute")
async def template(request: Request, body: Template, user: CurrentUser):
    admin(user)
    return await prepare_template(user, body.phone_number_id)


@router.post("/restock/send")
@limiter.limit("5/minute")
async def send(request: Request, body: SendAudience, user: CurrentUser):
    admin(user)
    from services.entitlement_service import feature_access
    access = await asyncio.to_thread(feature_access, user.org_id, "BROADCASTING")
    batch_limit = access["limits"].get("max_batch_recipients")
    if batch_limit is not None and len(set(body.request_ids)) > batch_limit:
        raise HTTPException(422, f"This broadcast exceeds the {batch_limit}-recipient batch limit")
    return await queue_restock(user, body.request_ids, body.revisions)


@router.post("/{request_id}/restock/retry")
@limiter.limit("5/minute")
async def retry(request: Request, request_id: str, user: CurrentUser):
    admin(user)
    return await retry_restock(user, request_id)
