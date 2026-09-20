# Built-in modules
import asyncio
import httpx
from typing import Optional
import requests
import io
import hashlib
import re
import uuid
import os
import tempfile
import cloudinary
import cloudinary.api
import cloudinary.uploader
import cloudinary.utils
from datetime import datetime, timedelta, timezone
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

# Internal modules
from config.settings import (
    CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_API_KEY,
    CLOUDINARY_API_SECRET,
    WA_VERSION,
)
from loguru import logger
from services.whatsapp_call_history import recording_quota_evictions, recording_retention_deadline


# Cloudinary configuration
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

AUDIO_RETENTION_DAYS = 30
AUDIO_RETENTION_TAG = "heidelai-audio-note"
TALLY_PDF_RETENTION_DAYS = 30
TALLY_PDF_RETENTION_TAG = "heidelai-tally-invoice"
NATIVE_INVOICE_TAG = "heidelai-native-invoice"
CALL_RECORDING_RETENTION_DAYS = 15
CALL_RECORDING_TAG = "heidelai-whatsapp-call"
CALL_RECORDING_MAX_BYTES = 32 * 1024 * 1024
CALL_RECORDING_MAX_PART_BYTES = 2 * 1024 * 1024
CALL_RECORDING_MAX_PARTS = 31
CALL_RECORDING_MAX_DURATION_SECONDS = 30 * 60
CALL_RECORDING_ORG_QUOTA_BYTES = 1024 * 1024 * 1024
CALL_RECORDING_MIME_TYPES = {"audio/mp4", "audio/ogg", "audio/webm"}


def _outbound_upload_options(semantic_type: str, convert_audio: bool, media_kind: Optional[str] = None) -> dict:
    options = {}
    if convert_audio:
        options["eager"] = [{"format": "ogg", "audio_codec": "opus"}] if media_kind == "voice_note" else [{"format": "mp3"}]
    if semantic_type == "audio" and media_kind == "voice_note":
        options["tags"] = [AUDIO_RETENTION_TAG]
    return options


def _audio_retention_expression(days_threshold: int) -> str:
    return f"tags={AUDIO_RETENTION_TAG} AND created_at<={days_threshold}d"


def _cleanup_expired_audio_notes_sync(days_threshold: int) -> int:
    resources_by_type = {}
    next_cursor = None

    while True:
        search = cloudinary.Search().expression(
            _audio_retention_expression(days_threshold)
        ).max_results(500)
        if next_cursor:
            search = search.next_cursor(next_cursor)
        result = search.execute()

        for resource in result.get("resources", []):
            public_id = resource.get("public_id")
            if public_id:
                resources_by_type.setdefault(resource.get("resource_type", "video"), []).append(public_id)

        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break

    total_deleted = 0
    for resource_type, public_ids in resources_by_type.items():
        for start in range(0, len(public_ids), 100):
            batch = public_ids[start:start + 100]
            response = cloudinary.api.delete_resources(batch, resource_type=resource_type)
            total_deleted += sum(status == "deleted" for status in response.get("deleted", {}).values())
    return total_deleted


async def _download_whatsapp_media(
    client,
    media_url: Optional[str],
    media_id: str,
    mime_type: str,
    headers: dict,
    phone_number_id: Optional[str] = None,
) -> tuple[bytes, str]:
    if not media_url:
        media_response = await client.get(
            f"https://graph.facebook.com/{WA_VERSION or 'v24.0'}/{media_id}",
            headers=headers,
            params={"phone_number_id": phone_number_id} if phone_number_id else None,
        )
        if media_response.status_code != 200:
            raise RuntimeError("WhatsApp media URL could not be retrieved")
        media_metadata = media_response.json()
        media_url = media_metadata.get("url")
        mime_type = media_metadata.get("mime_type") or mime_type
        if not media_url:
            raise RuntimeError("WhatsApp media URL was missing")

    response = await client.get(media_url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError("WhatsApp media could not be downloaded")
    return response.content, mime_type


async def cleanup_expired_audio_notes(days_threshold: int = AUDIO_RETENTION_DAYS) -> int:
    """Delete tagged Cloudinary audio assets after the retention period."""
    try:
        deleted = await asyncio.to_thread(_cleanup_expired_audio_notes_sync, days_threshold)
        if deleted:
            logger.info(f"Deleted {deleted} expired audio notes from Cloudinary")
        return deleted
    except Exception as exc:
        logger.error(f"Failed to delete expired audio notes: {exc}")
        return 0


async def upload_whatsapp_call_recording(file, org_id: str, call_id: str, expires_at: datetime, consent: dict) -> dict:
    from database import get_mongo_db

    db = get_mongo_db()
    if db.whatsapp_call_recordings.find_one({"org_id": org_id, "call_id": call_id}, {"_id": 1}):
        raise RuntimeError("This call already has a recording session")
    safe_call_id = re.sub(r"[^A-Za-z0-9_-]", "-", call_id)[:160]
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "-", file.filename or f"{safe_call_id}.webm")[:220]
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        file.file,
        folder=f"whatsapp_call_recordings/{org_id}",
        public_id=safe_call_id,
        resource_type="video",
        type="private",
        tags=[CALL_RECORDING_TAG],
        context={"expires_at": expires_at.isoformat(), "call_id": call_id},
        overwrite=True,
    )
    if not result.get("public_id"):
        raise RuntimeError("Cloudinary did not return a call recording identifier")
    uploaded = {
        "recording": {
            "status": "ready",
            "filename": safe_filename,
            "mime_type": (file.content_type or "audio/webm").lower(),
            "size_bytes": result.get("bytes") or file.size,
            "recorded_at": datetime.now(timezone.utc),
            "expires_at": expires_at,
            "consent_recorded_at": consent["attested_at"],
        },
        "storage": {
            "public_id": result["public_id"],
            "asset_id": result.get("asset_id"),
            "resource_type": result.get("resource_type") or "video",
            "delivery_type": result.get("type") or "private",
            "format": result.get("format"),
        },
    }
    session = {
        "org_id": org_id,
        "call_id": call_id,
        "session_id": f"legacy-{uuid.uuid4()}",
        "status": "assembling",
        "filename": safe_filename,
        "mime_type": uploaded["recording"]["mime_type"],
        "started_at": uploaded["recording"]["recorded_at"],
        "stopped_at": uploaded["recording"]["recorded_at"],
        "expires_at": expires_at,
        "size_bytes": int(uploaded["recording"]["size_bytes"] or 0),
        "storage": uploaded["storage"],
        "consent": consent,
        "audit": [{"event": "consent_attested", **consent}],
    }
    try:
        db.whatsapp_call_recordings.insert_one(session)
        await _enforce_recording_quota(db, session, session["size_bytes"])
        db.whatsapp_call_recordings.update_one(
            {"org_id": org_id, "call_id": call_id, "session_id": session["session_id"]},
            {"$set": {"status": "ready", "ready_at": datetime.now(timezone.utc)}, "$push": {"audit": {"event": "recording_ready", "at": datetime.now(timezone.utc)}}},
        )
    except Exception:
        await delete_whatsapp_call_asset(uploaded["storage"])
        db.whatsapp_call_recordings.delete_one({"org_id": org_id, "call_id": call_id, "session_id": session["session_id"]})
        raise
    return uploaded


