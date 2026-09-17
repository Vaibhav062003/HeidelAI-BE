import hashlib
import json
import tempfile
from pathlib import Path

from scripts.native_invoice_backup import (
    FORMAT_VERSION,
    BackupError,
    collection_names,
    external_pdf_records,
    verify_external_artifacts,
)


def main() -> None:
    digest = "a" * 64
    pdf = {
        "public_id": "native_invoices/org/invoice/v2-a.pdf", "version": 7,
        "sha256": digest, "byte_size": 321, "resource_type": "raw", "delivery_type": "private",
    }
    receipt = {**pdf, "public_id": "native_invoices/org/invoice-receipt/v3-b.pdf", "version": 8, "byte_size": 123}
    records = external_pdf_records(
        {"pdf": pdf, "legacy_pdfs": [{"pdf": {**pdf, "public_id": "legacy"}}], "receipt": receipt},
        "invoice", "invoice-1",
    )
    assert [record["artifact_state"] for record in records] == ["current", "legacy", "receipt"]

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        collections = {}
        empty_sha = hashlib.sha256(b"").hexdigest()
        for name in collection_names("org"):
            (root / f"{name}.bson").write_bytes(b"")
            (root / f"{name}.indexes.bson").write_bytes(b"")
            collections[name] = {"documents": 0, "data_sha256": empty_sha, "indexes": 0, "indexes_sha256": empty_sha}
        manifest = {
            "format_version": FORMAT_VERSION,
            "org_id": "org",
            "collections": collections,
            "external_artifact_policy": {
                "provider": "cloudinary", "bytes_included": False, "provider_backup_and_retention_required": True,
            },
            "external_pdf_artifacts": [records[0], records[2]],
        }
        (root / "manifest.json").write_text(json.dumps(manifest))

        def lookup(public_id, **_kwargs):
            expected = pdf if public_id == pdf["public_id"] else receipt
            return {
                "public_id": expected["public_id"], "version": expected["version"], "bytes": expected["byte_size"],
                "resource_type": "raw", "type": "private", "context": {"custom": {"sha256": digest}},
            }

        assert verify_external_artifacts(root, resource_lookup=lookup)["org_id"] == "org"
        try:
            verify_external_artifacts(root, resource_lookup=lambda *args, **kwargs: {**lookup(*args, **kwargs), "context": {}})
        except BackupError:
            pass
        else:
            raise AssertionError("External PDF verification accepted a missing checksum")
        try:
            verify_external_artifacts(root, resource_lookup=lambda *args, **kwargs: {**lookup(*args, **kwargs), "resource_type": "image"})
        except BackupError:
            pass
        else:
            raise AssertionError("External PDF verification accepted a mismatched resource type")
        def missing_receipt(public_id, **kwargs):
            if public_id == receipt["public_id"]:
                raise FileNotFoundError(public_id)
            return lookup(public_id, **kwargs)

        try:
            verify_external_artifacts(root, resource_lookup=missing_receipt)
        except BackupError:
            pass
        else:
            raise AssertionError("External PDF verification accepted a missing receipt")


if __name__ == "__main__":
    main()
    print("Native invoice backup artifact checks passed.")
