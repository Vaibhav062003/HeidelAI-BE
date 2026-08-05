import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.push_notification_service import (
    PushConfigurationError,
    PushMembershipRevokedError,
    deactivate_installation,
    register_device,
    resolve_event,
    process_member_device_revocation,
    queue_member_device_revocation,
    restore_member_push_access,
)
from config.settings import CLERK_WEBHOOK_SIGNING_SECRET


router = APIRouter(prefix="/api/push", tags=["push"])


def _verify_clerk_webhook(body: bytes, request: Request) -> str:
    if not CLERK_WEBHOOK_SIGNING_SECRET:
        raise HTTPException(status_code=503, detail="Clerk webhook is not configured")
    webhook_id = request.headers.get("svix-id")
    timestamp = request.headers.get("svix-timestamp")
    signatures = request.headers.get("svix-signature", "")
    if not webhook_id or not timestamp or not signatures:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        timestamp_number = int(timestamp)
        if abs(time.time() - timestamp_number) > 300:
            raise ValueError("expired")
        secret = CLERK_WEBHOOK_SIGNING_SECRET.removeprefix("whsec_")
        signing_key = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook signature") from exc
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(signing_key, signed, hashlib.sha256).digest()).decode()
    valid = any(
        version == "v1" and hmac.compare_digest(signature, expected)
        for item in signatures.split()
        for version, separator, signature in [item.partition(",")]
        if separator
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    return webhook_id


class DeviceRegistration(BaseModel):
    installation_id: str
    token: str = Field(min_length=20, max_length=4096)
    platform: Literal["android"]
    app_version: str | None = Field(default=None, max_length=40)

    @field_validator("installation_id")
    @classmethod
    def valid_installation_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as exc:
            raise ValueError("installation_id must be a UUID") from exc


@router.post("/devices")
@limiter.limit("12/minute")
def add_device(request: Request, registration: DeviceRegistration, user: CurrentUser):
    try:
        register_device(
            org_id=user.org_id,
            user_id=user.user_id,
            installation_id=registration.installation_id,
            token=registration.token,
            app_version=registration.app_version,
        )
    except PushConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except PushMembershipRevokedError as exc:
        raise HTTPException(status_code=403, detail="Organization membership is no longer active") from exc
    return {"status": "success"}


@router.delete("/devices/{installation_id}")
@limiter.limit("12/minute")
def remove_device(request: Request, installation_id: str, user: CurrentUser):
    try:
        normalized_id = str(uuid.UUID(installation_id))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid installation ID") from exc
    deactivate_installation(org_id=user.org_id, user_id=user.user_id, installation_id=normalized_id)
    return {"status": "success"}


@router.get("/events/{event_id}")
@limiter.limit("30/minute")
def get_event(request: Request, event_id: str, user: CurrentUser):
    if len(event_id) != 32 or any(char not in "0123456789abcdef" for char in event_id):
        raise HTTPException(status_code=404, detail="Notification not found")
    event = resolve_event(org_id=user.org_id, event_id=event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Notification not found")
    return event


@router.post("/clerk-membership-webhook")
@limiter.limit("120/minute")
async def clerk_membership_webhook(request: Request):
    body = await request.body()
    if len(body) > 256_000:
        raise HTTPException(status_code=413, detail="Webhook is too large")
    webhook_id = _verify_clerk_webhook(body, request)
    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook body") from exc
    event_type = event.get("type")
    deleted_events = {"organizationMembership.deleted", "organization_membership.deleted"}
    created_events = {"organizationMembership.created", "organization_membership.created"}
    if event_type not in deleted_events | created_events:
        return {"status": "ignored"}
    event_at = event.get("timestamp")
    if not isinstance(event_at, int) or isinstance(event_at, bool) or event_at <= 0:
        raise HTTPException(status_code=422, detail="Webhook timestamp is missing")

    data = event.get("data") or {}
    organization = data.get("organization") or {}
    public_user = data.get("public_user_data") or data.get("publicUserData") or {}
    org_id = organization.get("id") or data.get("organization_id")
    user_id = public_user.get("user_id") or public_user.get("userId")
    if not isinstance(org_id, str) or not org_id.startswith("org_"):
        raise HTTPException(status_code=422, detail="Membership organization is missing")
    if not isinstance(user_id, str) or not user_id.startswith("user_"):
        raise HTTPException(status_code=422, detail="Membership user is missing")

    if event_type in created_events:
        restore_member_push_access(org_id=org_id, user_id=user_id, event_at=event_at)
        return {"status": "success"}

    revocation_id = queue_member_device_revocation(
        org_id=org_id,
        user_id=user_id,
        revocation_id=f"clerk_{webhook_id}",
        event_at=event_at,
    )
    process_member_device_revocation(revocation_id)
    return {"status": "success"}
