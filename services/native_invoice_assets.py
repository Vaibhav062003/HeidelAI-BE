"""Strict raster-asset validation for native invoice PDFs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import warnings
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


SIGNATURE_MAX_BYTES = 96 * 1024
ATTACHMENT_MAX_BYTES = 3 * 1024 * 1024
ATTACHMENT_TOTAL_MAX_BYTES = 8 * 1024 * 1024
ATTACHMENT_MAX_COUNT = 4
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 12_000_000
_ALLOWED_MIME_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG"}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class InvoiceAssetError(ValueError):
    pass


@dataclass(frozen=True)
class InvoiceImage:
    filename: str
    content_type: str
    data: bytes
    width: int
    height: int
    sha256: str


def _filename(value: Any) -> str:
    filename = str(value or "").strip()
    if not filename or len(filename) > 120 or _CONTROL_CHARACTERS.search(filename):
        raise InvoiceAssetError("Invoice image filename is invalid")
    if PurePath(filename).name != filename or "/" in filename or "\\" in filename:
        raise InvoiceAssetError("Invoice image filename must not contain a path")
    return filename


def _asset_bytes(asset: dict, max_bytes: int) -> bytes:
    binary = asset.get("data")
    encoded = asset.get("data_base64")
    if binary is not None and encoded is not None:
        raise InvoiceAssetError("Invoice image must use exactly one data representation")
    if encoded is not None:
        if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4 + 4:
            raise InvoiceAssetError("Invoice image exceeds the size limit")
        try:
            binary = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvoiceAssetError("Invoice image is not valid base64") from exc
    if not isinstance(binary, (bytes, bytearray, memoryview)):
        raise InvoiceAssetError("Invoice image data is missing")
    data = bytes(binary)
    if not data or len(data) > max_bytes:
        raise InvoiceAssetError("Invoice image exceeds the size limit")
    declared_size = asset.get("byte_size", asset.get("size"))
    if declared_size is not None and (not isinstance(declared_size, int) or declared_size != len(data)):
        raise InvoiceAssetError("Invoice image size metadata does not match its data")
    declared_hash = asset.get("sha256")
    digest = hashlib.sha256(data).hexdigest()
    if declared_hash is not None and declared_hash != digest:
        raise InvoiceAssetError("Invoice image checksum does not match its data")
    return data


def validate_invoice_image(asset: dict, *, max_bytes: int) -> InvoiceImage:
    """Decode, verify, and metadata-strip one PNG/JPEG image."""
    if not isinstance(asset, dict):
        raise InvoiceAssetError("Invoice image metadata is invalid")
    filename = _filename(asset.get("filename"))
    content_type = str(asset.get("content_type") or "").lower()
    expected_format = _ALLOWED_MIME_TYPES.get(content_type)
    if not expected_format:
        raise InvoiceAssetError("Invoice images must be PNG or JPEG")
    data = _asset_bytes(asset, max_bytes)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as candidate:
                if candidate.format != expected_format:
                    raise InvoiceAssetError("Invoice image MIME type does not match its data")
                if getattr(candidate, "n_frames", 1) != 1:
                    raise InvoiceAssetError("Animated invoice images are not allowed")
                width, height = candidate.size
                if (
                    width < 1
                    or height < 1
                    or width > MAX_IMAGE_DIMENSION
                    or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise InvoiceAssetError("Invoice image dimensions exceed the safety limit")
                candidate.verify()
            with Image.open(io.BytesIO(data)) as decoded:
                decoded.load()
                decoded = ImageOps.exif_transpose(decoded)
                normalized = decoded.convert("RGBA" if "A" in decoded.getbands() else "RGB")
                output = io.BytesIO()
                if expected_format == "JPEG":
                    normalized.convert("RGB").save(output, format="JPEG", quality=92, optimize=False, progressive=False)
                else:
                    normalized.save(output, format="PNG", optimize=False, compress_level=9)
    except InvoiceAssetError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvoiceAssetError("Invoice image data is corrupt or unsafe") from exc
    canonical = output.getvalue()
    if len(canonical) > max_bytes:
        raise InvoiceAssetError("Normalized invoice image exceeds the size limit")
    return InvoiceImage(
        filename=filename,
        content_type=content_type,
        data=canonical,
        width=normalized.width,
        height=normalized.height,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def validate_signature_image(asset: dict | None) -> InvoiceImage | None:
    return validate_invoice_image(asset, max_bytes=SIGNATURE_MAX_BYTES) if asset else None


def validate_attachment_images(assets: list[dict] | None) -> tuple[InvoiceImage, ...]:
    if not assets:
        return ()
    if not isinstance(assets, list) or len(assets) > ATTACHMENT_MAX_COUNT:
        raise InvoiceAssetError(f"A native invoice supports at most {ATTACHMENT_MAX_COUNT} attachments")
    raw_total = 0
    validated = []
    for asset in assets:
        image = validate_invoice_image(asset, max_bytes=ATTACHMENT_MAX_BYTES)
        raw_total += len(image.data)
        if raw_total > ATTACHMENT_TOTAL_MAX_BYTES:
            raise InvoiceAssetError("Invoice attachments exceed the total size limit")
        validated.append(image)
    return tuple(validated)
