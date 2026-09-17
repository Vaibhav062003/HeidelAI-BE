"""Transactional storage for native-invoice attachment snapshots."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_client, get_async_mongo_db
from services.native_invoice_assets import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_COUNT,
    ATTACHMENT_TOTAL_MAX_BYTES,
    InvoiceAssetError,
    validate_invoice_image,
)


db = get_async_mongo_db()
attachments = db.native_invoice_attachments
invoices = db.native_invoices


async def ensure_native_invoice_asset_indexes() -> None:
    await attachments.create_index(
        [("org_id", 1), ("invoice_id", 1), ("attachment_id", 1)],
        unique=True,
        partialFilterExpression={"deleted_at": None},
    )
    await attachments.create_index(
        [("org_id", 1), ("invoice_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$type": "string"}},
    )
    await attachments.create_index(
        [("org_id", 1), ("invoice_id", 1), ("delete_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"delete_idempotency_key": {"$type": "string"}},
    )
    await attachments.create_index(
        [("org_id", 1), ("invoice_id", 1), ("deleted_at", 1), ("created_at", 1)],
    )


def attachment_metadata_view(document: dict) -> dict:
    """Return only safe attachment metadata; canonical bytes never leave this service."""
    return {
        "attachment_id": document.get("attachment_id"),
        "filename": document.get("filename"),
        "content_type": document.get("content_type"),
        "byte_size": int(document.get("byte_size") or 0),
        "sha256": document.get("sha256"),
        "created_at": document.get("created_at"),
    }


def attachment_upload_fingerprint(invoice_id: str, filename: str, content_type: str, sha256: str) -> str:
    canonical = json.dumps(
        {
            "invoice_id": invoice_id,
            "filename": filename,
            "content_type": content_type,
            "sha256": sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not 8 <= len(key) <= 200:
        raise HTTPException(status_code=422, detail="A valid idempotency key is required")
    return key


def _validate_capacity(active: list[dict], new_size: int) -> None:
    if len(active) >= ATTACHMENT_MAX_COUNT:
        raise HTTPException(status_code=422, detail=f"A native invoice supports at most {ATTACHMENT_MAX_COUNT} attachments")
    total = sum(int(document.get("byte_size") or 0) for document in active) + new_size
    if total > ATTACHMENT_TOTAL_MAX_BYTES:
        raise HTTPException(status_code=422, detail="Invoice attachments exceed the 8 MiB total limit")


def _attachment_event(action: str, actor_id: str, attachment_id: str, *, revision_before: int, revision_after: int) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "action": action,
        "actor_id": actor_id,
        "at": datetime.now(timezone.utc),
        "reason": None,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "details": {"attachment_id": attachment_id},
    }


async def _attachment_replay(org_id: str, invoice_id: str, idempotency_key: str, fingerprint: str) -> dict | None:
    existing = await attachments.find_one({
        "org_id": org_id,
        "invoice_id": invoice_id,
        "idempotency_key": idempotency_key,
    })
    if not existing:
        return None
    if existing.get("request_fingerprint") != fingerprint:
        raise HTTPException(status_code=409, detail="This idempotency key was already used with different attachment data")
    if existing.get("deleted_at"):
        raise HTTPException(status_code=409, detail="The idempotent attachment upload was subsequently deleted")
    return {
        "attachment": attachment_metadata_view(existing),
        "revision": int(existing["invoice_revision_after"]),
    }


async def _delete_replay(org_id: str, invoice_id: str, idempotency_key: str, fingerprint: str) -> dict | None:
    existing = await attachments.find_one({
        "org_id": org_id,
        "invoice_id": invoice_id,
        "delete_idempotency_key": idempotency_key,
    })
    if not existing:
        return None
    if existing.get("delete_request_fingerprint") != fingerprint:
        raise HTTPException(status_code=409, detail="This idempotency key was already used for a different attachment deletion")
    return {
        "attachment_id": existing["attachment_id"],
        "deleted": True,
        "revision": int(existing["invoice_revision_after_delete"]),
    }


async def store_invoice_attachment(
    org_id: str,
    invoice_id: str,
    actor_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
    filename: str,
    content_type: str,
    data: bytes,
) -> dict:
    """Attach one canonical raster image to an unchanged draft invoice."""
    key = _validate_idempotency_key(idempotency_key)
    if expected_revision < 1:
        raise HTTPException(status_code=422, detail="Expected revision must be positive")
    try:
        image = validate_invoice_image(
            {"filename": filename, "content_type": content_type, "byte_size": len(data), "data": data},
            max_bytes=ATTACHMENT_MAX_BYTES,
        )
    except InvoiceAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    fingerprint = attachment_upload_fingerprint(invoice_id, image.filename, image.content_type, image.sha256)
    if replay := await _attachment_replay(org_id, invoice_id, key, fingerprint):
        return replay
    attachment_id = image.sha256
    now = datetime.now(timezone.utc)
    document = {
        "org_id": org_id,
        "invoice_id": invoice_id,
        "attachment_id": attachment_id,
        "content_address": f"sha256:{image.sha256}",
        "filename": image.filename,
        "content_type": image.content_type,
        "byte_size": len(image.data),
        "sha256": image.sha256,
        "data": image.data,
        "idempotency_key": key,
        "request_fingerprint": fingerprint,
        "created_at": now,
        "created_by": actor_id,
        "deleted_at": None,
        "locked_at": None,
        "locked_revision": None,
        "invoice_revision_before": expected_revision,
        "invoice_revision_after": expected_revision + 1,
    }

    async def transaction(session):
        duplicate = await attachments.find_one({
            "org_id": org_id,
            "invoice_id": invoice_id,
            "attachment_id": attachment_id,
            "deleted_at": None,
            "locked_at": None,
        }, session=session)
        if duplicate:
            raise HTTPException(status_code=409, detail="This image is already attached to the invoice")
        active = await attachments.find(
            {"org_id": org_id, "invoice_id": invoice_id, "deleted_at": None},
            {"byte_size": 1},
            session=session,
        ).to_list(length=ATTACHMENT_MAX_COUNT + 1)
        _validate_capacity(active, len(image.data))
        updated = await invoices.find_one_and_update(
            {
                "org_id": org_id,
                "invoice_id": invoice_id,
                "lifecycle": "draft",
                "revision": expected_revision,
            },
            {
                "$inc": {"revision": 1},
                "$set": {"updated_at": now, "updated_by": actor_id},
                "$push": {"audit": _attachment_event(
                    "attachment_added", actor_id, attachment_id,
                    revision_before=expected_revision, revision_after=expected_revision + 1,
                )},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not updated:
            current = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, {"lifecycle": 1, "revision": 1}, session=session)
            if not current:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if current.get("lifecycle") != "draft":
                raise HTTPException(status_code=409, detail="Attachments can be changed only on a draft invoice")
            raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before attaching a file.")
        await attachments.insert_one(document, session=session)
        return {"attachment": attachment_metadata_view(document), "revision": int(updated["revision"])}

    client = get_async_mongo_client()
    try:
        async with await client.start_session() as session:
            return await session.with_transaction(transaction)
    except DuplicateKeyError as exc:
        replay = await _attachment_replay(org_id, invoice_id, key, fingerprint)
        if replay:
            return replay
        raise HTTPException(status_code=409, detail="This image is already attached to the invoice") from exc


async def delete_invoice_attachment(
    org_id: str,
    invoice_id: str,
    attachment_id: str,
    actor_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> dict:
    """Soft-delete an owned attachment using the invoice revision as the CAS token."""
    key = _validate_idempotency_key(idempotency_key)
    if expected_revision < 1:
        raise HTTPException(status_code=422, detail="Expected revision must be positive")
    fingerprint = hashlib.sha256(f"delete\0{invoice_id}\0{attachment_id}".encode("utf-8")).hexdigest()
    if replay := await _delete_replay(org_id, invoice_id, key, fingerprint):
        return replay
    now = datetime.now(timezone.utc)

    async def transaction(session):
        current_attachment = await attachments.find_one({
            "org_id": org_id,
            "invoice_id": invoice_id,
            "attachment_id": attachment_id,
            "deleted_at": None,
            "locked_at": None,
        }, session=session)
        if not current_attachment:
            raise HTTPException(status_code=404, detail="Invoice attachment not found")
        updated = await invoices.find_one_and_update(
            {
                "org_id": org_id,
                "invoice_id": invoice_id,
                "lifecycle": "draft",
                "revision": expected_revision,
            },
            {
                "$inc": {"revision": 1},
                "$set": {"updated_at": now, "updated_by": actor_id},
                "$push": {"audit": _attachment_event(
                    "attachment_deleted", actor_id, attachment_id,
                    revision_before=expected_revision, revision_after=expected_revision + 1,
                )},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not updated:
            current = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, {"lifecycle": 1, "revision": 1}, session=session)
            if not current:
                raise HTTPException(status_code=404, detail="Invoice not found")
            if current.get("lifecycle") != "draft":
                raise HTTPException(status_code=409, detail="Attachments can be changed only on a draft invoice")
            raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before deleting the attachment.")
        deleted = await attachments.update_one(
            {"_id": current_attachment["_id"], "deleted_at": None, "locked_at": None},
            {"$set": {
                "deleted_at": now,
                "deleted_by": actor_id,
                "delete_idempotency_key": key,
                "delete_request_fingerprint": fingerprint,
                "invoice_revision_after_delete": int(updated["revision"]),
            }},
            session=session,
        )
        if deleted.modified_count != 1:
            raise HTTPException(status_code=409, detail="The invoice attachment changed. Reload and try again.")
        return {"attachment_id": attachment_id, "deleted": True, "revision": int(updated["revision"])}

    client = get_async_mongo_client()
    try:
        async with await client.start_session() as session:
            return await session.with_transaction(transaction)
    except (DuplicateKeyError, HTTPException) as exc:
        replay = await _delete_replay(org_id, invoice_id, key, fingerprint)
        if replay:
            return replay
        raise exc


async def list_invoice_attachment_metadata(org_id: str, invoice_id: str) -> list[dict]:
    if not await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Invoice not found")
    rows = await attachments.find(
        {"org_id": org_id, "invoice_id": invoice_id, "deleted_at": None},
        {"data": 0, "idempotency_key": 0, "request_fingerprint": 0},
    ).sort("created_at", 1).to_list(length=ATTACHMENT_MAX_COUNT)
    return [attachment_metadata_view(row) for row in rows]


async def snapshot_invoice_attachments(org_id: str, invoice_id: str, issued_revision: int, session) -> list[dict]:
    """Lock the active attachment set inside the invoice-issuance transaction."""
    if issued_revision < 1 or session is None:
        raise RuntimeError("Attachment snapshot requires an issued revision and MongoDB session")
    now = datetime.now(timezone.utc)
    await attachments.update_many(
        {
            "org_id": org_id,
            "invoice_id": invoice_id,
            "deleted_at": None,
            "locked_at": None,
        },
        {"$set": {"locked_at": now, "locked_revision": issued_revision}},
        session=session,
    )
    rows = await attachments.find(
        {"org_id": org_id, "invoice_id": invoice_id, "deleted_at": None},
        {"data": 0, "idempotency_key": 0, "request_fingerprint": 0},
        session=session,
    ).sort("created_at", 1).to_list(length=ATTACHMENT_MAX_COUNT + 1)
    if any(int(row.get("locked_revision") or 0) != issued_revision for row in rows):
        raise RuntimeError("Invoice attachments were locked by a different issued revision")
    _validate_capacity(rows[:-1], int(rows[-1].get("byte_size") or 0)) if rows else None
    return [attachment_metadata_view(row) for row in rows]


async def load_invoice_attachment_images(
    org_id: str,
    invoice_id: str,
    *,
    issued_revision: int | None = None,
) -> list[dict]:
    """Load canonical bytes for an already-authorized final render; never expose via API."""
    query: dict = {"org_id": org_id, "invoice_id": invoice_id, "deleted_at": None}
    if issued_revision is not None:
        query.update({"locked_at": {"$ne": None}, "locked_revision": issued_revision})
    rows = await attachments.find(
        query,
    ).sort("created_at", 1).to_list(length=ATTACHMENT_MAX_COUNT + 1)
    if rows:
        _validate_capacity(rows[:-1], int(rows[-1].get("byte_size") or 0))
    loaded = []
    for row in rows:
        data = bytes(row.get("data") or b"")
        digest = hashlib.sha256(data).hexdigest()
        if digest != row.get("sha256") or len(data) != int(row.get("byte_size") or -1):
            raise RuntimeError("Stored native invoice attachment failed its integrity check")
        loaded.append({
            **attachment_metadata_view(row),
            "data": data,
        })
    return loaded
