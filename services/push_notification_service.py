"""Secure, organization-scoped Android push notification delivery."""

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from cryptography.fernet import Fernet, InvalidToken
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.settings import (
    FIREBASE_PROJECT_ID,
    FIREBASE_SERVICE_ACCOUNT_B64,
    PUSH_TOKEN_ENCRYPTION_KEY,
)
from database import get_mongo_db


db = get_mongo_db()
devices = db.push_devices
events = db.push_events
revocations = db.push_device_revocations
revoked_members = db.push_revoked_push_members
_credentials = None
_credential_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()

DEVICE_TTL_DAYS = 30
EVENT_TTL_HOURS = 24


class PushConfigurationError(RuntimeError):
    pass


class PushMembershipRevokedError(PermissionError):
    pass


def ensure_push_notification_indexes() -> None:
    # Safe migration for any pre-release rows: retain the most recently updated
    # registration and let active apps refresh it on their next resume.
    for field in ("installation_id", "token_hash"):
        duplicates = devices.aggregate([
            {"$match": {field: {"$type": "string"}}},
            {"$sort": {"updated_at": -1}},
            {"$group": {"_id": f"${field}", "ids": {"$push": "$_id"}, "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ])
        for duplicate in duplicates:
            devices.delete_many({"_id": {"$in": duplicate["ids"][1:]}})
    devices.delete_many({"$or": [
        {"installation_id": {"$exists": False}}, {"installation_id": None},
        {"token_hash": {"$exists": False}}, {"token_hash": None},
    ]})
    devices.create_index("token_hash", unique=True)
    devices.create_index("installation_id", unique=True)
    devices.create_index([("org_id", 1), ("active", 1), ("expires_at", 1)])
    devices.create_index("expires_at", expireAfterSeconds=0)
    events.create_index("event_id", unique=True)
    events.create_index([("org_id", 1), ("platform", 1), ("message_id", 1)], unique=True)
    events.create_index([("status", 1), ("next_attempt_at", 1)])
    events.create_index("expires_at", expireAfterSeconds=0)
    revocations.create_index("revocation_id", unique=True)
    revocations.create_index([("status", 1), ("created_at", 1)])
    revocations.create_index("expires_at", expireAfterSeconds=0)
    revoked_members.create_index([("org_id", 1), ("user_id", 1)], unique=True)


def _fernet() -> Fernet:
    if not PUSH_TOKEN_ENCRYPTION_KEY:
        raise PushConfigurationError("PUSH_TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return Fernet(PUSH_TOKEN_ENCRYPTION_KEY.encode())
    except (TypeError, ValueError) as exc:
        raise PushConfigurationError("PUSH_TOKEN_ENCRYPTION_KEY is invalid") from exc


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register_device(*, org_id: str, user_id: str, installation_id: str, token: str, app_version: str | None) -> None:
    now = datetime.now(timezone.utc)
    member_filter = {"org_id": org_id, "user_id": user_id}
    membership = revoked_members.find_one(member_filter, {"_id": 0, "revoked": 1, "event_at": 1}) or {}
    if membership.get("revoked"):
        raise PushMembershipRevokedError("Organization membership was revoked")
    membership_event_at = membership.get("event_at", 0)
    token_hash = _token_hash(token)
    encrypted_token = _fernet().encrypt(token.encode()).decode()

    update = {
        "$set": {
            "org_id": org_id,
            "user_id": user_id,
            "installation_id": installation_id,
            "encrypted_token": encrypted_token,
            "token_hash": token_hash,
            "platform": "android",
            "app_version": app_version,
            "membership_event_at": membership_event_at,
            "active": True,
            "updated_at": now,
            "expires_at": now + timedelta(days=DEVICE_TTL_DAYS),
        },
        "$setOnInsert": {"created_at": now},
    }
    try:
        devices.update_one({"installation_id": installation_id}, update, upsert=True)
    except DuplicateKeyError:
        # Preferences may be reset while FCM retains the same token. Move that token
        # to the newly authenticated installation instead of leaving two active rows.
        devices.delete_one({"token_hash": token_hash, "installation_id": {"$ne": installation_id}})
        devices.update_one(
            {"installation_id": installation_id},
            {
                "$set": update["$set"],
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    # Close the registration/revocation race: if the tombstone appeared while
    # writing the device, immediately undo this registration.
    membership = revoked_members.find_one(member_filter, {"_id": 0, "revoked": 1, "event_at": 1}) or {}
    if membership.get("revoked"):
        deactivate_installation(org_id=org_id, user_id=user_id, installation_id=installation_id)
        raise PushMembershipRevokedError("Organization membership was revoked")
    if membership.get("event_at", 0) > membership_event_at:
        devices.update_one(
            {"installation_id": installation_id, "org_id": org_id, "user_id": user_id},
            {"$set": {"membership_event_at": membership["event_at"]}},
        )


def deactivate_installation(*, org_id: str, user_id: str, installation_id: str) -> None:
    now = datetime.now(timezone.utc)
    devices.update_many(
        {"org_id": org_id, "user_id": user_id, "installation_id": installation_id},
        {"$set": {"active": False, "updated_at": now, "expires_at": now}},
    )


def deactivate_member_devices(*, org_id: str, user_id: str, event_at: int) -> None:
    now = datetime.now(timezone.utc)
    devices.update_many(
        {
            "org_id": org_id,
            "user_id": user_id,
            "$or": [
                {"membership_event_at": {"$lte": event_at}},
                {"membership_event_at": {"$exists": False}},
            ],
        },
        {"$set": {"active": False, "updated_at": now, "expires_at": now}},
    )


def _set_member_revocation_state(
    *, org_id: str, user_id: str, revoked: bool, event_at: int, revocation_id: str | None
) -> bool:
    try:
        result = revoked_members.update_one(
            {
                "org_id": org_id,
                "user_id": user_id,
                "$or": [{"event_at": {"$lt": event_at}}, {"event_at": {"$exists": False}}],
            },
            {"$set": {"revoked": revoked, "event_at": event_at, "revocation_id": revocation_id}},
            upsert=True,
        )
        return bool(result.matched_count or result.upserted_id)
    except DuplicateKeyError:
        current = revoked_members.find_one(
            {"org_id": org_id, "user_id": user_id},
            {"_id": 0, "revoked": 1, "event_at": 1, "revocation_id": 1},
        ) or {}
        # A retry of the same event may still need to create/cancel its durable job.
        return (
            current.get("revoked") == revoked
            and current.get("event_at") == event_at
            and current.get("revocation_id") == revocation_id
        )


def queue_member_device_revocation(
    *, org_id: str, user_id: str, revocation_id: str, event_at: int | None = None
) -> str:
    now = datetime.now(timezone.utc)
    event_at = event_at if event_at is not None else int(now.timestamp() * 1000)
    applied = _set_member_revocation_state(
        org_id=org_id,
        user_id=user_id,
        revoked=True,
        event_at=event_at,
        revocation_id=revocation_id,
    )
    if not applied:
        return revocation_id
    revocations.update_one(
        {"revocation_id": revocation_id},
        {
            "$setOnInsert": {
                "revocation_id": revocation_id,
                "org_id": org_id,
                "user_id": user_id,
                "event_at": event_at,
                "status": "pending",
                "created_at": now,
                "expires_at": now + timedelta(days=30),
            }
        },
        upsert=True,
    )
    return revocation_id


def restore_member_push_access(*, org_id: str, user_id: str, event_at: int) -> None:
    if not _set_member_revocation_state(
        org_id=org_id,
        user_id=user_id,
        revoked=False,
        event_at=event_at,
        revocation_id=None,
    ):
        return
    revocations.update_many(
        {"org_id": org_id, "user_id": user_id, "status": "pending", "event_at": {"$lt": event_at}},
        {"$set": {"status": "obsolete", "completed_at": datetime.now(timezone.utc)}},
    )


def process_member_device_revocation(revocation_id: str) -> None:
    revocation = revocations.find_one({"revocation_id": revocation_id, "status": "pending"})
    if not revocation:
        return
    current = revoked_members.find_one(
        {
            "org_id": revocation["org_id"],
            "user_id": revocation["user_id"],
            "revoked": True,
            "revocation_id": revocation_id,
            "event_at": revocation["event_at"],
        },
        {"_id": 1},
    )
    if not current:
        revocations.update_one(
            {"revocation_id": revocation_id, "status": "pending"},
            {"$set": {"status": "obsolete", "completed_at": datetime.now(timezone.utc)}},
        )
        return
    deactivate_member_devices(
        org_id=revocation["org_id"],
        user_id=revocation["user_id"],
        event_at=revocation["event_at"],
    )
    revocations.update_one(
        {"revocation_id": revocation_id, "status": "pending"},
        {"$set": {"status": "done", "completed_at": datetime.now(timezone.utc)}},
    )


async def process_pending_member_device_revocations(limit: int = 50) -> None:
    pending = revocations.find({"status": "pending"}, {"revocation_id": 1}).sort("created_at", 1).limit(limit)
    for revocation in pending:
        try:
            process_member_device_revocation(revocation["revocation_id"])
        except Exception as exc:
            logger.warning(f"Push device revocation deferred: {type(exc).__name__}")


def resolve_event(*, org_id: str, event_id: str) -> dict | None:
    return events.find_one(
        {
            "org_id": org_id,
            "event_id": event_id,
            "expires_at": {"$gt": datetime.now(timezone.utc)},
        },
        {"_id": 0, "conversation_id": 1, "platform": 1},
    )


def enqueue_customer_message_push(*, org_id: str, platform: str, message_id: str, conversation_id: str) -> str:
    now = datetime.now(timezone.utc)
    event_id = uuid.uuid4().hex
    try:
        event = events.find_one_and_update(
            {"org_id": org_id, "platform": platform, "message_id": message_id},
            {
                "$setOnInsert": {
                    "event_id": event_id,
                    "org_id": org_id,
                    "platform": platform,
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "status": "pending",
                    "attempts": 0,
                    "completed_token_hashes": [],
                    "created_at": now,
                    "next_attempt_at": now,
                    "expires_at": now + timedelta(hours=EVENT_TTL_HOURS),
                }
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        event = events.find_one({"org_id": org_id, "platform": platform, "message_id": message_id})

    if not event:
        raise RuntimeError("Could not enqueue push notification")
    return event["event_id"]


def schedule_customer_message_push(*, org_id: str, platform: str, message_id: str, conversation_id: str) -> None:
    try:
        event_id = enqueue_customer_message_push(
            org_id=org_id,
            platform=platform,
            message_id=message_id,
            conversation_id=conversation_id,
        )
        task = asyncio.create_task(process_push_event(event_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except Exception as exc:
        # Push must never make the Meta webhook fail; the stored message remains authoritative.
        logger.warning(f"Could not enqueue Android push notification: {type(exc).__name__}")


def _service_account_info() -> dict:
    if not FIREBASE_SERVICE_ACCOUNT_B64 or not FIREBASE_PROJECT_ID:
        raise PushConfigurationError("Firebase push credentials are not configured")
    try:
        encoded = "".join(FIREBASE_SERVICE_ACCOUNT_B64.split())
        if len(encoded) > 200_000:
            raise ValueError("credential is too large")
        info = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PushConfigurationError("FIREBASE_SERVICE_ACCOUNT_B64 is invalid") from exc
    if info.get("project_id") != FIREBASE_PROJECT_ID:
        raise PushConfigurationError("Firebase project does not match FIREBASE_PROJECT_ID")
    return info


async def _access_token() -> str:
    global _credentials
    async with _credential_lock:
        if _credentials is None:
            _credentials = service_account.Credentials.from_service_account_info(
                _service_account_info(),
                scopes=["https://www.googleapis.com/auth/firebase.messaging"],
            )
        if not _credentials.valid:
            await asyncio.to_thread(_credentials.refresh, GoogleAuthRequest())
        if not _credentials.token:
            raise PushConfigurationError("Firebase access token is unavailable")
        return _credentials.token


def build_fcm_message(token: str, event_id: str) -> dict:
    return {
        "message": {
            "token": token,
            "notification": {
                "title": "New customer message",
                "body": "Open HeidelAI to view it",
            },
            "data": {"kind": "customer_message", "event_id": event_id},
            "android": {
                "priority": "high",
                "notification": {
                    "channel_id": "sales_messages_v2",
                    "sound": "default",
                    "visibility": "PRIVATE",
                    "tag": event_id,
                },
            },
        }
    }


def _fcm_error_code(response: httpx.Response) -> str | None:
    try:
        details = response.json().get("error", {}).get("details", [])
    except (ValueError, AttributeError):
        return None
    for detail in details:
        if isinstance(detail, dict) and detail.get("errorCode"):
            return detail["errorCode"]
    return None


def classify_fcm_failure(status_code: int, error_code: str | None) -> Literal["permanent", "retry"]:
    if error_code in {"UNREGISTERED", "SENDER_ID_MISMATCH"}:
        return "permanent"
    return "retry"


async def _send(token: str, event_id: str) -> Literal["sent", "permanent", "retry"]:
    global _credentials
    url = f"https://fcm.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/messages:send"
    response = None
    for auth_attempt in range(2):
        access_token = await _access_token()
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=build_fcm_message(token, event_id),
                )
        except httpx.RequestError:
            return "retry"
        if response.status_code != 401 or auth_attempt == 1:
            break
        _credentials = None

    if response is None:
        return "retry"
    if response.is_success:
        return "sent"
    error_code = _fcm_error_code(response)
    if response.status_code == 401:
        _credentials = None
        return "retry"
    # Configuration and permission failures must not invalidate otherwise valid device tokens.
    return classify_fcm_failure(response.status_code, error_code)


async def process_push_event(event_id: str) -> None:
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4().hex
    event = events.find_one_and_update(
        {
            "event_id": event_id,
            "expires_at": {"$gt": now},
            "$or": [
                {"status": "pending", "next_attempt_at": {"$lte": now}},
                {"status": "sending", "claimed_at": {"$lt": now - timedelta(minutes=5)}},
            ],
        },
        {"$set": {"status": "sending", "claimed_at": now, "claim_id": claim_id}, "$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if not event:
        return

    completed = set(event.get("completed_token_hashes", []))
    active_devices = list(
        devices.find(
            {"org_id": event["org_id"], "active": True, "expires_at": {"$gt": now}},
            {"encrypted_token": 1, "token_hash": 1},
        )
    )
    retry_needed = False

    for device in active_devices:
        token_hash = device["token_hash"]
        if token_hash in completed:
            continue
        try:
            token = _fernet().decrypt(device["encrypted_token"].encode()).decode()
            result = await _send(token, event_id)
        except (InvalidToken, UnicodeDecodeError):
            result = "permanent"
        except Exception as exc:
            logger.warning(f"Android push delivery deferred: {type(exc).__name__}")
            result = "retry"

        if result == "retry":
            retry_needed = True
            progress = events.update_one(
                {"event_id": event_id, "claim_id": claim_id},
                {"$set": {"claimed_at": datetime.now(timezone.utc)}},
            )
        else:
            progress = events.update_one(
                {"event_id": event_id, "claim_id": claim_id},
                {
                    "$addToSet": {"completed_token_hashes": token_hash},
                    "$set": {"claimed_at": datetime.now(timezone.utc)},
                },
            )
            if result == "permanent":
                devices.update_one(
                    {"token_hash": token_hash},
                    {"$set": {"active": False, "updated_at": datetime.now(timezone.utc)}},
                )
        if progress.matched_count == 0:
            return

    update: dict = {"$set": {"updated_at": datetime.now(timezone.utc)}}
    if retry_needed:
        delay_minutes = min(30, 2 ** min(event.get("attempts", 1), 4))
        update["$set"].update({
            "status": "pending",
            "next_attempt_at": datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
        })
    else:
        update["$set"].update({"status": "sent", "sent_at": datetime.now(timezone.utc)})
    events.update_one({"event_id": event_id, "claim_id": claim_id}, update)


async def process_pending_push_notifications(limit: int = 25) -> None:
    now = datetime.now(timezone.utc)
    pending = events.find(
        {
            "expires_at": {"$gt": now},
            "$or": [
                {"status": "pending", "next_attempt_at": {"$lte": now}},
                {"status": "sending", "claimed_at": {"$lt": now - timedelta(minutes=5)}},
            ],
        },
        {"event_id": 1},
    ).sort("next_attempt_at", 1).limit(limit)
    for event in pending:
        await process_push_event(event["event_id"])