def whatsapp_call_part_upload_signature(org_id: str, call_id: str, session_id: str, sequence: int, expires_at: datetime) -> dict:
    safe_org = re.sub(r"[^A-Za-z0-9_-]", "-", org_id)[:100]
    safe_call = re.sub(r"[^A-Za-z0-9_-]", "-", call_id)[:160]
    public_id = f"whatsapp_call_parts/{safe_org}/{safe_call}/{session_id}/{sequence:03d}"
    timestamp = int(datetime.now(timezone.utc).timestamp())
    params = {
        "context": f"call_id={call_id}|expires_at={expires_at.isoformat()}",
        "overwrite": "false",
        "public_id": public_id,
        "tags": f"{CALL_RECORDING_TAG},recording-session-{session_id}",
        "timestamp": timestamp,
        "type": "private",
    }
    return {
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/raw/upload",
        "cloud_name": CLOUDINARY_CLOUD_NAME,
        "api_key": CLOUDINARY_API_KEY,
        "signature": cloudinary.utils.api_sign_request(params, CLOUDINARY_API_SECRET),
        "params": params,
    }


def verify_whatsapp_call_part_response(payload: dict, expected_public_id: str) -> bool:
    public_id = str(payload.get("public_id") or "")
    version = payload.get("version")
    signature = str(payload.get("signature") or "")
    if public_id != expected_public_id or not isinstance(version, int) or not signature:
        return False
    return cloudinary.utils.verify_api_response_signature(public_id, version, signature)


async def whatsapp_call_part_metadata(public_id: str) -> dict:
    result = await asyncio.to_thread(
        cloudinary.api.resource,
        public_id,
        resource_type="raw",
        type="private",
    )
    return {
        "public_id": result.get("public_id"),
        "asset_id": result.get("asset_id"),
        "resource_type": "raw",
        "delivery_type": "private",
        "format": result.get("format") or "bin",
        "bytes": int(result.get("bytes") or 0),
        "version": result.get("version"),
    }


async def delete_whatsapp_call_asset(storage: dict) -> bool:
    return await asyncio.to_thread(_delete_whatsapp_call_recording_sync, storage)


def whatsapp_call_recording_download_url(storage: dict, expires_in_seconds: int = 300) -> str:
    if not storage.get("public_id"):
        raise RuntimeError("Call recording storage metadata is missing")
    return cloudinary.utils.private_download_url(
        storage["public_id"],
        storage.get("format") or "webm",
        resource_type=storage.get("resource_type") or "video",
        type=storage.get("delivery_type") or "private",
        expires_at=int(datetime.now(timezone.utc).timestamp()) + expires_in_seconds,
        attachment=True,
    )


def _delete_whatsapp_call_recording_sync(storage: dict) -> bool:
    result = cloudinary.uploader.destroy(
        storage.get("public_id"),
        resource_type=storage.get("resource_type") or "video",
        type=storage.get("delivery_type") or "private",
        invalidate=True,
    )
    return result.get("result") in {"ok", "not found"}


def _recording_assets(session: dict) -> list[dict]:
    assets = list((session.get("parts") or {}).values())
    if session.get("pending_storage"):
        assets.append(session["pending_storage"])
    if session.get("storage"):
        assets.append(session["storage"])
    return [asset for asset in assets if asset.get("public_id")]


async def _delete_recording_assets(session: dict) -> bool:
    for asset in _recording_assets(session):
        if not await asyncio.to_thread(_delete_whatsapp_call_recording_sync, asset):
            return False
    return True


