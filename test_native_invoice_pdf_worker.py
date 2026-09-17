import os
from datetime import timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "native_invoice_pdf_worker_test")

from services.native_invoice_pdf_worker import enabled_org_ids, retry_delay


def test_pdf_retry_backoff_is_bounded() -> None:
    assert retry_delay(1) == timedelta(minutes=1)
    assert retry_delay(2) == timedelta(minutes=2)
    assert retry_delay(5) == timedelta(minutes=16)
    assert retry_delay(99) == timedelta(minutes=60)


def test_enabled_org_ids_are_trimmed_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv("NATIVE_INVOICING_ORG_IDS", " org-b,org-a,org-b, ")
    assert enabled_org_ids() == ["org-a", "org-b"]
