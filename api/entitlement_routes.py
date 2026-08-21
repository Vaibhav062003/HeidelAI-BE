import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Header, HTTPException, Request

from auth.dependencies import CurrentUser
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
    if not secret:
        raise HTTPException(status_code=503, detail={"code": "entitlement_not_configured", "message": "Entitlement synchronization is not configured."})
    try:
        timestamp = int(x_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid entitlement timestamp") from exc
    if abs(int(time.time() * 1000) - timestamp) > 5 * 60 * 1000:
        raise HTTPException(status_code=401, detail="Expired entitlement request")
    body = await request.body()
    canonical = f"PUT\n{request.url.path}\n{x_timestamp}\n{x_request_id}\n".encode() + body
    expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=401, detail="Invalid entitlement signature")
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid entitlement JSON") from exc
    if payload.get("account_id") != account_id or not x_idempotency_key:
        raise HTTPException(status_code=422, detail="Entitlement account or idempotency key is invalid")
    return apply_entitlement(account_id, payload)


@router.get("/api/features/access")
async def get_feature_access(user: CurrentUser):
    return access_snapshot(user.org_id)
