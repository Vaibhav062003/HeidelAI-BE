"""Retry finalized native-invoice PDFs after transient rendering/storage failures."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

from loguru import logger
from pymongo import ReturnDocument

from database import get_async_mongo_db


db = get_async_mongo_db()
_task: asyncio.Task | None = None
_MAX_ATTEMPTS = 5


def enabled_org_ids() -> list[str]:
    return sorted({value.strip() for value in os.getenv("NATIVE_INVOICING_ORG_IDS", "").split(",") if value.strip()})


def retry_delay(attempt: int) -> timedelta:
    return timedelta(minutes=min(60, 2 ** max(0, attempt - 1)))


async def _claim(collection, org_ids: list[str]):
    now = datetime.now(timezone.utc)
    return await collection.find_one_and_update(
        {
            "org_id": {"$in": org_ids},
            "lifecycle": {"$in": ["issued", "void"]},
            "pdf_state": {"$in": ["pending", "failed", "rendering"]},
            "$and": [
                {"$or": [{"pdf_retry_count": {"$exists": False}}, {"pdf_retry_count": {"$lt": _MAX_ATTEMPTS}}]},
                {"$or": [{"pdf_retry_after": {"$exists": False}}, {"pdf_retry_after": {"$lte": now}}]},
                {"$or": [{"pdf_lease_until": {"$exists": False}}, {"pdf_lease_until": {"$lte": now}}]},
            ],
        },
        {"$set": {"pdf_state": "rendering", "pdf_lease_until": now + timedelta(minutes=5)}},
        sort=[("updated_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def _finish(collection, document: dict, succeeded: bool) -> None:
    query = {"_id": document["_id"]}
    if succeeded:
        await collection.update_one(query, {"$unset": {
            "pdf_lease_until": "", "pdf_retry_after": "", "pdf_retry_count": "",
        }})
        return
    attempt = int(document.get("pdf_retry_count") or 0) + 1
    update = {
        "$set": {
            "pdf_state": "failed",
            "pdf_retry_count": attempt,
            "pdf_retry_after": datetime.now(timezone.utc) + retry_delay(attempt),
        },
        "$unset": {"pdf_lease_until": ""},
    }
    await collection.update_one(query, update)
    if attempt >= _MAX_ATTEMPTS:
        logger.error(
            "Native invoice PDF exhausted retries collection={} org_id={} document_id={}",
            collection.name,
            document.get("org_id"),
            document.get("invoice_id") or document.get("adjustment_id"),
        )


async def retry_pending_pdfs_once() -> int:
    from services.native_invoice_adjustment_service import render_adjustment_pdf
    from services.native_invoice_service import render_invoice_pdf

    org_ids = enabled_org_ids()
    if not org_ids:
        return 0
    completed = 0
    for collection, id_field, renderer in (
        (db.native_invoices, "invoice_id", render_invoice_pdf),
        (db.native_invoice_adjustments, "adjustment_id", render_adjustment_pdf),
    ):
        while document := await _claim(collection, org_ids):
            succeeded = False
            try:
                result = await renderer(document["org_id"], document[id_field], "system:pdf-retry")
                succeeded = result.get("state") == "ready"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Native invoice PDF retry failed collection={} document_id={} error={}",
                    collection.name,
                    document.get(id_field),
                    type(exc).__name__,
                )
            await _finish(collection, document, succeeded)
            completed += int(succeeded)
    return completed


async def _loop() -> None:
    interval = max(10, min(3600, int(os.getenv("NATIVE_INVOICE_PDF_RETRY_INTERVAL_SECONDS", "60"))))
    while True:
        try:
            await retry_pending_pdfs_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Native invoice PDF retry cycle failed: {}", type(exc).__name__)
        await asyncio.sleep(interval)


async def startup_native_invoice_pdf_worker() -> None:
    global _task
    if not enabled_org_ids() or (_task and not _task.done()):
        return
    _task = asyncio.create_task(_loop(), name="native-invoice-pdf-retry")


async def shutdown_native_invoice_pdf_worker() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
