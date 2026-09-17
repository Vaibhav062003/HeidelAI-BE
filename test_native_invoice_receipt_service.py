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
