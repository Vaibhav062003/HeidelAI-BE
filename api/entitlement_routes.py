import json
import os

from fastapi import APIRouter, Header, HTTPException, Request

from auth.dependencies import CurrentUser
from services.entitlement_auth import verify_entitlement_request
from services.entitlement_service import access_snapshot, apply_entitlement


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
    if not isinstance(account_ids, list) or len(account_ids) > 500:
        raise HTTPException(status_code=422, detail="account_ids must be a list containing at most 500 organizations")
    normalized = []
    seen = set()
    for account_id in account_ids:
        if not isinstance(account_id, str) or not account_id.strip() or len(account_id.strip()) > 100:
            raise HTTPException(status_code=422, detail="Each entitlement account must be a valid organization ID")
        account_id = account_id.strip()
        if account_id not in seen:
            seen.add(account_id)
            normalized.append(account_id)
    return {"accounts": [{"account_id": account_id, **access_snapshot(account_id)} for account_id in normalized]}


@router.get("/api/features/access")
async def get_feature_access(user: CurrentUser):
    return access_snapshot(user.org_id)
