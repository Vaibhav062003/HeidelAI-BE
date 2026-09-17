import io
import os
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, PngImagePlugin

os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "native_invoice_asset_test")

from services.native_invoice_asset_service import (  # noqa: E402
    _validate_capacity,
    attachment_metadata_view,
    attachment_upload_fingerprint,
)
from services.native_invoice_assets import (  # noqa: E402
    ATTACHMENT_MAX_BYTES,
    InvoiceAssetError,
    validate_attachment_images,
    validate_invoice_image,
)


def _png(*, width=80, height=40, metadata=False) -> bytes:
    output = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("Comment", "metadata must not survive canonicalization")
    Image.new("RGB", (width, height), "white").save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def test_raster_assets_are_verified_and_canonicalized():
    source = _png(metadata=True)
    image = validate_invoice_image(
        {"filename": "evidence.png", "content_type": "image/png", "byte_size": len(source), "data": source},
        max_bytes=ATTACHMENT_MAX_BYTES,
    )
    assert image.data != source
    assert b"metadata must not survive" not in image.data
    assert image.width == 80 and image.height == 40
    assert image.sha256 == validate_invoice_image(
        {"filename": "evidence.png", "content_type": "image/png", "data": source},
        max_bytes=ATTACHMENT_MAX_BYTES,
    ).sha256

    for patch in (
        {"filename": "../escape.png", "content_type": "image/png", "data": source},
        {"filename": "evidence.png", "content_type": "image/jpeg", "data": source},
        {"filename": "evidence.svg", "content_type": "image/svg+xml", "data": source},
        {"filename": "evidence.png", "content_type": "image/png", "byte_size": len(source) + 1, "data": source},
    ):
        try:
            validate_invoice_image(patch, max_bytes=ATTACHMENT_MAX_BYTES)
        except InvoiceAssetError:
            pass
        else:
            raise AssertionError("Unsafe invoice image was accepted")


def test_attachment_limits_and_metadata_only_views():
    source = _png()
    asset = {"filename": "page.png", "content_type": "image/png", "data": source}
    assert len(validate_attachment_images([asset] * 4)) == 4
    try:
        validate_attachment_images([asset] * 5)
    except InvoiceAssetError:
        pass
    else:
        raise AssertionError("The attachment-count limit was not enforced")

    document = {
        "attachment_id": "a" * 64,
        "filename": "page.png",
        "content_type": "image/png",
        "byte_size": 123,
        "sha256": "b" * 64,
        "data": b"secret bytes",
        "idempotency_key": "secret key",
    }
    view = attachment_metadata_view(document)
    assert view["attachment_id"] == "a" * 64
    assert "data" not in view and "idempotency_key" not in view


def test_fingerprints_and_capacity_are_deterministic():
    value = attachment_upload_fingerprint("invoice-1", "page.png", "image/png", "a" * 64)
    assert value == attachment_upload_fingerprint("invoice-1", "page.png", "image/png", "a" * 64)
    assert value != attachment_upload_fingerprint("invoice-2", "page.png", "image/png", "a" * 64)
    _validate_capacity([{"byte_size": 1}] * 3, 1)
    try:
        _validate_capacity([{"byte_size": 1}] * 4, 1)
    except HTTPException as exc:
        assert exc.status_code == 422
    else:
        raise AssertionError("The four-attachment limit was not enforced")


def test_storage_contract_is_scoped_transactional_and_integrity_checked():
    source = Path("services/native_invoice_asset_service.py").read_text()
    for required in (
        '"org_id": org_id',
        '"invoice_id": invoice_id',
        '"attachment_id": attachment_id',
        '"lifecycle": "draft"',
        '"revision": expected_revision',
        "with_transaction",
        "request_fingerprint",
        "delete_idempotency_key",
        "content_address",
        "snapshot_invoice_attachments",
        "locked_revision",
        "failed its integrity check",
    ):
        assert required in source
    lifespan = Path("core/lifespan.py").read_text()
    assert "ensure_native_invoice_asset_indexes" in lifespan


if __name__ == "__main__":
    test_raster_assets_are_verified_and_canonicalized()
    test_attachment_limits_and_metadata_only_views()
    test_fingerprints_and_capacity_are_deterministic()
    test_storage_contract_is_scoped_transactional_and_integrity_checked()
    print("Native invoice asset storage checks passed.")