async def _enforce_recording_quota(db, session: dict, new_size: int) -> None:
    locks = db.whatsapp_recording_quota_locks
    now = datetime.now(timezone.utc)
    token = uuid.uuid4().hex
    try:
        await asyncio.to_thread(
            locks.update_one,
            {"_id": session["org_id"]},
            {"$setOnInsert": {"lease_until": datetime.fromtimestamp(0, tz=timezone.utc)}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass
    claimed = await asyncio.to_thread(
        locks.find_one_and_update,
        {"_id": session["org_id"], "lease_until": {"$lte": now}},
        {"$set": {"lease_until": now + timedelta(minutes=15), "lease_token": token}},
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        raise RuntimeError("Recording quota is currently being updated")
    try:
        rows = await asyncio.to_thread(
            lambda: list(db.whatsapp_call_recordings.find(
                {
                    "org_id": session["org_id"],
                    "status": {"$in": ["ready", "assembling"]},
                    "session_id": {"$ne": session["session_id"]},
                },
            ).sort("stopped_at", 1))
        )
        eviction_ids, fits = recording_quota_evictions(rows, new_size, CALL_RECORDING_ORG_QUOTA_BYTES)
        for old in (row for row in rows if row.get("_id") in eviction_ids):
            if not await _delete_recording_assets(old):
                raise RuntimeError("Old recording could not be removed for quota")
            await asyncio.to_thread(
                db.whatsapp_call_recordings.update_one,
                {"_id": old["_id"], "status": "ready"},
                {"$set": {"status": "expired", "expired_reason": "quota", "expired_at": now}, "$push": {"audit": {"event": "expired_quota", "at": now}}, "$unset": {"storage": "", "parts": ""}},
            )
            await asyncio.to_thread(
                db.whatsapp_calls.update_one,
                {"org_id": old["org_id"], "call_id": old["call_id"]},
                {"$set": {"recording.status": "expired", "recording.expired_reason": "quota"}, "$unset": {"recording_storage": ""}},
            )
        if not fits:
            raise RuntimeError("Organization recording quota is full")
    finally:
        await asyncio.to_thread(
            locks.update_one,
            {"_id": session["org_id"], "lease_token": token},
            {"$set": {"lease_until": datetime.fromtimestamp(0, tz=timezone.utc)}, "$unset": {"lease_token": ""}},
        )


async def _assemble_call_recording(db, session: dict) -> None:
    parts = session.get("parts") or {}
    last_sequence = min(int(session.get("last_sequence") or 0), CALL_RECORDING_MAX_PARTS - 1)
    ordered = []
    for sequence in range(last_sequence + 1):
        part = parts.get(f"{sequence:03d}")
        if not part:
            break
        ordered.append(part)
    if not ordered:
        raise RuntimeError("No confirmed recording segments are available")

    mime_type = str(session.get("mime_type") or "audio/webm").lower()
    extension = "m4a" if "mp4" in mime_type else "ogg" if "ogg" in mime_type else "webm"
    temp = tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False)
    temp_path = temp.name
    temp.close()
    try:
        total = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            with open(temp_path, "wb") as output:
                for part in ordered:
                    url = cloudinary.utils.private_download_url(
                        part["public_id"], part.get("format") or "bin",
                        resource_type="raw", type="private",
                        expires_at=int(datetime.now(timezone.utc).timestamp()) + 300,
                    )
                    response = await client.get(url)
                    response.raise_for_status()
                    output.write(response.content)
                    total += len(response.content)
        if total <= 0 or total > CALL_RECORDING_MAX_BYTES:
            raise RuntimeError("The assembled recording has an invalid size")
        safe_org = re.sub(r"[^A-Za-z0-9_-]", "-", session["org_id"])[:100]
        safe_call = re.sub(r"[^A-Za-z0-9_-]", "-", session["call_id"])[:160]
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            temp_path,
            public_id=f"whatsapp_call_recordings/{safe_org}/{safe_call}",
            resource_type="video",
            type="private",
            tags=[CALL_RECORDING_TAG],
            context={"call_id": session["call_id"], "expires_at": session["expires_at"].isoformat()},
            overwrite=True,
        )
        storage = {
            "public_id": result["public_id"],
            "asset_id": result.get("asset_id"),
            "resource_type": result.get("resource_type") or "video",
            "delivery_type": result.get("type") or "private",
            "format": result.get("format") or extension,
        }
        size_bytes = int(result.get("bytes") or total)
        await asyncio.to_thread(
            db.whatsapp_call_recordings.update_one,
            {"_id": session["_id"], "lease_token": session["lease_token"]},
            {"$set": {"size_bytes": size_bytes, "pending_storage": storage}},
        )
        try:
            await _enforce_recording_quota(db, session, size_bytes)
        except Exception:
            if await delete_whatsapp_call_asset(storage):
                await asyncio.to_thread(
                    db.whatsapp_call_recordings.update_one,
                    {"_id": session["_id"], "lease_token": session["lease_token"]},
                    {"$unset": {"pending_storage": ""}},
                )
            raise
        recording = {
            "status": "ready",
            "filename": session["filename"],
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "duration_seconds": session.get("duration_seconds"),
            "recorded_at": session["started_at"],
            "expires_at": session["expires_at"],
            "partial": len(ordered) != last_sequence + 1,
        }
        await asyncio.to_thread(
            db.whatsapp_calls.update_one,
            {"org_id": session["org_id"], "call_id": session["call_id"]},
            {"$set": {"recording": recording, "recording_storage": storage}},
        )
        await asyncio.to_thread(
            db.whatsapp_call_recordings.update_one,
            {"_id": session["_id"], "lease_token": session["lease_token"]},
            {"$set": {"status": "ready", "storage": storage, "size_bytes": size_bytes, "ready_at": datetime.now(timezone.utc)}, "$push": {"audit": {"event": "recording_ready", "at": datetime.now(timezone.utc)}}, "$unset": {"pending_storage": "", "lease_token": "", "lease_until": "", "next_attempt_at": ""}},
        )
        if all([await delete_whatsapp_call_asset(part) for part in ordered]):
            await asyncio.to_thread(
                db.whatsapp_call_recordings.update_one,
                {"_id": session["_id"]},
                {"$unset": {"parts": "", "planned_parts": ""}},
            )
        call = await asyncio.to_thread(db.whatsapp_calls.find_one, {"org_id": session["org_id"], "call_id": session["call_id"]})
        if call and call.get("wa_id"):
            from services.websocket_service import broadcast_main_ws
            await broadcast_main_ws(call["wa_id"], "whatsapp", "whatsapp_call", {
                "id": session["call_id"], "call_id": session["call_id"], "event": "recording_ready",
                "status": call.get("state"), "conversation_id": call.get("conversation_id"), "recording": recording,
            })
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


async def process_call_recording_jobs() -> int:
    from database import get_mongo_db
    from services.whatsapp_call_history import meta_datetime

    db = get_mongo_db()
    sessions = db.whatsapp_call_recordings
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=10)
    candidates = await asyncio.to_thread(
        lambda: list(sessions.find({"status": "recording", "$or": [
            {"started_at": {"$lte": now - timedelta(seconds=CALL_RECORDING_MAX_DURATION_SECONDS + 60)}},
            {"last_part_at": {"$lte": stale_before}},
            {"last_part_at": {"$exists": False}, "started_at": {"$lte": stale_before}},
        ]}).limit(50))
    )
    for candidate in candidates:
        call = await asyncio.to_thread(db.whatsapp_calls.find_one, {"org_id": candidate["org_id"], "call_id": candidate["call_id"]}, {"state": 1})
        started_at = meta_datetime(candidate.get("started_at"), now) or now
        timed_out = started_at <= now - timedelta(seconds=CALL_RECORDING_MAX_DURATION_SECONDS + 60)
        terminal = str((call or {}).get("state") or "").lower() in {"completed", "declined", "missed", "failed", "ended", "terminated", "rejected"}
        if not timed_out and not terminal:
            continue
        sequences = [int(key) for key in (candidate.get("parts") or {}) if str(key).isdigit()]
        await asyncio.to_thread(
            sessions.update_one,
            {"_id": candidate["_id"], "status": "recording"},
            {"$set": {
                "status": "finalizing",
                "stopped_at": now,
                "expires_at": recording_retention_deadline(now, CALL_RECORDING_RETENTION_DAYS),
                "last_sequence": max(sequences, default=0),
                "duration_seconds": max(1, min(CALL_RECORDING_MAX_DURATION_SECONDS, int((now - started_at).total_seconds()))),
                "next_attempt_at": now,
            }},
        )

    completed = 0
    for _ in range(5):
        token = uuid.uuid4().hex
        session = await asyncio.to_thread(
            sessions.find_one_and_update,
            {
                "status": {"$in": ["finalizing", "assembling"]},
                "next_attempt_at": {"$lte": now},
                "$or": [{"lease_until": {"$lte": now}}, {"lease_until": {"$exists": False}}],
            },
            {"$set": {"status": "assembling", "lease_token": token, "lease_until": now + timedelta(minutes=5)}},
            sort=[("next_attempt_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if not session:
            break
        try:
            await _assemble_call_recording(db, session)
            completed += 1
        except Exception as exc:
            attempts = int(session.get("attempts") or 0) + 1
            failed = attempts >= 8
            logger.error("WhatsApp recording finalization failed for {}: {}", session.get("call_id"), type(exc).__name__)
            await asyncio.to_thread(
                sessions.update_one,
                {"_id": session["_id"], "lease_token": token},
                {"$set": {
                    "status": "failed" if failed else "finalizing",
                    "attempts": attempts,
                    "next_attempt_at": now + timedelta(seconds=min(300, 2 ** attempts)),
                    "last_error": type(exc).__name__,
                }, "$unset": {"lease_token": "", "lease_until": ""}},
            )
            if failed:
                await asyncio.to_thread(
                    db.whatsapp_calls.update_one,
                    {"org_id": session["org_id"], "call_id": session["call_id"], "recording.status": {"$ne": "ready"}},
                    {"$set": {"recording.status": "failed"}},
                )
    return completed


async def cleanup_expired_call_recordings() -> int:
    from database import get_mongo_db

    db = get_mongo_db()
    calls = db.whatsapp_calls
    sessions = db.whatsapp_call_recordings
    now = datetime.now(timezone.utc)
    deleted = 0
    rows = await asyncio.to_thread(lambda: list(sessions.find({
        "expires_at": {"$lte": now},
        "status": {"$ne": "expired"},
        "$or": [{"status": {"$ne": "deleting"}}, {"delete_lease_until": {"$lte": now}}],
    }).limit(500)))
    for row in rows:
        claimed = await asyncio.to_thread(
            sessions.find_one_and_update,
            {
                "_id": row["_id"],
                "$or": [{"status": {"$ne": "deleting"}}, {"delete_lease_until": {"$lte": now}}],
            },
            {"$set": {"status": "deleting", "delete_lease_until": now + timedelta(minutes=5)}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            continue
        try:
            if not await _delete_recording_assets(claimed):
                raise RuntimeError("Cloudinary did not confirm recording deletion")
            await asyncio.to_thread(
                sessions.update_one,
                {"_id": claimed["_id"], "status": "deleting"},
                {"$set": {"status": "expired", "expired_reason": "retention", "expired_at": now}, "$push": {"audit": {"event": "expired_retention", "at": now}}, "$unset": {"storage": "", "pending_storage": "", "parts": "", "planned_parts": "", "delete_lease_until": ""}},
            )
            await asyncio.to_thread(
                calls.update_one,
                {"org_id": claimed["org_id"], "call_id": claimed["call_id"]},
                {"$set": {"recording.status": "expired", "recording.expired_reason": "retention"}, "$unset": {"recording_storage": ""}},
            )
            deleted += 1
        except Exception as exc:
            logger.error("Failed to delete expired WhatsApp recording {}: {}", claimed.get("call_id"), type(exc).__name__)
            await asyncio.to_thread(
                sessions.update_one,
                {"_id": claimed["_id"], "status": "deleting"},
                {"$set": {"status": row["status"]}, "$unset": {"delete_lease_until": ""}},
            )

    # Legacy whole-file recordings remain eligible during the compatibility window.
    rows = await asyncio.to_thread(
        lambda: list(calls.find(
            {"recording.status": {"$in": ["ready", "expired"]}, "recording.expires_at": {"$lte": now}},
            {"_id": 1, "recording_storage": 1},
        ).limit(500))
    )
    for row in rows:
        storage = row.get("recording_storage") or {}
        try:
            removed = not storage.get("public_id") or await asyncio.to_thread(_delete_whatsapp_call_recording_sync, storage)
            if not removed:
                continue
            await asyncio.to_thread(
                calls.update_one,
                {"_id": row["_id"], "recording.status": {"$in": ["ready", "expired"]}, "recording.expires_at": {"$lte": now}},
                {"$set": {"recording.status": "expired", "recording.expired_reason": "retention"}, "$unset": {"recording_storage": ""}},
            )
            deleted += 1
        except Exception as exc:
            logger.error(f"Failed to delete expired WhatsApp call recording: {exc}")
    if deleted:
        logger.info(f"Deleted {deleted} expired WhatsApp call recordings")
    return deleted


def _cleanup_orphaned_call_recording_parts_sync() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    deleted = 0
    cursor = None
    while True:
        page = cloudinary.api.resources_by_tag(
            CALL_RECORDING_TAG,
            resource_type="raw",
            type="private",
            max_results=500,
            **({"next_cursor": cursor} if cursor else {}),
        )
        for asset in page.get("resources", []):
            try:
                created_at = datetime.fromisoformat(str(asset.get("created_at") or "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at <= cutoff and _delete_whatsapp_call_recording_sync({
                "public_id": asset.get("public_id"),
                "resource_type": "raw",
                "delivery_type": "private",
            }):
                deleted += 1
        cursor = page.get("next_cursor")
        if not cursor:
            return deleted


async def cleanup_orphaned_call_recording_parts() -> int:
    from database import get_mongo_db

    locks = get_mongo_db().whatsapp_recording_quota_locks
    lock_id = "__orphaned_call_recording_parts__"
    now = datetime.now(timezone.utc)
    token = uuid.uuid4().hex
    try:
        try:
            await asyncio.to_thread(
                locks.update_one,
                {"_id": lock_id},
                {"$setOnInsert": {"lease_until": datetime.fromtimestamp(0, tz=timezone.utc)}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass
        claimed = await asyncio.to_thread(
            locks.find_one_and_update,
            {"_id": lock_id, "lease_until": {"$lte": now}},
            {"$set": {"lease_until": now + timedelta(hours=1), "lease_token": token}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return 0
        return await asyncio.to_thread(_cleanup_orphaned_call_recording_parts_sync)
    except Exception as exc:
        logger.error("Failed to delete orphaned WhatsApp recording parts: {}", type(exc).__name__)
        return 0
    finally:
        await asyncio.to_thread(
            locks.update_one,
            {"_id": lock_id, "lease_token": token},
            {"$set": {"lease_until": datetime.fromtimestamp(0, tz=timezone.utc)}, "$unset": {"lease_token": ""}},
        )


async def upload_tally_invoice_pdf(data: bytes, org_id: str, invoice_id: str, alter_id: str) -> dict:
    """Store an exact Tally export privately for at most 30 days."""
    safe_invoice = re.sub(r"[^A-Za-z0-9_-]", "-", invoice_id)[:100] or uuid.uuid4().hex
    safe_alter = re.sub(r"[^A-Za-z0-9_-]", "-", alter_id)[:40] or "current"
    file_obj = io.BytesIO(data)
    file_obj.name = f"{safe_invoice}-{safe_alter}.pdf"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=TALLY_PDF_RETENTION_DAYS)
    result = await asyncio.to_thread(
        cloudinary.uploader.upload,
        file_obj,
        folder=f"tally_invoices/{org_id}",
        public_id=file_obj.name,
        resource_type="raw",
        type="private",
        tags=[TALLY_PDF_RETENTION_TAG],
        context={"expires_at": expires_at.isoformat()},
        overwrite=True,
    )
    if not result.get("public_id"):
        raise RuntimeError("Cloudinary did not return a Tally invoice PDF identifier")
    return {
        "public_id": result["public_id"], "asset_id": result.get("asset_id"),
        "resource_type": result.get("resource_type") or "raw", "delivery_type": result.get("type") or "private",
        "format": result.get("format") or "pdf", "generated_at": now, "expires_at": expires_at,
    }


async def upload_native_invoice_pdf(
    data: bytes,
    org_id: str,
    invoice_id: str,
    invoice_number: str,
    *,
    renderer_version: int,
    issued_revision: int,
) -> dict:
    """Store the immutable customer PDF for an issued native invoice."""
    safe_org = re.sub(r"[^A-Za-z0-9_-]", "-", org_id)[:100] or "organization"
    safe_invoice = re.sub(r"[^A-Za-z0-9_-]", "-", invoice_id)[:100] or uuid.uuid4().hex
    safe_number = re.sub(r"[^A-Za-z0-9_-]", "-", invoice_number)[:60] or "invoice"
    digest = hashlib.sha256(data).hexdigest()
    file_obj = io.BytesIO(data)
    file_obj.name = f"{safe_number}.pdf"
    now = datetime.now(timezone.utc)
    folder = f"native_invoices/{safe_org}/{safe_invoice}"
    leaf_id = f"v{renderer_version}-{digest}.pdf"
    expected_public_id = f"{folder}/{leaf_id}"
    try:
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            file_obj,
            folder=folder,
            public_id=leaf_id,
            resource_type="raw",
            type="private",
            tags=[NATIVE_INVOICE_TAG],
            context={
                "sha256": digest,
                "renderer_version": str(renderer_version),
                "issued_revision": str(issued_revision),
            },
            overwrite=False,
            unique_filename=False,
        )
    except Exception:
        # A retry may follow a successful upload whose database write failed.
        result = await asyncio.to_thread(
            cloudinary.api.resource,
            expected_public_id,
            resource_type="raw",
            type="private",
        )
    if result.get("public_id") != expected_public_id:
        raise RuntimeError("Cloudinary did not return a native invoice PDF identifier")
    if int(result.get("bytes") or -1) != len(data):
        raise RuntimeError("Stored native invoice PDF size does not match the rendered artifact")
    return {
        "public_id": result["public_id"],
        "asset_id": result.get("asset_id"),
        "version": result.get("version"),
        "resource_type": result.get("resource_type") or "raw",
        "delivery_type": result.get("type") or "private",
        "format": result.get("format") or "pdf",
        "sha256": digest,
        "byte_size": len(data),
        "renderer_version": renderer_version,
        "issued_revision": issued_revision,
        "generated_at": now,
    }


def native_invoice_pdf_download_url(pdf: dict, expires_in_seconds: int = 60) -> str:
    if (
        not re.fullmatch(r"[0-9a-f]{64}", str(pdf.get("sha256") or ""))
        or not all(pdf.get(field) is not None for field in ("version", "byte_size", "renderer_version", "issued_revision"))
    ):
        raise RuntimeError("Native invoice PDF is not a verified server artifact")
    return cloudinary.utils.private_download_url(
        pdf["public_id"], pdf.get("format") or "pdf", resource_type=pdf.get("resource_type") or "raw",
        type=pdf.get("delivery_type") or "private",
        expires_at=int(datetime.now(timezone.utc).timestamp()) + expires_in_seconds,
        attachment=True,
    )


def tally_invoice_pdf_download_url(pdf: dict, expires_in_seconds: int = 300) -> str:
    """Create a short-lived URL; legacy public URLs remain usable only until their stored expiry."""
    if pdf.get("public_id"):
        return cloudinary.utils.private_download_url(
            pdf["public_id"], pdf.get("format") or "pdf", resource_type=pdf.get("resource_type") or "raw",
            type=pdf.get("delivery_type") or "private",
            expires_at=int(datetime.now(timezone.utc).timestamp()) + expires_in_seconds,
            attachment=True,
        )
    if pdf.get("url"):
        return pdf["url"]
    raise RuntimeError("Tally invoice PDF storage metadata is missing")


def _cleanup_expired_tally_invoice_pdfs_sync(days_threshold: int) -> int:
    groups = {}
    next_cursor = None
    while True:
        search = cloudinary.Search().expression(
            f"folder:tally_invoices/* AND resource_type:raw AND created_at<={days_threshold}d"
        ).max_results(500)
        if next_cursor:
            search = search.next_cursor(next_cursor)
        result = search.execute()
        for resource in result.get("resources", []):
            if resource.get("public_id"):
                groups.setdefault((resource.get("resource_type") or "raw", resource.get("type") or "upload"), []).append(resource["public_id"])
        next_cursor = result.get("next_cursor")
        if not next_cursor:
            break
    deleted = 0
    for (resource_type, delivery_type), public_ids in groups.items():
        for start in range(0, len(public_ids), 100):
            response = cloudinary.api.delete_resources(
                public_ids[start:start + 100], resource_type=resource_type, type=delivery_type,
            )
            deleted += sum(status in {"deleted", "not_found"} for status in response.get("deleted", {}).values())
    return deleted


async def cleanup_expired_tally_invoice_pdfs(days_threshold: int = TALLY_PDF_RETENTION_DAYS) -> int | None:
    try:
        deleted = await asyncio.to_thread(_cleanup_expired_tally_invoice_pdfs_sync, days_threshold)
        if deleted:
            logger.info(f"Deleted {deleted} expired Tally invoice PDFs from Cloudinary")
        return deleted
    except Exception as exc:
        logger.error(f"Failed to delete expired Tally invoice PDFs: {exc}")
        return None

# @app.get("/api/cloudinary/stats")
async def get_cloudinary_stats():
    """
    Get current Cloudinary storage usage statistics
    """
    try:
        cloud_name = CLOUDINARY_CLOUD_NAME
        api_key = CLOUDINARY_API_KEY
        api_secret = CLOUDINARY_API_SECRET
        
        # Request usage data from Cloudinary Admin API
        response = requests.get(
            f"https://api.cloudinary.com/v1_1/{cloud_name}/usage",
            auth=(api_key, api_secret)
        )
        
        if response.status_code != 200:
            return {
                "status": "error", 
                "message": "Failed to fetch Cloudinary stats",
                "error": response.text
            }
            
        usage_data = response.json()
        
        # Calculate storage in GB
        storage_bytes = usage_data.get("storage", {}).get("usage", 0)
        storage_gb = storage_bytes / (1024 * 1024 * 1024)  # Convert bytes to GB
        
        # Calculate percentage of 25GB limit
        percentage_used = (storage_gb / 25) * 100
        
        return {
            "status": "success",
            "storage": {
                "bytes": storage_bytes,
                "gigabytes": round(storage_gb, 2),
                "percentage_of_limit": round(percentage_used, 2)
            },
            "bandwidth": usage_data.get("bandwidth", {}),
            "resources": usage_data.get("resources", {}),
            "is_approaching_limit": percentage_used > 80
        }
        
    except Exception as e:
        logger.error(f"Error fetching Cloudinary stats: {e}")
        return {"status": "error", "message": str(e)}

async def cleanup_old_cloudinary_media(days_threshold=30):
    """
    Delete Cloudinary resources (image, video, raw) older than the specified threshold.
    Uses Search API to safely find files by date before deleting.
    """
    try:
        # 1. Calculate cutoff date in YYYY-MM-DD format (Required for Cloudinary Search)
        cutoff_date = datetime.now() - timedelta(days=days_threshold)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d')
        
        folders = ["whatsapp_media", "instagram_media"]
        resource_types = ["image", "video", "raw"] # 'raw' covers PDFs, DOCs, etc.
        
        total_deleted = 0
        deleted_details = {}

        for folder in folders:
            for r_type in resource_types:
                # 2. Build Search Expression
                # Syntax: folder:name/* AND resource_type:type AND created_at<=date
                expression = f"folder:{folder}/* AND resource_type:{r_type} AND -tags={AUDIO_RETENTION_TAG} AND created_at<={cutoff_str}"
                
                # 3. Search for matching files (Limit 500 per run to be safe)
                # Note: If you have >500 old files, this runs daily so it will catch up eventually.
                search_result = cloudinary.Search()\
                    .expression(expression)\
                    .max_results(500)\
                    .execute()
                
                resources = search_result.get('resources', [])
                public_ids = [res['public_id'] for res in resources]
                
                if public_ids:
                    logger.info(f"Deleting {len(public_ids)} {r_type}s from {folder}...")
                    
                    # 4. Delete by Public IDs
                    delete_response = cloudinary.api.delete_resources(
                        public_ids, 
                        resource_type=r_type
                    )
                    
                    count = len(delete_response.get("deleted", {}))
                    total_deleted += count
                    deleted_details[f"{folder}_{r_type}"] = count
                else:
                    deleted_details[f"{folder}_{r_type}"] = 0

        return {
            "status": "success",
            "total_deleted": total_deleted,
            "details": deleted_details
        }
        
    except Exception as e:
        logger.error(f"Error cleaning up Cloudinary resources: {e}")
        return {"status": "error", "message": str(e)}

async def alert_if_storage_high():
    """Check storage usage and send alert if approaching limit"""
    stats = await get_cloudinary_stats()
    if stats.get("status") == "success" and stats["storage"]["percentage_of_limit"] > 75:
        # Send alert (email, Slack, etc.)
        logger.warning(f"Cloudinary storage at {stats['storage']['percentage_of_limit']}% of 25GB limit")
        # Implement notification system here

async def check_cloudinary_storage_limit():
    """Check if storage is approaching 25GB limit"""
    try:
        stats = await get_cloudinary_stats()
        if stats.get("status") == "success":
            if stats["storage"]["percentage_of_limit"] > 90:
                # Perform emergency cleanup if over 90%
                await cleanup_old_cloudinary_media(days_threshold=15)
            return stats["storage"]["percentage_of_limit"] < 95  # Allow uploads if under 95%
        return True  # Default to allowing uploads if check fails
    except Exception:
        return True  # Allow uploads if check fails
    
async def upload_and_send_whatsapp_media_background(
    media_url: Optional[str],
    media_id: str, 
    org_id: str, 
    mime_type: str, 
    access_token: str, 
    message_type: str,
    whatsapp_business_id: str,
    message_id: str,
    media_name: str,
    caption: Optional[str] = None,
    phone_number_id: Optional[str] = None,
) -> Optional[str]:
    """
    Downloads media from WhatsApp and uploads to Cloudinary.
    This is designed to be run as a BackgroundTask.
    """
    from services.wa_service import update_whatsapp_media_message
    logger.success(f"Background Task Started: Processing media {media_id}")
    
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    if mime_type == "application/pdf":
        mime_type = "pdf"

    try:
        # Meta URLs are short-lived. Retry the complete retrieve/download/upload
        # operation, using the stable media ID each time.
        last_error = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    media_bytes, mime_type = await _download_whatsapp_media(
                        client, media_url, media_id, mime_type, headers, phone_number_id
                    )

                file_obj = io.BytesIO(media_bytes)
                file_obj.name = media_id
                cld_resource_type = "raw"
                if "image" in mime_type:
                    cld_resource_type = "image"
                elif "video" in mime_type or "audio" in mime_type:
                    cld_resource_type = "video"
                elif "pdf" in mime_type:
                    cld_resource_type = "image"

                result = await asyncio.to_thread(
                    cloudinary.uploader.upload,
                    file_obj,
                    folder=f"whatsapp_media/{org_id}/customer/",
                    public_id=media_id,
                    resource_type=cld_resource_type,
                    **({"tags": [AUDIO_RETENTION_TAG]} if message_type == "audio" else {}),
                )
                if not result.get("secure_url"):
                    raise RuntimeError("Cloudinary did not return a media URL")
                break
            except Exception as exc:
                last_error = exc
                media_url = None
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("WhatsApp media processing failed after three attempts") from last_error

        secure_url = result.get("secure_url")
        file_format = result.get("format")
        file_size = result.get("bytes")
        file_pages = result.get("pages")

        logger.success(f"Upload Successful: {secure_url}")

        # 4. Generate Thumbnail URL
        # If we uploaded as image/video (including PDF), we can swap extension
        thumbnail_url = None
        
        if cld_resource_type == "image" and mime_type == "pdf":
            # Convert .../upload/v123/file.pdf -> .../upload/v123/file.jpg
            # Cloudinary usually returns the PDF link, so we force JPG for thumbnail
            if secure_url.endswith(".pdf"):
                thumbnail_url = secure_url[:-4] + ".jpg"

        # 5. Prepare Payload with Thumbnail
        payload = {
            "url": secure_url,
            "caption": caption,
            "thumbnail_url": thumbnail_url, 
            "file_name": media_name,
            "file_size": file_size,
            "file_format" : file_format,
            "file_pages": file_pages,
            "media_id": media_id,
            "mime_type": mime_type,
            "media_status": "ready",
        }

        logger.success(f"Uploaded: {secure_url}")
        logger.success(f"Preview:  {thumbnail_url}")

        # ---------------------------------------------------------
        # Here I am storing & broadcasting this type of message after upload is completed.
        # TODO: In future we want to notify frontend on new message
        # have this process in background, after upload send a new message
        # that image is ready with the Cloudinary URL.
        # Till then frontend will show loading

        # This is because -> 
        # Frontend immmediately receives message that a new image has arrived
        # but the image is not yet uploaded to Cloudinary, so it shows loading.
        # ---------------------------------------------------------

        # Update the durable placeholder and broadcast the same message ID.
        await update_whatsapp_media_message(
            org_id=org_id,
            message_id=message_id,
            payload=payload,
            recipient_id=whatsapp_business_id,
            status="received",
        )
        return secure_url

    except Exception as e:
        logger.error(f"Background Upload Failed: {e}")
        await update_whatsapp_media_message(
            org_id=org_id,
            message_id=message_id,
            payload={
                "media_id": media_id,
                "mime_type": mime_type,
                "file_name": media_name,
                "media_status": "failed",
            },
            recipient_id=whatsapp_business_id,
            status="received",
        )
        return None

def _outbound_media_spec(mime_type: str):
    base_mime = (mime_type or "").lower().split(";", 1)[0].strip()
    if base_mime.startswith("image/"):
        return "image", "image", False
    if base_mime.startswith("video/"):
        return "video", "video", False
    if base_mime.startswith("audio/"):
        supported = {"audio/aac", "audio/amr", "audio/mp4", "audio/mpeg", "audio/mp3", "audio/ogg"}
        is_supported = base_mime in supported and (base_mime != "audio/ogg" or "opus" in mime_type.lower())
        return "audio", "video", not is_supported
    return "document", "image" if base_mime == "application/pdf" else "raw", False


async def upload_media_to_cloudinary(file: any, platform: str, org_id: str, media_kind: Optional[str] = None) -> Optional[dict]:
    """
    Uploads a media file to Cloudinary under the specified platform folder.
    Returns the secure URL of the uploaded media.
    """
    storage_ok = await check_cloudinary_storage_limit()

    if not storage_ok:
        logger.error("Cloudinary storage limit approaching. Upload aborted.")
        return None

    try:
        if platform == "whatsapp":
            folder = f"whatsapp_media/{org_id}/user"
        elif platform == "instagram":
            folder = f"instagram_media/{org_id}/user"
        else:
            folder = f"other_media/{org_id}/user"
        mime_type = (file.content_type or "").lower()
        semantic_type, cld_resource_type, convert_audio = _outbound_media_spec(mime_type)
        if media_kind == "voice_note" and not (mime_type.startswith("audio/ogg") and "opus" in mime_type):
            convert_audio = True
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", file.filename or "attachment")
        unique_name = f"{uuid.uuid4().hex}-{safe_name}"
        public_id = unique_name if cld_resource_type == "raw" else unique_name.rsplit(".", 1)[0]
        upload_options = _outbound_upload_options(semantic_type, convert_audio, media_kind)

        # Upload to Cloudinary
        result = await asyncio.to_thread(
            cloudinary.uploader.upload,
            file.file,
            folder=folder,
            public_id=public_id,
            resource_type=cld_resource_type,
            **upload_options,
        )

        secure_url = result.get("secure_url")
        if convert_audio:
            eager = result.get("eager") or []
            if not eager or not eager[0].get("secure_url"):
                raise ValueError("Cloudinary did not return the converted audio file")
            secure_url = eager[0]["secure_url"]
            voice_note = media_kind == "voice_note"
            mime_type = "audio/ogg; codecs=opus" if voice_note else "audio/mpeg"
            extension = "ogg" if voice_note else "mp3"
            safe_name = f"{safe_name.rsplit('.', 1)[0]}.{extension}"
        logger.success(f"Media uploaded to Cloudinary: {secure_url}")
        return {
            "file_url": secure_url,
            "file_type": semantic_type,
            "mime_type": mime_type,
            "file_name": safe_name,
        }

    except Exception as e:
        logger.error(f"Error uploading media to Cloudinary: {e}")
        return None
