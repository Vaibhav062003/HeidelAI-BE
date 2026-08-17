from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_async_mongo_db
from services.sales_offer_policy import is_admin, report_query
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


router = APIRouter(prefix="/api/offers", tags=["Sales offers"])
db = get_async_mongo_db()


class OfferOutcomePayload(BaseModel):
    status: Literal["open", "accepted", "billed", "paid", "lost", "expired"]


def _collection(org_id: str):
    return db[f"sales_offers_{org_id}"]


@router.get("/report")
@limiter.limit("30/minute")
async def offer_report(
    request: Request,
    user: CurrentUser,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    source: Optional[str] = Query(None, pattern="^(formal_quote|chat_price)$"),
    status: Optional[str] = Query(None, pattern="^(open|accepted|billed|paid|lost|expired|superseded)$"),
    agent_id: Optional[str] = None,
    limit: int = Query(200, ge=1, le=500),
):
    query = report_query(
        org_id=user.org_id,
        user_id=user.user_id,
        role=user.role,
        start=start,
        end=end,
        source=source,
        status=status,
        agent_id=agent_id,
    )
    result = await _collection(user.org_id).aggregate([
        {"$match": query},
        {"$facet": {
            "totals": [{"$group": {
                "_id": None,
                "sent_count": {"$sum": 1},
                "formal_count": {"$sum": {"$cond": [{"$eq": ["$source_type", "formal_quote"]}, 1, 0]}},
                "chat_count": {"$sum": {"$cond": [{"$eq": ["$source_type", "chat_price"]}, 1, 0]}},
                "offered_value": {"$sum": {"$cond": [
                    {"$in": ["$status", ["open", "accepted"]]},
                    {"$ifNull": ["$total_amount", 0]},
                    0,
                ]}},
            }}],
            "outcomes": [{"$group": {"_id": "$status", "count": {"$sum": 1}}}],
            "agents": [
                {"$group": {
                    "_id": "$agent_id",
                    "agent_name": {"$last": "$agent_name"},
                    "sent_count": {"$sum": 1},
                    "formal_count": {"$sum": {"$cond": [{"$eq": ["$source_type", "formal_quote"]}, 1, 0]}},
                    "chat_count": {"$sum": {"$cond": [{"$eq": ["$source_type", "chat_price"]}, 1, 0]}},
                    "offered_value": {"$sum": {"$cond": [
                        {"$in": ["$status", ["open", "accepted"]]},
                        {"$ifNull": ["$total_amount", 0]},
                        0,
                    ]}},
                    "won_count": {"$sum": {"$cond": [{"$in": ["$status", ["accepted", "billed", "paid"]]}, 1, 0]}},
                    "lost_count": {"$sum": {"$cond": [{"$in": ["$status", ["lost", "expired"]]}, 1, 0]}},
                }},
                {"$sort": {"offered_value": -1, "sent_count": -1}},
                {"$project": {
                    "_id": 0,
                    "agent_id": "$_id",
                    "agent_name": 1,
                    "sent_count": 1,
                    "formal_count": 1,
                    "chat_count": 1,
                    "offered_value": 1,
                    "won_count": 1,
                    "lost_count": 1,
                }},
            ],
            "items": [
                {"$sort": {"created_at": -1}},
                {"$limit": limit},
                {"$project": {"org_id": 0, "outcome_history": 0}},
            ],
        }},
    ]).to_list(length=1)
    payload = result[0] if result else {"totals": [], "outcomes": [], "agents": [], "items": []}
    totals = payload["totals"][0] if payload["totals"] else {
        "sent_count": 0,
        "formal_count": 0,
        "chat_count": 0,
        "offered_value": 0,
    }
    totals.pop("_id", None)
    outcomes = {item["_id"]: item["count"] for item in payload["outcomes"] if item.get("_id")}
    decided = sum(outcomes.get(key, 0) for key in ("accepted", "billed", "paid", "lost", "expired"))
    won = sum(outcomes.get(key, 0) for key in ("accepted", "billed", "paid"))
    return serialize_mongo({
        "scope": "organization" if is_admin(user.role) else "agent",
        "summary": {
            **totals,
            "outcomes": outcomes,
            "conversion_rate": round(won * 100 / decided, 1) if decided else 0,
        },
        "agents": payload["agents"] if is_admin(user.role) else [],
        "offers": payload["items"],
    })


@router.get("/conversations/{conversation_id}")
@limiter.limit("60/minute")
async def conversation_offers(
    request: Request,
    conversation_id: str,
    user: CurrentUser,
    source: Optional[str] = Query(None, pattern="^(formal_quote|chat_price)$"),
    limit: int = Query(50, ge=1, le=100),
):
    await require_conversation_access(user.org_id, conversation_id, user.user_id, user.role)
    query = {
        "org_id": user.org_id,
        "conversation_id": conversation_id,
        "delivery_status": {"$ne": "failed"},
    }
    if source:
        query["source_type"] = source
    offers = await _collection(user.org_id).find(
        query,
        {"org_id": 0, "outcome_history": 0},
    ).sort("created_at", -1).to_list(length=limit)
    return {"offers": serialize_mongo(offers)}


@router.patch("/{offer_id}")
@limiter.limit("30/minute")
async def update_offer_outcome(
    request: Request,
    offer_id: str,
    payload: OfferOutcomePayload,
    user: CurrentUser,
):
    query: dict = {"_id": offer_id, "org_id": user.org_id}
    if not is_admin(user.role):
        query["agent_id"] = user.user_id
    current = await _collection(user.org_id).find_one(query)
    if not current:
        raise HTTPException(status_code=404, detail="Offer not found")
    now = datetime.now(timezone.utc)
    await _collection(user.org_id).update_one(query, {
        "$set": {"status": payload.status, "updated_at": now},
        "$push": {"outcome_history": {
            "from": current.get("status", "open"),
            "to": payload.status,
            "changed_at": now,
            "changed_by": user.user_id,
            "changed_by_name": user.full_name or "Team member",
        }},
    })
    updated = await _collection(user.org_id).find_one(query, {"org_id": 0, "outcome_history": 0})
    return {"offer": serialize_mongo(updated)}
