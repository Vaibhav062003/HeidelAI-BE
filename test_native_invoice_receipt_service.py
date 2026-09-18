import asyncio
import os
from copy import deepcopy

from fastapi import HTTPException

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "native_invoice_receipt_test")

import services.native_invoice_service as service


class _Invoices:
    def __init__(self, document):
        self.document = document

    async def find_one(self, query, *_args, **_kwargs):
        if query.get("org_id") != self.document["org_id"] or query.get("invoice_id") != self.document["invoice_id"]:
            return None
        return deepcopy(self.document)

    async def find_one_and_update(self, query, update, **_kwargs):
        if query.get("revision") != self.document["revision"]:
            return None
        self.document.update(update["$set"])
        return deepcopy(self.document)


def test_receipt_artifact_is_revision_bound(monkeypatch) -> None:
    document = {
        "_id": "mongo-id", "org_id": "org-a", "invoice_id": "invoice-1", "invoice_number": "INV/1",
        "lifecycle": "issued", "revision": 4, "total_paise": 10_000, "amount_paid_paise": 10_000,
        "amount_refunded_paise": 0, "payments": [{"amount_paise": 10_000}], "refunds": [],
    }
    collection = _Invoices(document)
    monkeypatch.setattr(service, "invoices", collection)
    monkeypatch.setattr(service, "render_native_receipt_pdf", lambda _invoice: b"%PDF-receipt")

    async def upload(*_args, **kwargs):
        return {
            "public_id": "private-receipt", "version": 1, "byte_size": 12,
            "renderer_version": kwargs["renderer_version"], "issued_revision": kwargs["issued_revision"],
            "sha256": "a" * 64, "resource_type": "raw", "delivery_type": "private", "format": "pdf",
        }

    monkeypatch.setattr(service, "upload_native_invoice_pdf", upload)
    monkeypatch.setattr(service, "native_invoice_pdf_download_url", lambda _artifact: "https://signed.example/receipt")

    async def exercise():
        assert (await service.render_invoice_receipt("org-a", "invoice-1", "user-a"))["state"] == "ready"
        link = await service.invoice_receipt_link("org-a", "invoice-1")
        assert link["url"] == "https://signed.example/receipt"
        collection.document["revision"] += 1
        try:
            await service.invoice_receipt_link("org-a", "invoice-1")
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("A stale payment receipt remained downloadable after the invoice revision changed")

    asyncio.run(exercise())


def test_create_and_issue_invoice_runs_as_one_replay_safe_operation(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(service, "_direct_issue_operation", lambda *_args: ("request-hash", ()))

    async def preflight(*_args):
        calls.append("preflight")

    async def create(*_args, direct_issue_key="", direct_issue_hash="", **_kwargs):
        assert direct_issue_key == "request-key"
        assert direct_issue_hash == "request-hash"
        calls.append("create")
        return {"invoice_id": "invoice-1"}

    async def get(*_args):
        return {
            "invoice_id": "invoice-1", "revision": 1, "lifecycle": "draft",
            "direct_issue_idempotency_key": "request-key", "direct_issue_request_hash": "request-hash",
        }

    async def attach_and_issue(*_args):
        calls.append("issue")
        return {"invoice_id": "invoice-1", "lifecycle": "issued"}

    monkeypatch.setattr(service, "_preflight_direct_issue", preflight)
    monkeypatch.setattr(service, "create_invoice", create)
    monkeypatch.setattr(service, "get_invoice", get)
    monkeypatch.setattr(service, "_attach_and_issue", attach_and_issue)

    result = asyncio.run(service.create_and_issue_invoice(
        "org-a", "user-a", "User A", {"invoice_date": "2026-09-18"}, [], "request-key",
    ))
    assert result["lifecycle"] == "issued"
    assert calls == ["preflight", "create", "issue"]
