import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


class ReplyOperationConflict(Exception):
    pass


def claim_reply(collection, org_id: str, temp_id: Optional[str], conversation_id: str, mode: str, contents: list[str], now: Optional[datetime] = None) -> Optional[dict]:
    if mode not in {"reply", "ai"} or not temp_id or len(contents) != 1:
        return None
    now = now or datetime.now(timezone.utc)
    operation_id = f"{org_id}:{temp_id}"
    fingerprint = hashlib.sha256(json.dumps({
        "conversation_id": conversation_id,
        "mode": mode,
        "contents": contents,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    claim_id = str(uuid.uuid4())
    operation = {
        "_id": operation_id,
        "org_id": org_id,
        "temp_id": temp_id,
        "fingerprint": fingerprint,
        "status": "claimed",
        "claim_id": claim_id,
        "claim_expires_at": now + timedelta(minutes=2),
        "created_at": now,
        "expires_at": now + timedelta(days=30),
    }
    existing = collection.find_one_and_update(
        {"_id": operation_id},
        {"$setOnInsert": operation},
        upsert=True,
        return_document=True,
    ) or operation
    if existing.get("fingerprint") != fingerprint:
        raise ReplyOperationConflict("This reply operation was already used for different content")
    if existing.get("claim_id") == claim_id:
        return {**existing, "action": "claimed"}
    if existing.get("status") == "completed" and isinstance(existing.get("result"), dict):
        return {**existing, "action": "replay"}
    if existing.get("status") == "provider_accepted" and existing.get("provider_message_id"):
        reconcile_id = str(uuid.uuid4())
        resumed = collection.find_one_and_update(
            {"_id": operation_id, "fingerprint": fingerprint, "status": "provider_accepted"},
            {"$set": {"status": "reconciling", "reconcile_id": reconcile_id, "reconcile_expires_at": now + timedelta(minutes=2)}},
            return_document=True,
        )
        if resumed:
            return {**resumed, "action": "resume"}
    if existing.get("status") == "reconciling":
        reconcile_id = str(uuid.uuid4())
        resumed = collection.find_one_and_update(
            {"_id": operation_id, "fingerprint": fingerprint, "status": "reconciling", "reconcile_id": existing.get("reconcile_id"), "reconcile_expires_at": {"$lte": now}},
            {"$set": {"reconcile_id": reconcile_id, "reconcile_expires_at": now + timedelta(minutes=2)}},
            return_document=True,
        )
        if resumed:
            return {**resumed, "action": "resume"}
    if existing.get("status") == "claimed":
        recovered = collection.find_one_and_update(
            {"_id": operation_id, "fingerprint": fingerprint, "status": "claimed", "claim_id": existing.get("claim_id"), "claim_expires_at": {"$lte": now}},
            {"$set": {"claim_id": claim_id, "claim_expires_at": now + timedelta(minutes=2)}},
            return_document=True,
        )
        if recovered:
            return {**recovered, "action": "claimed"}
    raise ReplyOperationConflict("This reply is still being confirmed. Refresh the conversation before retrying.")


def start_provider(collection, operation: Optional[dict], now: Optional[datetime] = None) -> bool:
    if not operation:
        return True
    updated = collection.find_one_and_update(
        {"_id": operation["_id"], "fingerprint": operation["fingerprint"], "status": "claimed", "claim_id": operation["claim_id"]},
        {"$set": {"status": "provider_pending", "provider_started_at": now or datetime.now(timezone.utc)}},
        return_document=True,
    )
    return bool(updated)


def release_claim(collection, operation: Optional[dict], provider_started: bool = False) -> None:
    if not operation:
        return
    collection.delete_one({
        "_id": operation["_id"],
        "fingerprint": operation["fingerprint"],
        "claim_id": operation["claim_id"],
        "status": "provider_pending" if provider_started else "claimed",
    })


def mark_unknown(collection, operation: Optional[dict], now: Optional[datetime] = None) -> None:
    if operation:
        collection.update_one(
            {"_id": operation["_id"], "fingerprint": operation["fingerprint"], "claim_id": operation["claim_id"], "status": "provider_pending"},
            {"$set": {"status": "delivery_unknown", "unknown_at": now or datetime.now(timezone.utc)}},
        )


def record_acceptance(collection, operation: Optional[dict], message_id: str, response_data: dict, now: Optional[datetime] = None) -> Optional[dict]:
    if not operation:
        return None
    now = now or datetime.now(timezone.utc)
    reconcile_id = str(uuid.uuid4())
    return collection.find_one_and_update(
        {"_id": operation["_id"], "fingerprint": operation["fingerprint"], "claim_id": operation["claim_id"], "status": "provider_pending"},
        {"$set": {"status": "reconciling", "provider_message_id": message_id, "response_data": response_data, "accepted_at": now, "reconcile_id": reconcile_id, "reconcile_expires_at": now + timedelta(minutes=2)}},
        return_document=True,
    )


def complete_reply(collection, operation: Optional[dict], result: dict, now: Optional[datetime] = None) -> bool:
    if not operation:
        return True
    completed = collection.find_one_and_update(
        {"_id": operation["_id"], "fingerprint": operation["fingerprint"], "status": "reconciling", "reconcile_id": operation.get("reconcile_id")},
        {"$set": {"status": "completed", "result": result, "completed_at": now or datetime.now(timezone.utc)}},
        return_document=True,
    )
    return bool(completed)
