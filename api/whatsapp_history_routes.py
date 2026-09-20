"""Admin-only endpoints for durable WhatsApp backup history imports."""

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from gridfs import GridFSBucket
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from auth.auth_utils import admin_required
from auth.dependencies import CurrentUser
from core.limiter import limiter
from database import get_mongo_db
from services.whatsapp_history_import import (
    ACTIVE_STATUSES,
    COMPLETED_TTL,
    GRID_BUCKET,
    IMPORT_TTL,
    MAX_BACKUP_BYTES,
    ImportConfigurationError,
    cleanup_job_artifacts,
    delete_backup_file,
    encrypt_credential,
    ensure_indexes,
)

router = APIRouter(prefix="/api/whatsapp/history-imports", tags=["WhatsApp history imports"])
_HEX_KEY = re.compile(r"^[a-fA-F0-9]{64}$")
_KEY_FILE_MAX_BYTES = 64 * 1024
_CANCELLABLE_STATUSES = ("uploading", "queued", "processing", "ready")


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return None
    allowed = {"job_id", "status", "error", "preview", "result", "created_at", "updated_at"}
    return {key: value for key, value in job.items() if key in allowed}


async def _read_key_file(upload: UploadFile) -> bytes:
    value = await upload.read(_KEY_FILE_MAX_BYTES + 1)
    if not value:
        raise HTTPException(status_code=422, detail="The encryption key file is empty.")
    if len(value) > _KEY_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The encryption key file is too large.")
    return value


async def _store_backup(db, job: dict, upload: UploadFile) -> tuple[int, str]:
    bucket = GridFSBucket(db, bucket_name=GRID_BUCKET)
    stream = bucket.open_upload_stream(
        "backup.crypt15",
        _id=job["backup_file_id"],
        metadata={"job_id": job["job_id"], "org_id": job["org_id"], "expires_at": job["expires_at"]},
    )
    total = 0
    digest = hashlib.sha256()
    try:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                raise HTTPException(status_code=413, detail="The uploaded backup is too large.")
            digest.update(chunk)
            await asyncio.to_thread(stream.write, chunk)
        if not total:
            raise HTTPException(status_code=422, detail="The uploaded backup is empty.")
        await asyncio.to_thread(stream.close)
        return total, digest.hexdigest()
    except BaseException:
        await asyncio.to_thread(stream.abort)
        raise


