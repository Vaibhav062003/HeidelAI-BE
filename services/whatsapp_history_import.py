"""Durable, tenant-scoped import of encrypted Android WhatsApp history."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

import ijson
from bson import ObjectId
from cryptography.fernet import Fernet, InvalidToken
from gridfs import GridFSBucket, NoFile
from pymongo import ReturnDocument, UpdateOne

logger = logging.getLogger(__name__)

MAX_BACKUP_BYTES = max(1, min(int(os.getenv("WHATSAPP_HISTORY_IMPORT_MAX_BYTES", 256 * 1024 * 1024)), 1024 * 1024 * 1024))
MAX_REQUEST_BYTES = MAX_BACKUP_BYTES + 2 * 1024 * 1024
MAX_DECRYPTED_BYTES = max(1, min(int(os.getenv("WHATSAPP_HISTORY_IMPORT_MAX_DECRYPTED_BYTES", 512 * 1024 * 1024)), 1024 * 1024 * 1024))
MAX_MESSAGES = max(1, min(int(os.getenv("WHATSAPP_HISTORY_IMPORT_MAX_MESSAGES", 500_000)), 2_000_000))
IMPORT_TTL = timedelta(hours=24)
COMPLETED_TTL = timedelta(days=7)
LEASE_TTL = timedelta(minutes=30)
GRID_BUCKET = "whatsapp_history_import_files"
ACTIVE_STATUSES = ("uploading", "queued", "processing", "ready", "importing")
_PHONE = re.compile(r"^\+?(\d{6,20})(?:@(?:s\.whatsapp\.net|c\.us))?$")
_worker_task: asyncio.Task | None = None
_worker_stop = threading.Event()
_last_recovery = 0.0


class ImportFailure(Exception):
    """Expected failure whose message is safe to show to an administrator."""


class ImportConfigurationError(Exception):
    pass


class ImportCancelled(Exception):
    pass


def _fernet() -> Fernet:
    key = os.getenv("WHATSAPP_HISTORY_IMPORT_ENCRYPTION_KEY", "")
    if not key:
        raise ImportConfigurationError("WhatsApp history import encryption is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ImportConfigurationError("WhatsApp history import encryption is misconfigured.") from exc


def encrypt_credential(value: bytes) -> bytes:
    return _fernet().encrypt(value)


def _decrypt_credential(value: bytes) -> bytes:
    try:
        return _fernet().decrypt(value)
    except InvalidToken as exc:
        raise ImportFailure("The stored backup key could not be decrypted.") from exc


def _message_row(
    chat_id: object,
    chat_name: object,
    source_id: object,
    message: object,
    *,
    job_id: str,
    org_id: str,
    phone_id: str,
    business_phone: str,
    expires_at: datetime,
    now: datetime,
) -> dict | None:
    match = _PHONE.fullmatch(str(chat_id).strip())
    if not match or not isinstance(message, dict) or message.get("meta"):
        return None
    try:
        timestamp = float(message.get("timestamp"))
        if timestamp > 9_999_999_999:
            timestamp /= 1000
        sent_at = datetime.fromtimestamp(timestamp, timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if sent_at < datetime(2009, 1, 1, tzinfo=timezone.utc) or sent_at > now + timedelta(days=1):
        return None

    customer_phone = match.group(1)
    content = next(
        (value.strip() for value in (message.get("data"), message.get("caption")) if isinstance(value, str) and value.strip()),
        "[Media omitted]" if message.get("media") or message.get("mime") else "[Unsupported WhatsApp message]",
    )[:65_536]
    stable_source = str(message.get("key_id") or source_id)
    message_id = "wa-history:" + hashlib.sha256(f"{phone_id}:{chat_id}:{stable_source}".encode()).hexdigest()
    return {
        "job_id": job_id,
        "org_id": org_id,
        "expires_at": expires_at,
        "message_id": message_id,
        "conversation_id": f"whatsapp_{phone_id}_{customer_phone}",
        "customer_phone": customer_phone,
        "customer_name": str(chat_name or customer_phone).strip()[:200],
        "business_phone_id": phone_id,
        "business_phone": business_phone,
        "content": content,
        "role": "agent" if bool(message.get("from_me")) else "customer",
        "timestamp": sent_at,
    }


def normalize_export(payload: object, *, job_id: str, org_id: str, phone_id: str, business_phone: str) -> tuple[list[dict], dict]:
    """Small-payload reference normalizer used by the contract check."""
    if not isinstance(payload, dict):
        raise ImportFailure("The decrypted backup did not contain a valid WhatsApp export.")
    now = datetime.now(timezone.utc)
    expires_at = now + IMPORT_TTL
    rows: list[dict] = []
    conversations: set[str] = set()
    skipped = 0
    for chat_id, chat in payload.items():
        messages = chat.get("messages", {}) if isinstance(chat, dict) else {}
        items = messages.items() if isinstance(messages, dict) else enumerate(messages) if isinstance(messages, list) else ()
        for source_id, message in items:
            row = _message_row(
                chat_id,
                chat.get("name") if isinstance(chat, dict) else None,
                source_id,
                message,
                job_id=job_id,
                org_id=org_id,
                phone_id=phone_id,
                business_phone=business_phone,
                expires_at=expires_at,
                now=now,
            )
            if row is None:
                skipped += 1
            else:
                rows.append(row)
                conversations.add(row["conversation_id"])
                if len(rows) > MAX_MESSAGES:
                    raise ImportFailure(f"This backup exceeds the {MAX_MESSAGES:,}-message import limit.")
    if not rows:
        raise ImportFailure("No supported one-to-one messages were found in this backup.")
    timestamps = [row["timestamp"] for row in rows]
    return rows, {
        "conversation_count": len(conversations),
        "message_count": len(rows),
        "skipped_count": skipped,
        "first_message_at": min(timestamps),
        "last_message_at": max(timestamps),
    }


def iter_export_messages(handle: BinaryIO) -> Iterator[tuple[object, object, object, object]]:
    """Yield one message at a time from exporter JSON without loading a chat into RAM."""
    stack: list[dict] = []
    for event, value in ijson.basic_parse(handle):
        if event == "map_key":
            stack[-1]["key"] = value
            continue
        if event in ("start_map", "start_array"):
            if not stack:
                stack.append({"kind": "root" if event == "start_map" else "ignore", "key": None})
                continue
            parent = stack[-1]
            key = parent.pop("key", None)
            if parent["kind"] == "root" and event == "start_map":
                frame = {"kind": "chat", "chat_id": key, "name": None, "key": None}
            elif parent["kind"] == "chat" and key == "messages":
                frame = {"kind": "messages_map" if event == "start_map" else "messages_array", "chat": parent, "index": 0, "key": None}
            elif parent["kind"] == "messages_map" and event == "start_map":
                frame = {"kind": "message", "chat": parent["chat"], "source_id": key, "data": {}, "key": None}
            elif parent["kind"] == "messages_array" and event == "start_map":
                source_id = parent["index"]
                parent["index"] += 1
                frame = {"kind": "message", "chat": parent["chat"], "source_id": source_id, "data": {}, "key": None}
            else:
                if parent["kind"] == "message" and key in {"meta", "media", "mime"}:
                    parent["data"][key] = True
                frame = {"kind": "ignore", "key": None}
            stack.append(frame)
            continue
        if event in ("end_map", "end_array"):
            frame = stack.pop()
            if frame["kind"] == "message":
                chat = frame["chat"]
                yield chat["chat_id"], chat["name"], frame["source_id"], frame["data"]
            continue

        frame = stack[-1]
        key = frame.pop("key", None)
        if frame["kind"] == "chat" and key == "name":
            frame["name"] = value
        elif frame["kind"] == "message" and key in {"from_me", "timestamp", "key_id", "data", "caption", "meta", "media", "mime"}:
            frame["data"][key] = value
        elif frame["kind"] == "messages_map":
            chat = frame["chat"]
            yield chat["chat_id"], chat["name"], key, value
        elif frame["kind"] == "messages_array":
            source_id = frame["index"]
            frame["index"] += 1
            chat = frame["chat"]
            yield chat["chat_id"], chat["name"], source_id, value


def ensure_indexes() -> None:
    from database import get_mongo_db

    db = get_mongo_db()
    db.whatsapp_history_imports.create_index("job_id", unique=True)
    db.whatsapp_history_imports.create_index("active_key", unique=True, sparse=True)
    db.whatsapp_history_imports.create_index("expires_at", expireAfterSeconds=0)
    db.whatsapp_history_imports.create_index([("status", 1), ("lease_until", 1), ("created_at", 1)])
    db.whatsapp_history_import_messages.create_index([("job_id", 1), ("message_id", 1)], unique=True)
    db.whatsapp_history_import_messages.create_index([("job_id", 1), ("org_id", 1), ("conversation_id", 1), ("timestamp", 1)])
    db.whatsapp_history_import_messages.create_index("expires_at", expireAfterSeconds=0)
    db[f"{GRID_BUCKET}.files"].create_index("metadata.expires_at")


def delete_backup_file(db, file_id: ObjectId | None) -> None:
    if not file_id:
        return
    try:
        GridFSBucket(db, bucket_name=GRID_BUCKET).delete(file_id)
    except NoFile:
        db[f"{GRID_BUCKET}.chunks"].delete_many({"files_id": file_id})


def cleanup_job_artifacts(db, job: dict) -> None:
    db.whatsapp_history_import_messages.delete_many({"job_id": job["job_id"], "org_id": job["org_id"]})
    delete_backup_file(db, job.get("backup_file_id"))


def _claimed(db, job: dict, status: str) -> bool:
    return db.whatsapp_history_imports.count_documents({"job_id": job["job_id"], "lease_id": job["lease_id"], "status": status}, limit=1) == 1


def _refresh_lease(db, job: dict, status: str) -> None:
    result = db.whatsapp_history_imports.update_one(
        {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": status},
        {"$set": {"lease_until": datetime.now(timezone.utc) + LEASE_TTL}},
    )
    if not result.matched_count or _worker_stop.is_set():
        raise ImportCancelled()


def _claim(statuses: tuple[str, ...], target: str, attempt_field: str) -> dict | None:
    from database import get_mongo_db

    now = datetime.now(timezone.utc)
    return get_mongo_db().whatsapp_history_imports.find_one_and_update(
        {
            "status": {"$in": statuses},
            attempt_field: {"$lt": 3},
            "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lte": now}}],
        },
        {
            "$set": {"status": target, "lease_id": str(uuid.uuid4()), "lease_until": now + LEASE_TTL, "updated_at": now},
            "$inc": {attempt_field: 1},
            "$unset": {"error": ""},
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _download_backup(db, job: dict, destination: Path) -> None:
    try:
        stream = GridFSBucket(db, bucket_name=GRID_BUCKET).open_download_stream(job["backup_file_id"])
    except (KeyError, NoFile) as exc:
        raise ImportFailure("The encrypted backup is no longer available. Upload it again.") from exc
    digest = hashlib.sha256()
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        while chunk := stream.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            if output.tell() > MAX_BACKUP_BYTES:
                raise ImportFailure("The encrypted backup exceeds the configured size limit.")
            if _worker_stop.is_set():
                raise ImportCancelled()
    if job.get("backup_sha256") and digest.hexdigest() != job["backup_sha256"]:
        raise ImportFailure("The encrypted backup failed its integrity check. Upload it again.")


def _run_exporter(db, job: dict, work: Path) -> Path:
    runner = (
        "import os,resource,runpy,sys;"
        "os.umask(0o077);"
        "limit=int(os.environ['WA_IMPORT_FILE_LIMIT']);"
        "resource.setrlimit(resource.RLIMIT_FSIZE,(limit,limit));"
        "resource.setrlimit(resource.RLIMIT_CPU,(600,600));"
        "resource.setrlimit(resource.RLIMIT_NOFILE,(64,64));"
        "resource.setrlimit(resource.RLIMIT_AS,(int(os.environ['WA_IMPORT_MEMORY_LIMIT']),)*2);"
        "key=open('backup.key',encoding='ascii').read().strip() if os.environ['WA_IMPORT_HEX_KEY']=='1' else 'backup.key';"
        "sys.argv=['wtsexporter','-a','-b','backup.crypt15','-k',key,'-j','result.json','--no-html','--no-banner','-o','output'];"
        "runpy.run_module('Whatsapp_Chat_Exporter',run_name='__main__')"
    )
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONUTF8": "1",
        "WA_IMPORT_HEX_KEY": "1" if job["credential_kind"] == "hex" else "0",
        "WA_IMPORT_FILE_LIMIT": str(MAX_DECRYPTED_BYTES),
        "WA_IMPORT_MEMORY_LIMIT": str(min(max(MAX_BACKUP_BYTES * 4, 1024 * 1024 * 1024), 4 * 1024 * 1024 * 1024)),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", runner],
        cwd=work,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 600
    next_lease_refresh = time.monotonic() + 30
    try:
        while process.poll() is None:
            if _worker_stop.is_set() or not _claimed(db, job, "processing"):
                raise ImportCancelled()
            if time.monotonic() >= deadline:
                raise ImportFailure("Backup processing timed out.")
            if time.monotonic() >= next_lease_refresh:
                _refresh_lease(db, job, "processing")
                next_lease_refresh = time.monotonic() + 30
            time.sleep(1)
    except BaseException:
        if process.poll() is None:
            _kill_process(process)
        raise
    result_path = work / "result.json"
    if process.returncode != 0 or not result_path.is_file():
        raise ImportFailure("The backup could not be decrypted. Check the file and encryption key.")
    if result_path.stat().st_size > MAX_DECRYPTED_BYTES:
        raise ImportFailure("The decrypted history is too large to import safely.")
    return result_path


def _kill_process(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _stage_export(db, job: dict, result_path: Path) -> dict:
    staged = db.whatsapp_history_import_messages
    staged.delete_many({"job_id": job["job_id"], "org_id": job["org_id"]})
    now = datetime.now(timezone.utc)
    expires_at = now + IMPORT_TTL
    batch: list[UpdateOne] = []
    batch_ids: set[str] = set()
    scanned = staged_count = skipped = 0
    conversations: set[str] = set()
    first_at: datetime | None = None
    last_at: datetime | None = None

    def flush() -> None:
        nonlocal batch, batch_ids, staged_count
        if batch:
            staged_count += staged.bulk_write(batch, ordered=False).upserted_count
            batch = []
            batch_ids = set()
            _refresh_lease(db, job, "processing")

    try:
        with result_path.open("rb") as handle:
            for chat_id, chat_name, source_id, message in iter_export_messages(handle):
                scanned += 1
                if scanned > MAX_MESSAGES:
                    raise ImportFailure(f"This backup exceeds the {MAX_MESSAGES:,}-message import limit.")
                row = _message_row(
                    chat_id,
                    chat_name,
                    source_id,
                    message,
                    job_id=job["job_id"],
                    org_id=job["org_id"],
                    phone_id=job["business_phone_id"],
                    business_phone=job.get("business_phone", ""),
                    expires_at=expires_at,
                    now=now,
                )
                if row is None:
                    skipped += 1
                    continue
                conversations.add(row["conversation_id"])
                first_at = row["timestamp"] if first_at is None or row["timestamp"] < first_at else first_at
                last_at = row["timestamp"] if last_at is None or row["timestamp"] > last_at else last_at
                if row["message_id"] not in batch_ids:
                    batch_ids.add(row["message_id"])
                    batch.append(UpdateOne({"job_id": job["job_id"], "message_id": row["message_id"]}, {"$setOnInsert": row}, upsert=True))
                if len(batch) == 1_000:
                    flush()
        flush()
    except (ijson.JSONError, UnicodeDecodeError) as exc:
        raise ImportFailure("The decrypted backup produced an invalid message export.") from exc
    if not staged_count:
        raise ImportFailure("No supported one-to-one messages were found in this backup.")
    return {
        "conversation_count": len(conversations),
        "message_count": staged_count,
        "skipped_count": skipped,
        "first_message_at": first_at,
        "last_message_at": last_at,
    }


def process_import(job: dict) -> None:
    from database import get_mongo_db

    db = get_mongo_db()
    with tempfile.TemporaryDirectory(prefix="wa-history-") as temp_dir:
        work = Path(temp_dir)
        os.chmod(work, 0o700)
        _download_backup(db, job, work / "backup.crypt15")
        credential = _decrypt_credential(job["encrypted_credential"])
        key_path = work / "backup.key"
        key_path.write_bytes(credential)
        os.chmod(key_path, 0o600)
        result_path = _run_exporter(db, job, work)
        preview = _stage_export(db, job, result_path)
        updated = db.whatsapp_history_imports.update_one(
            {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": "processing"},
            {
                "$set": {"status": "ready", "preview": preview, "updated_at": datetime.now(timezone.utc)},
                "$unset": {"lease_id": "", "lease_until": "", "encrypted_credential": "", "credential_kind": "", "backup_file_id": "", "error": ""},
            },
        )
        if not updated.matched_count:
            db.whatsapp_history_import_messages.delete_many({"job_id": job["job_id"], "org_id": job["org_id"]})
        delete_backup_file(db, job.get("backup_file_id"))


def _finish_processing_failure(job: dict, message: str) -> None:
    from database import get_mongo_db

    db = get_mongo_db()
    result = db.whatsapp_history_imports.update_one(
        {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": "processing"},
        {
            "$set": {"status": "failed", "error": message, "updated_at": datetime.now(timezone.utc)},
            "$unset": {"active_key": "", "lease_id": "", "lease_until": "", "encrypted_credential": "", "credential_kind": "", "backup_file_id": ""},
        },
    )
    if result.matched_count:
        cleanup_job_artifacts(db, job)


def _retry_processing(job: dict) -> None:
    from database import get_mongo_db

    db = get_mongo_db()
    if job["process_attempts"] >= 3:
        _finish_processing_failure(job, "The backup could not be processed after multiple attempts.")
        return
    db.whatsapp_history_imports.update_one(
        {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": "processing"},
        {"$set": {"status": "queued", "updated_at": datetime.now(timezone.utc)}, "$unset": {"lease_id": "", "lease_until": ""}},
    )


def _save_conversation(conversations, conversation_id: str, row: dict, job_id: str, now: datetime) -> None:
    existing = conversations.find_one({"conversation_id": conversation_id}, {"last_message_timestamp": 1})
    imported_fields = {"history_imported_at": now, "history_import_job_id": job_id}
    if existing:
        if not existing.get("last_message_timestamp") or existing["last_message_timestamp"] <= row["timestamp"]:
            imported_fields.update({"last_message": row["content"], "last_message_timestamp": row["timestamp"], "last_sender": row["role"]})
        conversations.update_one({"conversation_id": conversation_id}, {"$set": imported_fields})
        return
    conversations.update_one({"conversation_id": conversation_id}, {"$setOnInsert": {
        "conversation_id": conversation_id,
        "platform": "whatsapp",
        "customer_id": row["customer_phone"],
        "customer_name": row["customer_name"],
        "business_phone_id": row["business_phone_id"],
        "business_phone": row["business_phone"],
        "last_message": row["content"],
        "last_message_timestamp": row["timestamp"],
        "last_sender": row["role"],
        "status": "closed",
        "is_ai_enabled": False,
        "unread_count": 0,
        "message_counter": 0,
        "historical_only": True,
        "reply_window_ends_at": datetime(1970, 1, 1, tzinfo=timezone.utc),
        "created_at": now,
        **imported_fields,
    }}, upsert=True)


def commit_import(job: dict) -> None:
    """Idempotently copy staged rows without invoking any live-message side effects."""
    from database import get_mongo_db

    db = get_mongo_db()
    staged = db.whatsapp_history_import_messages
    messages = db[f"messages_{job['org_id']}"]
    conversations = db[f"conversations_{job['org_id']}"]
    _refresh_lease(db, job, "importing")
    if not any(info.get("key") == [("message_id", 1)] for info in messages.index_information().values()):
        messages.create_index("message_id")
    _refresh_lease(db, job, "importing")
    conversation_index = [("platform", 1), ("customer_id", 1), ("business_phone_id", 1)]
    if not any(info.get("key") == conversation_index for info in conversations.index_information().values()):
        conversations.create_index(conversation_index)
    _refresh_lease(db, job, "importing")
    connection = db.whatsapp_connections.find_one(
        {
            "org_id": job["org_id"],
            "$or": [{"phone_id": job["business_phone_id"]}, {"phone_number_id": job["business_phone_id"]}],
            "is_active": {"$ne": False},
        },
        {"slot": 1},
    )
    allow_legacy = (connection or {}).get("slot") == 1
    now = datetime.now(timezone.utc)
    batch: list[UpdateOne] = []
    current_staged_id = current_id = None
    latest: dict | None = None
    conversation_count = 0

    def flush_messages() -> None:
        nonlocal batch
        if batch:
            messages.bulk_write(batch, ordered=False)
            batch = []
            _refresh_lease(db, job, "importing")

    for row in staged.find({"job_id": job["job_id"], "org_id": job["org_id"]}, {"_id": 0}).sort([("conversation_id", 1), ("timestamp", 1)]):
        if row["conversation_id"] != current_staged_id:
            if latest is not None:
                _save_conversation(conversations, current_id, latest, job["job_id"], now)
            current_staged_id = row["conversation_id"]
            scope = {"platform": "whatsapp", "customer_id": row["customer_phone"], "business_phone_id": row["business_phone_id"]}
            existing = conversations.find_one(scope, {"conversation_id": 1})
            if not existing and allow_legacy:
                existing = conversations.find_one({
                    "platform": "whatsapp",
                    "customer_id": row["customer_phone"],
                    "$or": [{"business_phone_id": {"$exists": False}}, {"business_phone_id": None}],
                }, {"conversation_id": 1})
            current_id = (existing or {}).get("conversation_id") or current_staged_id
            conversation_count += 1
        row["conversation_id"] = current_id
        latest = row
        role = row["role"]
        batch.append(UpdateOne({"message_id": row["message_id"]}, {"$setOnInsert": {
            "id": row["message_id"],
            "message_id": row["message_id"],
            "platform": "whatsapp",
            "conversation_id": current_id,
            "business_phone_id": row["business_phone_id"],
            "content": row["content"],
            "type": "text",
            "payload": None,
            "sender_id": row["business_phone"] if role == "agent" else row["customer_phone"],
            "sender_name": "Historical agent" if role == "agent" else row["customer_name"],
            "role": role,
            "timestamp": row["timestamp"],
            "status": "read",
            "mode": "history",
            "context": None,
            "context_type": None,
            "source": "whatsapp_backup",
            "historical": True,
            "history_import_job_id": job["job_id"],
        }}, upsert=True))
        if len(batch) == 1_000:
            flush_messages()
    flush_messages()
    if latest is not None:
        _save_conversation(conversations, current_id, latest, job["job_id"], now)
    if latest is None:
        raise ImportFailure("The staged history expired before it could be imported.")

    completed = db.whatsapp_history_imports.update_one(
        {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": "importing"},
        {
            "$set": {
                "status": "completed",
                "result": {
                    "imported_messages": job.get("preview", {}).get("message_count", 0),
                    "imported_conversations": conversation_count,
                },
                "updated_at": now,
                "expires_at": now + COMPLETED_TTL,
            },
            "$unset": {"active_key": "", "lease_id": "", "lease_until": "", "error": ""},
        },
    )
    if completed.matched_count:
        staged.delete_many({"job_id": job["job_id"], "org_id": job["org_id"]})


def _retry_commit(job: dict, message: str) -> None:
    from database import get_mongo_db

    retrying = job["commit_attempts"] < 3
    get_mongo_db().whatsapp_history_imports.update_one(
        {"job_id": job["job_id"], "lease_id": job["lease_id"], "status": "importing"},
        {
            "$set": {
                "status": "importing" if retrying else "ready",
                "error": message if not retrying else "",
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"lease_id": "", "lease_until": ""},
        },
    )


def _recover_abandoned_jobs() -> None:
    global _last_recovery
    if time.monotonic() - _last_recovery < 60:
        return
    _last_recovery = time.monotonic()
    from database import get_mongo_db

    db = get_mongo_db()
    now = datetime.now(timezone.utc)
    for job in db.whatsapp_history_imports.find({
        "status": "processing", "process_attempts": {"$gte": 3}, "lease_until": {"$lte": now},
    }):
        _finish_processing_failure(job, "The backup could not be processed after multiple attempts.")
    db.whatsapp_history_imports.update_many(
        {"status": "importing", "commit_attempts": {"$gte": 3}, "lease_until": {"$lte": now}},
        {"$set": {"status": "ready", "error": "The import could not finish. Try again.", "updated_at": now}, "$unset": {"lease_id": "", "lease_until": ""}},
    )
    for job in db.whatsapp_history_imports.find({"status": "uploading", "updated_at": {"$lte": now - timedelta(minutes=30)}}):
        result = db.whatsapp_history_imports.update_one(
            {"job_id": job["job_id"], "status": "uploading"},
            {
                "$set": {"status": "failed", "error": "The upload was interrupted. Upload the backup again.", "updated_at": now},
                "$unset": {"active_key": "", "encrypted_credential": "", "credential_kind": "", "backup_file_id": ""},
            },
        )
        if result.matched_count:
            cleanup_job_artifacts(db, job)
    for item in db[f"{GRID_BUCKET}.files"].find({"metadata.expires_at": {"$lte": now}}, {"_id": 1}).limit(100):
        delete_backup_file(db, item["_id"])


def drain_one_history_import() -> bool:
    job = _claim(("queued", "processing"), "processing", "process_attempts")
    if job:
        try:
            process_import(job)
        except ImportCancelled:
            from database import get_mongo_db
            db = get_mongo_db()
            if db.whatsapp_history_imports.count_documents({"job_id": job["job_id"], "status": "cancelled"}, limit=1):
                cleanup_job_artifacts(db, job)
        except ImportFailure as exc:
            _finish_processing_failure(job, str(exc))
        except Exception as exc:
            logger.exception("WhatsApp history processing failed for job %s (%s)", job["job_id"], type(exc).__name__)
            _retry_processing(job)
        return True

    job = _claim(("importing",), "importing", "commit_attempts")
    if job:
        try:
            commit_import(job)
        except ImportCancelled:
            pass
        except ImportFailure as exc:
            _retry_commit(job, str(exc))
        except Exception as exc:
            logger.exception("WhatsApp history commit failed for job %s (%s)", job["job_id"], type(exc).__name__)
            _retry_commit(job, "The import could not be saved. You can try again.")
        return True
    _recover_abandoned_jobs()
    return False


async def _worker_loop() -> None:
    while not _worker_stop.is_set():
        try:
            worked = await asyncio.to_thread(drain_one_history_import)
        except Exception:
            logger.exception("WhatsApp history worker iteration failed")
            await asyncio.sleep(5)
            continue
        if not worked:
            await asyncio.sleep(2)


async def start_history_import_worker() -> None:
    global _worker_task
    await asyncio.to_thread(ensure_indexes)
    if os.getenv("WHATSAPP_HISTORY_IMPORT_WORKER_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return
    _worker_stop.clear()
    _worker_task = asyncio.create_task(_worker_loop(), name="whatsapp-history-import-worker")


async def stop_history_import_worker() -> None:
    global _worker_task
    _worker_stop.set()
    if not _worker_task:
        return
    try:
        await asyncio.wait_for(_worker_task, timeout=15)
    except TimeoutError:
        _worker_task.cancel()
        await asyncio.gather(_worker_task, return_exceptions=True)
    _worker_task = None
