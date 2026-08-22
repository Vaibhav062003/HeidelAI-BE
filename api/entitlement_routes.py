import json
import os

from fastapi import APIRouter, Header, HTTPException, Request

from auth.dependencies import CurrentUser
from services.entitlement_auth import verify_entitlement_request
from services.entitlement_service import (
    access_snapshot,
    apply_entitlement,
    consume_quota,
    record_ai_usage,
    release_quota,
)


router = APIRouter(tags=["Feature entitlements"])


@router.put("/entitlements/{account_id}")
async def put_entitlement(
    account_id: str,
    request: Request,
    x_request_id: str = Header(alias="X-Request-Id"),
    x_timestamp: str = Header(alias="X-Timestamp"),
    x_signature: str = Header(alias="X-Signature"),
    x_idempotency_key: str = Header(alias="X-Idempotency-Key"),
):
    secret = os.getenv("ENTITLEMENT_HMAC_SECRET", "")
    body = await request.body()
    verify_entitlement_request("PUT", request.url.path, x_timestamp, x_request_id, x_signature, body, secret)
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid entitlement JSON") from exc
    if payload.get("account_id") != account_id or not x_idempotency_key:
        raise HTTPException(status_code=422, detail="Entitlement account or idempotency key is invalid")
    return apply_entitlement(account_id, payload)


@router.post("/entitlements/usage")
async def post_entitlement_usage(
    request: Request,
    x_request_id: str = Header(alias="X-Request-Id"),
    x_timestamp: str = Header(alias="X-Timestamp"),
    x_signature: str = Header(alias="X-Signature"),
):
    body = await request.body()
    verify_entitlement_request("POST", request.url.path, x_timestamp, x_request_id, x_signature, body, os.getenv("ENTITLEMENT_HMAC_SECRET", ""))
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid entitlement JSON") from exc
    account_ids = payload.get("account_ids") if isinstance(payload, dict) else None
    months = payload.get("months", 12) if isinstance(payload, dict) else 12
    if not isinstance(account_ids, list) or len(account_ids) > 500:
        raise HTTPException(status_code=422, detail="account_ids must be a list containing at most 500 organizations")
    if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 24:
        raise HTTPException(status_code=422, detail="months must be between 1 and 24")
    normalized = []
    seen = set()
    for account_id in account_ids:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id.strip()) > 100:
            raise HTTPException(status_code=422, detail="Each entitlement account must be a valid organization ID")
        account_id = account_id.strip()
        if account_id not in seen:
            seen.add(account_id)
            normalized.append(account_id)
    return {"accounts": [{"account_id": account_id, **access_snapshot(account_id, months)} for account_id in normalized]}


@router.post("/entitlements/{account_id}/usage-events")
async def post_entitlement_usage_event(
    account_id: str,
    request: Request,
    x_request_id: str = Header(alias="X-Request-Id"),
    x_timestamp: str = Header(alias="X-Timestamp"),
    x_signature: str = Header(alias="X-Signature"),
):
    body = await request.body()
    verify_entitlement_request("POST", request.url.path, x_timestamp, x_request_id, x_signature, body, os.getenv("ENTITLEMENT_HMAC_SECRET", ""))
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid usage event JSON") from exc
    action = payload.get("action") if isinstance(payload, dict) else None
    event_key = payload.get("event_key") if isinstance(payload, dict) else None
    if action not in {"consume", "release", "record"} or not isinstance(event_key, str) or not event_key.strip() or len(event_key) > 200:
        raise HTTPException(status_code=422, detail="Usage event action or key is invalid")
    quantity = payload.get("quantity", 1)
    numbers = {name: payload.get(name, 0) for name in ("input_tokens", "output_tokens", "recorded_cost_microusd")}
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (quantity, *numbers.values())) or not 1 <= quantity <= 100:
        raise HTTPException(status_code=422, detail="Usage event values are invalid")
    event_key = event_key.strip()
    if action == "consume":
        return consume_quota(account_id, "AI", "monthly_messages", quantity, event_key)
    if action == "release":
        return {"released": release_quota(account_id, "monthly_messages", event_key)}
    try:
        return record_ai_usage(account_id, event_key, payload.get("channel"), quantity, **numbers)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/features/access")
async def get_feature_access(user: CurrentUser):
    return access_snapshot(user.org_id)