@router.post("", status_code=202)
@limiter.limit("3/hour")
async def create_history_import(
    request: Request,
    user: CurrentUser,
    business_phone_id: str = Form(...),
    backup: UploadFile = File(...),
    consent_confirmed: bool = Form(False),
    decryption_key: Optional[str] = Form(None),
    key_file: Optional[UploadFile] = File(None),
):
    await admin_required(user)
    if not consent_confirmed:
        raise HTTPException(status_code=422, detail="Confirm that you are authorized to import this backup.")
    phone_id = business_phone_id.strip()
    db = get_mongo_db()
    connection = db.whatsapp_connections.find_one({
        "org_id": user.org_id,
        "$or": [{"phone_id": phone_id}, {"phone_number_id": phone_id}],
        "is_active": {"$ne": False},
    }, {"phone": 1, "display_phone_number": 1})
    if not connection:
        raise HTTPException(status_code=404, detail="WhatsApp connection not found.")
    if not (backup.filename or "").lower().endswith(".crypt15"):
        raise HTTPException(status_code=422, detail="Upload an Android .crypt15 backup.")

    clean_key = "".join((decryption_key or "").split())
    if bool(clean_key) == bool(key_file):
        raise HTTPException(status_code=422, detail="Provide either the 64-digit key or encrypted_backup.key.")
    if clean_key and not _HEX_KEY.fullmatch(clean_key):
        raise HTTPException(status_code=422, detail="The encryption key must contain exactly 64 hexadecimal characters.")
    credential = clean_key.encode("ascii") if clean_key else await _read_key_file(key_file)
    try:
        encrypted_credential = encrypt_credential(credential)
    except ImportConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    ensure_indexes()
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()),
        "org_id": user.org_id,
        "created_by": user.user_id,
        "business_phone_id": phone_id,
        "business_phone": connection.get("phone") or connection.get("display_phone_number") or "",
        "active_key": f"{user.org_id}:{phone_id}",
        "status": "uploading",
        "backup_file_id": ObjectId(),
        "encrypted_credential": encrypted_credential,
        "credential_kind": "hex" if clean_key else "file",
        "process_attempts": 0,
        "commit_attempts": 0,
        "created_at": now,
        "updated_at": now,
        "expires_at": now + IMPORT_TTL,
    }
    try:
        db.whatsapp_history_imports.insert_one(job)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="This WhatsApp number already has an unfinished history import.") from exc

    try:
        size, digest = await _store_backup(db, job, backup)
        queued = db.whatsapp_history_imports.find_one_and_update(
            {"job_id": job["job_id"], "org_id": user.org_id, "status": "uploading"},
            {"$set": {"status": "queued", "backup_size": size, "backup_sha256": digest, "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        if not queued:
            raise HTTPException(status_code=409, detail="The history import was cancelled during upload.")
        return _public_job(queued)
    except BaseException:
        delete_backup_file(db, job["backup_file_id"])
        db.whatsapp_history_imports.delete_one({"job_id": job["job_id"], "status": "uploading"})
        raise


@router.get("")
@limiter.limit("60/minute")
async def get_active_history_import(request: Request, user: CurrentUser, business_phone_id: str = Query(...)):
    await admin_required(user)
    job = get_mongo_db().whatsapp_history_imports.find_one(
        {"org_id": user.org_id, "business_phone_id": business_phone_id.strip(), "status": {"$in": ACTIVE_STATUSES}},
        sort=[("created_at", -1)],
    )
    return _public_job(job)


@router.get("/{job_id}")
@limiter.limit("60/minute")
async def get_history_import(request: Request, job_id: str, user: CurrentUser):
    await admin_required(user)
    job = get_mongo_db().whatsapp_history_imports.find_one({"job_id": job_id, "org_id": user.org_id})
    if not job:
        raise HTTPException(status_code=404, detail="History import not found.")
    return _public_job(job)


@router.delete("/{job_id}")
@limiter.limit("10/minute")
async def cancel_history_import(request: Request, job_id: str, user: CurrentUser):
    await admin_required(user)
    db = get_mongo_db()
    now = datetime.now(timezone.utc)
    existing = db.whatsapp_history_imports.find_one({"job_id": job_id, "org_id": user.org_id})
    if existing and existing.get("status") == "ready" and existing.get("commit_attempts"):
        partially_imported = (
            db[f"messages_{user.org_id}"].count_documents({"history_import_job_id": job_id}, limit=1)
            or db[f"conversations_{user.org_id}"].count_documents({"history_import_job_id": job_id}, limit=1)
        )
        if partially_imported:
            raise HTTPException(status_code=409, detail="This import was partially saved and must be retried, not discarded.")
    job = db.whatsapp_history_imports.find_one_and_update(
        {"job_id": job_id, "org_id": user.org_id, "status": {"$in": _CANCELLABLE_STATUSES}},
        {
            "$set": {"status": "cancelled", "updated_at": now, "expires_at": now + COMPLETED_TTL},
            "$unset": {
                "active_key": "", "lease_id": "", "lease_until": "", "encrypted_credential": "", "credential_kind": "", "backup_file_id": "",
            },
        },
        return_document=ReturnDocument.BEFORE,
    )
    if not job:
        if not existing:
            raise HTTPException(status_code=404, detail="History import not found.")
        raise HTTPException(status_code=409, detail="This history import can no longer be cancelled.")
    cleanup_job_artifacts(db, job)
    return {"job_id": job_id, "status": "cancelled", "updated_at": now}


@router.post("/{job_id}/commit", status_code=202)
@limiter.limit("10/minute")
async def commit_history_import(request: Request, job_id: str, user: CurrentUser):
    await admin_required(user)
    db = get_mongo_db()
    job = db.whatsapp_history_imports.find_one({"job_id": job_id, "org_id": user.org_id})
    if not job:
        raise HTTPException(status_code=404, detail="History import not found.")
    if job.get("status") in {"completed", "importing"}:
        return _public_job(job)
    claimed = db.whatsapp_history_imports.find_one_and_update(
        {"job_id": job_id, "org_id": user.org_id, "status": "ready"},
        {
            "$set": {"status": "importing", "commit_attempts": 0, "updated_at": datetime.now(timezone.utc)},
            "$unset": {"error": "", "lease_id": "", "lease_until": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="This history import is not ready to commit.")
    return _public_job(claimed)
