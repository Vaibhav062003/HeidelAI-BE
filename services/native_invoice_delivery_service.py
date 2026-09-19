"""Durable email and WhatsApp delivery for finalized native invoices."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.utils import serialize_mongo


db = get_async_mongo_db()
jobs = db.native_invoice_delivery_jobs
preferences = db.native_invoice_reminder_preferences
invoices = db.native_invoices
INDIA = ZoneInfo("Asia/Kolkata")
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
DEFAULT_DELIVERY_SETTINGS = {
    "automatic_reminders_enabled": False,
    "default_channels": [],
    "whatsapp_phone_id": "",
    "share_template_name": "",
    "reminder_template_name": "",
    "template_language": "en",
}
REMINDER_OFFSETS = (0, 3, 7)
MAX_ATTEMPTS = 3
_task: asyncio.Task | None = None


def _enabled_org_ids() -> list[str]:
    return sorted({value.strip() for value in os.getenv("NATIVE_INVOICING_ORG_IDS", "").split(",") if value.strip()})


def normalize_delivery_settings(value: dict | None) -> dict:
    settings = {**DEFAULT_DELIVERY_SETTINGS, **(value or {})}
    settings["default_channels"] = [channel for channel in ("email", "whatsapp") if channel in set(settings.get("default_channels") or [])]
    return settings


async def invoice_delivery_settings(org_id: str) -> dict:
    settings = await db[f"quote_settings_{org_id}"].find_one({"_id": "branding"}, {"invoice_delivery": 1})
    return normalize_delivery_settings((settings or {}).get("invoice_delivery"))


async def invoice_delivery_readiness(org_id: str) -> dict:
    google = await db.google_connections.find_one(
        {"org_id": org_id, "status": "active"}, {"_id": 0, "email": 1, "scopes": 1},
    )
    connections = await db.whatsapp_connections.find(
        {"org_id": org_id, "is_active": {"$ne": False}},
        {"_id": 0, "phone_id": 1, "phone": 1, "label": 1, "name": 1, "slot": 1},
    ).sort("slot", 1).to_list(length=20)
    return {
        "gmail_connected": bool(google),
        "gmail_send_ready": bool(google and GMAIL_SEND_SCOPE in set(google.get("scopes") or [])),
        "gmail_sender": (google or {}).get("email", ""),
        "whatsapp_connections": [{
            "phone_id": str(row.get("phone_id") or ""),
            "phone": row.get("phone", ""),
            "label": row.get("label") or row.get("name") or row.get("phone") or "WhatsApp",
            "slot": int(row.get("slot") or 0),
        } for row in connections if row.get("phone_id")],
    }


async def ensure_native_invoice_delivery_indexes() -> None:
    await jobs.create_index("delivery_id", unique=True)
    await jobs.create_index("operation_key", unique=True)
    await jobs.create_index([("org_id", 1), ("invoice_id", 1), ("created_at", -1)])
    await jobs.create_index([("status", 1), ("scheduled_for", 1), ("lease_until", 1)])
    await preferences.create_index([("org_id", 1), ("invoice_id", 1)], unique=True)
    await _recover_stale_jobs()


async def _recover_stale_jobs() -> None:
    now = datetime.now(timezone.utc)
    await jobs.update_many(
        {"status": "sending", "lease_until": {"$lte": now}, "provider_started_at": {"$exists": True}},
        {"$set": {"status": "delivery_unknown", "retryable": False, "updated_at": now}, "$unset": {"lease_until": ""}},
    )
    await jobs.update_many(
        {"status": "sending", "lease_until": {"$lte": now}, "provider_started_at": {"$exists": False}},
        {"$set": {"status": "pending", "scheduled_for": now, "updated_at": now}, "$unset": {"lease_until": ""}},
    )


def _fingerprint(operation: str, payload: dict) -> str:
    encoded = json.dumps({"operation": operation, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _job_view(document: dict) -> dict:
    return serialize_mongo({key: value for key, value in document.items() if key not in {"_id", "request_hash", "lease_until"}})


def _digits(value: str) -> str:
    phone = "".join(character for character in str(value or "") if character.isdigit())
    if not 7 <= len(phone) <= 15:
        raise HTTPException(status_code=422, detail="Enter a valid WhatsApp phone number including country code")
    return phone


def _email(value: str) -> str:
    try:
        return validate_email(str(value or "").strip(), check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise HTTPException(status_code=422, detail="Enter a valid customer email address") from exc


async def _invoice(org_id: str, invoice_id: str) -> dict:
    document = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    from services.native_invoice_service import invoice_view
    return invoice_view(document)


async def enqueue_manual_deliveries(
    org_id: str, invoice_id: str, actor_id: str, payload: dict, idempotency_key: str,
) -> list[dict]:
    invoice = await _invoice(org_id, invoice_id)
    if invoice.get("lifecycle") != "issued":
        raise HTTPException(status_code=409, detail="Only an issued invoice can be shared")
    kind = payload["kind"]
    if kind == "payment_reminder" and int(invoice.get("balance_paise") or 0) <= 0:
        raise HTTPException(status_code=409, detail="This invoice has no outstanding balance")
    channels = tuple(dict.fromkeys(payload["channels"]))
    recipients = {
        "email": _email(payload.get("email") or (invoice.get("customer") or {}).get("email") or "") if "email" in channels else "",
        "whatsapp": _digits(payload.get("phone") or (invoice.get("customer") or {}).get("phone") or "") if "whatsapp" in channels else "",
    }
    now = datetime.now(timezone.utc)
    created = []
    request_hash = _fingerprint("manual_invoice_delivery", {"invoice_id": invoice_id, **payload, "recipients": recipients})
    for channel in channels:
        operation_key = f"manual:{org_id}:{idempotency_key}:{channel}"
        existing = await jobs.find_one({"operation_key": operation_key})
        if existing:
            if existing.get("request_hash") != request_hash:
                raise HTTPException(status_code=409, detail="This idempotency key was already used with different delivery data")
            created.append(_job_view(existing))
            continue
        document = {
            "delivery_id": str(uuid.uuid4()), "operation_key": operation_key, "request_hash": request_hash,
            "org_id": org_id, "invoice_id": invoice_id, "kind": kind, "channel": channel,
            "recipient": recipients[channel], "automatic": False, "stage": None,
            "status": "pending", "attempts": 0, "scheduled_for": now,
            "created_at": now, "created_by": actor_id, "updated_at": now,
        }
        try:
            await jobs.insert_one(document)
        except DuplicateKeyError:
            document = await jobs.find_one({"operation_key": operation_key})
            if not document or document.get("request_hash") != request_hash:
                raise HTTPException(status_code=409, detail="This idempotency key was already used with different delivery data")
        created.append(_job_view(document))
    return created


def _reminder_time(due_on: str | date, offset: int) -> datetime:
    due = due_on if isinstance(due_on, date) else date.fromisoformat(str(due_on)[:10])
    return datetime.combine(due + timedelta(days=offset), time(hour=10), tzinfo=INDIA).astimezone(timezone.utc)


async def configure_invoice_reminders(
    org_id: str, invoice_id: str, actor_id: str, enabled: bool, channels: list[str],
) -> dict:
    invoice = await _invoice(org_id, invoice_id)
    if invoice.get("lifecycle") != "issued":
        raise HTTPException(status_code=409, detail="Payment reminders are available only for issued invoices")
    channels = [channel for channel in ("email", "whatsapp") if channel in set(channels)]
    if enabled and not channels:
        raise HTTPException(status_code=422, detail="Select at least one reminder channel")
    customer = invoice.get("customer") or {}
    if enabled and "email" in channels and not customer.get("email"):
        raise HTTPException(status_code=422, detail="Add a customer email before enabling email reminders")
    if enabled and "email" in channels:
        _email(customer.get("email") or "")
    if enabled and "whatsapp" in channels:
        _digits(customer.get("phone") or "")
    if enabled and not invoice.get("due_date"):
        raise HTTPException(status_code=422, detail="A due date is required for automatic reminders")
    if enabled:
        readiness = await invoice_delivery_readiness(org_id)
        if "email" in channels and not readiness["gmail_send_ready"]:
            raise HTTPException(status_code=409, detail="Reconnect Google and grant Gmail send permission before enabling email reminders")
        if "whatsapp" in channels:
            settings = await invoice_delivery_settings(org_id)
            active_phone_ids = {row["phone_id"] for row in readiness["whatsapp_connections"]}
            if settings["whatsapp_phone_id"] not in active_phone_ids or not settings["reminder_template_name"]:
                raise HTTPException(status_code=409, detail="Configure an active WhatsApp sender and approved reminder template first")
    now = datetime.now(timezone.utc)
    preference = await preferences.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id},
        {"$set": {"enabled": enabled, "channels": channels, "updated_at": now, "updated_by": actor_id},
         "$setOnInsert": {"created_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    await jobs.update_many(
        {"org_id": org_id, "invoice_id": invoice_id, "automatic": True, "status": {"$in": ["pending", "failed"]}},
        {"$set": {"status": "cancelled", "cancel_reason": "Reminder settings changed", "updated_at": now}},
    )
    if enabled and int(invoice.get("balance_paise") or 0) > 0:
        recipients = {"email": _email(customer.get("email") or "") if "email" in channels else "", "whatsapp": _digits(customer.get("phone") or "") if "whatsapp" in channels else ""}
        for offset in REMINDER_OFFSETS:
            scheduled_for = _reminder_time(invoice["due_date"], offset)
            if scheduled_for <= now:
                continue
            for channel in channels:
                operation_key = f"reminder:{org_id}:{invoice_id}:{offset}:{channel}"
                document = {
                    "delivery_id": str(uuid.uuid4()), "operation_key": operation_key,
                    "request_hash": _fingerprint("automatic_invoice_reminder", {"invoice_id": invoice_id, "offset": offset, "channel": channel}),
                    "org_id": org_id, "invoice_id": invoice_id, "kind": "payment_reminder", "channel": channel,
                    "recipient": recipients[channel], "automatic": True, "stage": offset,
                    "status": "pending", "attempts": 0, "scheduled_for": scheduled_for,
                    "created_at": now, "created_by": actor_id, "updated_at": now,
                }
                existing = await jobs.find_one({"operation_key": operation_key}, {"status": 1, "delivery_id": 1})
                if existing and existing.get("status") not in {"sent", "delivery_unknown", "sending"}:
                    await jobs.update_one({"_id": existing["_id"]}, {"$set": {
                        **document, "delivery_id": existing["delivery_id"],
                    }, "$unset": {"cancel_reason": "", "last_error": "", "lease_until": "", "provider_started_at": "", "retryable": ""}})
                elif not existing:
                    try:
                        await jobs.insert_one(document)
                    except DuplicateKeyError:
                        pass
    return serialize_mongo({key: value for key, value in preference.items() if key != "_id"})


async def refresh_invoice_reminders(org_id: str, invoice_id: str) -> None:
    """Reconcile future reminders after a payment, reversal, or void."""
    invoice = await _invoice(org_id, invoice_id)
    if invoice.get("lifecycle") != "issued" or int(invoice.get("balance_paise") or 0) <= 0:
        await jobs.update_many(
            {"org_id": org_id, "invoice_id": invoice_id, "automatic": True, "status": {"$in": ["pending", "failed"]}},
            {"$set": {"status": "cancelled", "cancel_reason": "Invoice no longer has an outstanding balance", "updated_at": datetime.now(timezone.utc)}},
        )
        return
    preference = await preferences.find_one({"org_id": org_id, "invoice_id": invoice_id, "enabled": True})
    if preference:
        await configure_invoice_reminders(org_id, invoice_id, "system:payment-change", True, preference.get("channels") or [])


async def list_invoice_deliveries(org_id: str, invoice_id: str) -> dict:
    await _invoice(org_id, invoice_id)
    rows = await jobs.find({"org_id": org_id, "invoice_id": invoice_id}).sort("created_at", -1).to_list(length=200)
    preference = await preferences.find_one({"org_id": org_id, "invoice_id": invoice_id})
    return {
        "deliveries": [_job_view(row) for row in rows],
        "reminder_settings": serialize_mongo({key: value for key, value in (preference or {}).items() if key != "_id"}) if preference else None,
    }


async def retry_invoice_delivery(org_id: str, invoice_id: str, delivery_id: str, actor_id: str) -> dict:
    now = datetime.now(timezone.utc)
    updated = await jobs.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id, "delivery_id": delivery_id, "status": "failed", "retryable": True},
        {"$set": {"status": "pending", "attempts": 0, "scheduled_for": now, "updated_at": now, "updated_by": actor_id},
         "$unset": {"last_error": "", "lease_until": "", "provider_started_at": ""}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This delivery cannot be retried safely")
    return _job_view(updated)


async def cancel_invoice_delivery(org_id: str, invoice_id: str, delivery_id: str, actor_id: str) -> dict:
    now = datetime.now(timezone.utc)
    updated = await jobs.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id, "delivery_id": delivery_id, "status": {"$in": ["pending", "failed"]}},
        {"$set": {"status": "cancelled", "cancel_reason": "Cancelled by user", "updated_at": now, "updated_by": actor_id}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This delivery can no longer be cancelled")
    return _job_view(updated)


async def _gmail_connection(org_id: str) -> tuple[dict, str]:
    connection = await db.google_connections.find_one({"org_id": org_id, "status": "active"})
    if not connection:
        raise HTTPException(status_code=409, detail="Connect Google before sending invoice email")
    if GMAIL_SEND_SCOPE not in set(connection.get("scopes") or []):
        raise HTTPException(status_code=409, detail="Reconnect Google and grant Gmail send permission")
    from api.google_sheets_routes import refresh_google_token
    return connection, await refresh_google_token(org_id)


async def _pdf(invoice: dict) -> tuple[str, bytes]:
    from services.native_invoice_service import invoice_pdf_link, render_invoice_pdf
    if invoice.get("pdf_state") != "ready":
        await render_invoice_pdf(invoice["org_id"], invoice["invoice_id"], "system:delivery")
        invoice = await _invoice(invoice["org_id"], invoice["invoice_id"])
    link = await invoice_pdf_link(invoice["org_id"], invoice["invoice_id"])
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(link["url"])
    if response.status_code != 200 or not response.content.startswith(b"%PDF-") or len(response.content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=503, detail="The finalized invoice PDF is unavailable")
    return link["url"], response.content


def _message_text(invoice: dict, kind: str) -> str:
    number = invoice.get("invoice_number") or "invoice"
    customer = (invoice.get("customer") or {}).get("name") or "Customer"
    paise = invoice.get("balance_paise") if kind == "payment_reminder" else invoice.get("adjusted_total_paise", invoice.get("total_paise"))
    amount = f"INR {int(paise or 0) / 100:,.2f}"
    if kind == "payment_reminder":
        return f"Hello {customer}, this is a payment reminder for invoice {number}. Outstanding amount: {amount}. Due date: {invoice.get('due_date') or '-'}."
    return f"Hello {customer}, please find invoice {number} for {amount} attached."


async def _send_email(job: dict, invoice: dict, pdf: bytes, provider_start) -> str:
    connection, token = await _gmail_connection(job["org_id"])
    message = EmailMessage()
    message["To"] = job["recipient"]
    message["From"] = connection.get("email") or "me"
    number = invoice.get("invoice_number") or "Invoice"
    message["Subject"] = f"Payment reminder: {number}" if job["kind"] == "payment_reminder" else f"Invoice {number}"
    message.set_content(_message_text(invoice, job["kind"]))
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"{str(number).replace('/', '-')}.pdf")
    await provider_start()
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"}, json={"raw": raw},
        )
    if response.status_code not in {200, 201}:
        raise HTTPException(status_code=response.status_code, detail="Gmail rejected the invoice email")
    return str(response.json().get("id") or "gmail-accepted")


async def _send_whatsapp(job: dict, invoice: dict, pdf_url: str, provider_start) -> str:
    from services.wa_service import (
        ensure_whatsapp_reply_window_open,
        resolve_outbound_whatsapp_connection,
        send_wa_media_message,
        send_whatsapp_template_message,
    )
    from services.whatsapp_connection_service import whatsapp_conversation_id

    settings = await invoice_delivery_settings(job["org_id"])
    connection = await asyncio.to_thread(
        resolve_outbound_whatsapp_connection, job["org_id"], None, settings.get("whatsapp_phone_id") or None,
    )
    if not connection:
        raise HTTPException(status_code=409, detail="Configure an active WhatsApp sender for invoice delivery")
    conversation_id = invoice.get("conversation_id") or whatsapp_conversation_id(job["recipient"], connection.get("phone_id"))
    conversation = await db[f"conversations_{job['org_id']}"].find_one({"conversation_id": conversation_id, "platform": "whatsapp"})
    use_template = bool(job.get("automatic"))
    if not use_template:
        try:
            ensure_whatsapp_reply_window_open(conversation, "reply")
        except HTTPException:
            use_template = True
    filename = f"{str(invoice.get('invoice_number') or invoice['invoice_id']).replace('/', '-')}.pdf"
    if not use_template:
        await provider_start()
        result = await send_wa_media_message(
            job["recipient"], pdf_url, "document", connection["wa_id"], _message_text(invoice, job["kind"]),
            job["org_id"], job["delivery_id"], "reply", None, None, "Billing", mime_type="application/pdf",
            file_name=filename, conversation_id=conversation_id,
        )
    else:
        template_name = settings["reminder_template_name" if job["kind"] == "payment_reminder" else "share_template_name"]
        if not template_name:
            raise HTTPException(status_code=409, detail="Configure an approved WhatsApp invoice template before sending")
        paise = invoice.get("balance_paise") if job["kind"] == "payment_reminder" else invoice.get("adjusted_total_paise", invoice.get("total_paise"))
        amount = f"INR {int(paise or 0) / 100:,.2f}"
        template = {
            "name": template_name,
            "language": {"code": settings.get("template_language") or "en"},
            "components": [
                {"type": "header", "parameters": [{"type": "document", "document": {"link": pdf_url, "filename": filename}}]},
                {"type": "body", "parameters": [
                    {"type": "text", "text": (invoice.get("customer") or {}).get("name") or "Customer"},
                    {"type": "text", "text": invoice.get("invoice_number") or "Invoice"},
                    {"type": "text", "text": amount},
                    {"type": "text", "text": str(invoice.get("due_date") or "-")},
                ]},
            ],
        }
        await provider_start()
        result = await send_whatsapp_template_message(
            job["recipient"], template, connection["wa_id"], job["org_id"], job["delivery_id"],
            "reply", None, None, "Billing", conversation_id,
        )
    return str(result.get("message_id") or "whatsapp-accepted")


async def _claim_job() -> dict | None:
    now = datetime.now(timezone.utc)
    org_ids = _enabled_org_ids()
    if not org_ids:
        return None
    return await jobs.find_one_and_update(
        {"org_id": {"$in": org_ids}, "status": {"$in": ["pending", "failed"]}, "retryable": {"$ne": False}, "attempts": {"$lt": MAX_ATTEMPTS},
         "scheduled_for": {"$lte": now}, "$or": [{"lease_until": {"$exists": False}}, {"lease_until": {"$lte": now}}]},
        {"$set": {"status": "sending", "lease_until": now + timedelta(minutes=5), "updated_at": now}, "$inc": {"attempts": 1}},
        sort=[("scheduled_for", 1)], return_document=ReturnDocument.AFTER,
    )


async def _process_job(job: dict) -> None:
    now = datetime.now(timezone.utc)
    invoice = await _invoice(job["org_id"], job["invoice_id"])
    if invoice.get("lifecycle") != "issued" or (job["kind"] == "payment_reminder" and int(invoice.get("balance_paise") or 0) <= 0):
        await jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "cancelled", "cancel_reason": "Invoice no longer requires delivery", "updated_at": now}, "$unset": {"lease_until": ""}})
        return

    async def provider_start():
        await jobs.update_one({"_id": job["_id"]}, {"$set": {"provider_started_at": datetime.now(timezone.utc)}})

    try:
        pdf_url, pdf = await _pdf(invoice)
        provider_id = await (_send_email(job, invoice, pdf, provider_start) if job["channel"] == "email" else _send_whatsapp(job, invoice, pdf_url, provider_start))
    except HTTPException as exc:
        provider_started = await jobs.find_one({"_id": job["_id"]}, {"provider_started_at": 1})
        definitive = exc.status_code < 500 and exc.status_code != 408
        status = "failed" if definitive or not (provider_started or {}).get("provider_started_at") else "delivery_unknown"
        await jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": status, "retryable": status == "failed", "last_error": str(exc.detail)[:500],
            "scheduled_for": datetime.now(timezone.utc) + timedelta(minutes=2 ** min(int(job.get("attempts") or 1), 5)),
            "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"lease_until": ""}})
        return
    except Exception as exc:
        provider_started = await jobs.find_one({"_id": job["_id"]}, {"provider_started_at": 1})
        status = "delivery_unknown" if (provider_started or {}).get("provider_started_at") else "failed"
        await jobs.update_one({"_id": job["_id"]}, {"$set": {
            "status": status, "retryable": status == "failed", "last_error": type(exc).__name__,
            "scheduled_for": datetime.now(timezone.utc) + timedelta(minutes=2 ** min(int(job.get("attempts") or 1), 5)),
            "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"lease_until": ""}})
        return
    await jobs.update_one({"_id": job["_id"]}, {"$set": {
        "status": "sent", "provider_id": provider_id, "sent_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }, "$unset": {"lease_until": "", "last_error": ""}})


async def _reconcile_one() -> bool:
    now = datetime.now(timezone.utc)
    org_ids = _enabled_org_ids()
    if not org_ids:
        return False
    invoice = await invoices.find_one_and_update(
        {"org_id": {"$in": org_ids}, "lifecycle": "issued", "reminder_schedule_state": "pending"},
        {"$set": {"reminder_schedule_state": "processing", "reminder_schedule_updated_at": now}},
        sort=[("issued_at", 1)], return_document=ReturnDocument.AFTER,
    )
    if not invoice:
        return False
    try:
        settings = await invoice_delivery_settings(invoice["org_id"])
        if settings["automatic_reminders_enabled"] and invoice.get("due_date"):
            await configure_invoice_reminders(
                invoice["org_id"], invoice["invoice_id"], "system:invoice-reminders", True, settings["default_channels"],
            )
            state = "scheduled"
        elif settings["automatic_reminders_enabled"]:
            state = "skipped_no_due_date"
        else:
            state = "disabled"
        await invoices.update_one({"_id": invoice["_id"]}, {"$set": {"reminder_schedule_state": state, "reminder_schedule_updated_at": datetime.now(timezone.utc)}})
    except Exception as exc:
        logger.warning("Invoice reminder scheduling failed invoice_id={} error={}", invoice.get("invoice_id"), type(exc).__name__)
        await invoices.update_one({"_id": invoice["_id"]}, {"$set": {"reminder_schedule_state": "failed", "reminder_schedule_error": type(exc).__name__}})
    return True


async def process_invoice_deliveries_once(limit: int = 50) -> int:
    await _recover_stale_jobs()
    for _ in range(limit):
        if not await _reconcile_one():
            break
    processed = 0
    for _ in range(limit):
        job = await _claim_job()
        if not job:
            break
        await _process_job(job)
        processed += 1
    return processed


async def _loop() -> None:
    interval = max(10, min(300, int(os.getenv("NATIVE_INVOICE_DELIVERY_INTERVAL_SECONDS", "30"))))
    while True:
        try:
            await process_invoice_deliveries_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Native invoice delivery cycle failed: {}", type(exc).__name__)
        await asyncio.sleep(interval)


async def startup_native_invoice_delivery_worker() -> None:
    global _task
    if not _enabled_org_ids() or (_task and not _task.done()):
        return
    _task = asyncio.create_task(_loop(), name="native-invoice-delivery")


async def shutdown_native_invoice_delivery_worker() -> None:
    global _task
    if not _task:
        return
    _task.cancel()
    await asyncio.gather(_task, return_exceptions=True)
    _task = None
