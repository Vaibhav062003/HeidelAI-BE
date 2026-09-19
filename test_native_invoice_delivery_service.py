import os
import base64
from datetime import date, datetime, timezone
from email import policy
from email.parser import BytesParser

import pytest
from fastapi import HTTPException

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "native_invoice_delivery_test")

from services import native_invoice_delivery_service as delivery
from services.native_invoice_delivery_service import _digits, _email, _message_text, _reminder_time


def test_invoice_reminder_schedule_uses_ten_am_india() -> None:
    assert _reminder_time(date(2026, 9, 19), 0) == datetime(2026, 9, 19, 4, 30, tzinfo=timezone.utc)
    assert _reminder_time("2026-09-19", 7) == datetime(2026, 9, 26, 4, 30, tzinfo=timezone.utc)


def test_delivery_recipients_and_amounts_are_normalized() -> None:
    assert _email("  Accounts@Example.COM ") == "Accounts@example.com"
    assert _digits("+91 98765-43210") == "919876543210"
    invoice = {
        "invoice_number": "INV/26-27/1", "customer": {"name": "Acme"},
        "total_paise": 1_180_000, "adjusted_total_paise": 1_000_000,
        "balance_paise": 400_000, "due_date": "2026-09-19",
    }
    assert "INR 10,000.00" in _message_text(invoice, "share")
    assert "INR 4,000.00" in _message_text(invoice, "payment_reminder")
    with pytest.raises(HTTPException):
        _email("not-an-email")


@pytest.mark.asyncio
async def test_gmail_delivery_contains_the_final_pdf(monkeypatch) -> None:
    captured = {}

    async def connection(_org_id):
        return {"email": "billing@example.com"}, "token"

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "gmail-message-1"}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, _url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    started = False

    async def provider_start():
        nonlocal started
        started = True

    monkeypatch.setattr(delivery, "_gmail_connection", connection)
    monkeypatch.setattr(delivery.httpx, "AsyncClient", Client)
    provider_id = await delivery._send_email(
        {"org_id": "org-a", "recipient": "customer@example.com", "kind": "share"},
        {"invoice_number": "INV/1", "customer": {"name": "Acme"}, "adjusted_total_paise": 10_000},
        b"%PDF-1.7\ninvoice", provider_start,
    )
    raw = captured["raw"] + "=" * (-len(captured["raw"]) % 4)
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    attachment = next(message.iter_attachments())
    assert started and provider_id == "gmail-message-1"
    assert attachment.get_filename() == "INV-1.pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-1.7\ninvoice"
