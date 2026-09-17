"""Native INR/GST invoice persistence, arithmetic, and state transitions."""

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import re
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from PIL import Image, UnidentifiedImageError

from config.settings import CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_CLOUD_NAME
from database import get_async_mongo_client, get_async_mongo_db
from services.cloudinary_service import native_invoice_pdf_download_url, upload_native_invoice_pdf
from services.native_invoice_asset_service import load_invoice_attachment_images, snapshot_invoice_attachments
from services.native_invoice_pdf import (
    InvoicePdfError,
    RENDERER_VERSION,
    assert_invoice_text_supported,
    render_native_invoice_pdf,
    render_native_receipt_pdf,
)
from services.utils import serialize_mongo


db = get_async_mongo_db()
invoices = db.native_invoices
sequences = db.native_invoice_sequences
conversion_claims = db.invoice_conversion_claims
adjustments = db.native_invoice_adjustments
MAX_PAISE = 999_999_999_00
INDIA = ZoneInfo("Asia/Kolkata")
DEFAULT_NUMBERING = {
    "invoice": {"series": "INV", "prefix": "INV/", "suffix": "", "leading_zeros": 6},
    "credit_note": {"series": "CN", "prefix": "CN/", "suffix": "", "leading_zeros": 6},
    "debit_note": {"series": "DN", "prefix": "DN/", "suffix": "", "leading_zeros": 6},
}
EINVOICE_EXEMPTION_DECLARATION = (
    "I/We hereby declare that though our aggregate turnover in any preceding financial year from 2017-18 onwards "
    "is more than the aggregate turnover notified under sub-rule (4) of rule 48, we are not required to prepare "
    "an invoice in terms of the provisions of the said sub-rule."
)


async def ensure_native_invoice_indexes() -> None:
    if os.getenv("NATIVE_INVOICING_ORG_IDS", "").strip() and not all((CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)):
        raise RuntimeError("Native invoicing requires Cloudinary credentials for private PDF artifacts")
    if os.getenv("NATIVE_INVOICING_ORG_IDS", "").strip() or await db.tally_integrations.find_one({}, {"_id": 1}):
        hello = await db.command("hello")
        if not hello.get("setName") and hello.get("msg") != "isdbgrid":
            raise RuntimeError("Invoice finalization requires MongoDB replica-set or mongos transaction support")
    await invoices.create_index("invoice_id", unique=True)
    await invoices.create_index([("org_id", 1), ("updated_at", -1)])
    await invoices.create_index([("org_id", 1), ("archived_at", 1), ("adjusted_total_paise", -1), ("invoice_id", -1)])
    await invoices.create_index([("org_id", 1), ("lifecycle", 1), ("due_date", 1)])
    await invoices.create_index([("org_id", 1), ("lifecycle", 1), ("pdf_state", 1), ("pdf_retry_after", 1), ("updated_at", 1)])
    await invoices.create_index(
        [("org_id", 1), ("financial_year", 1), ("series", 1), ("invoice_number", 1)],
        unique=True,
        partialFilterExpression={"invoice_number": {"$type": "string"}},
    )
    await invoices.create_index(
        [("org_id", 1), ("source_quote_id", 1), ("source_quote_revision", 1)],
        unique=True,
        partialFilterExpression={"source_quote_id": {"$type": "string"}},
    )
    await invoices.create_index(
        [("org_id", 1), ("create_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"create_idempotency_key": {"$type": "string"}},
    )
    await invoices.create_index(
        [("org_id", 1), ("issue_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"issue_idempotency_key": {"$type": "string"}},
    )
    await invoices.create_index(
        [("org_id", 1), ("void_idempotency_key", 1)], unique=True,
        partialFilterExpression={"void_idempotency_key": {"$type": "string"}},
    )
    await adjustments.create_index("adjustment_id", unique=True)
    await adjustments.create_index([("org_id", 1), ("source_invoice_id", 1), ("updated_at", -1)])
    await adjustments.create_index([("org_id", 1), ("lifecycle", 1), ("pdf_state", 1), ("pdf_retry_after", 1), ("updated_at", 1)])
    await adjustments.create_index(
        [("org_id", 1), ("financial_year", 1), ("series", 1), ("note_number", 1)],
        unique=True,
        partialFilterExpression={"note_number": {"$type": "string"}},
    )
    await adjustments.create_index(
        [("org_id", 1), ("create_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"create_idempotency_key": {"$type": "string"}},
    )
    await adjustments.create_index(
        [("org_id", 1), ("issue_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"issue_idempotency_key": {"$type": "string"}},
    )
    await adjustments.create_index(
        [("org_id", 1), ("void_idempotency_key", 1)], unique=True,
        partialFilterExpression={"void_idempotency_key": {"$type": "string"}},
    )
    await invoices.update_many(
        {"adjusted_total_paise": {"$exists": False}},
        [{"$set": {"adjusted_total_paise": {"$max": [0, {"$add": [
            "$total_paise", {"$ifNull": ["$debit_total_paise", 0]},
            {"$multiply": [-1, {"$ifNull": ["$credit_total_paise", 0]}]},
        ]}]}}}],
    )


async def invoice_provider(org_id: str) -> str:
    settings = await db[f"quote_settings_{org_id}"].find_one({"_id": "branding"}, {"invoice_provider": 1})
    configured = (settings or {}).get("invoice_provider")
    if configured in {"native", "tally"}:
        return configured
    tally = await db.tally_integrations.find_one({"org_id": org_id}, {"_id": 1})
    return "tally" if tally else "native"


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _today() -> str:
    return datetime.now(INDIA).date().isoformat()


def request_fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonicalize_inline_image(value: dict | None, *, label: str, max_bytes: int, max_dimension: int) -> dict | None:
    if not value:
        return None
    try:
        raw = base64.b64decode(str(value.get("data_base64") or ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"{label} is not valid base64") from exc
    declared = int(value.get("byte_size") or 0)
    if declared != len(raw) or not raw or len(raw) > max_bytes:
        raise HTTPException(status_code=422, detail=f"{label} exceeds its size limit or byte_size does not match")
    declared_type = str(value.get("content_type") or "").lower()
    if declared_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(status_code=422, detail=f"{label} must be PNG or JPEG")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            expected_format = {"image/png": "PNG", "image/jpeg": "JPEG"}[declared_type]
            if image.format != expected_format or getattr(image, "n_frames", 1) != 1:
                raise HTTPException(status_code=422, detail=f"{label} MIME type does not match its data")
            if image.width < 1 or image.height < 1 or image.width > max_dimension or image.height > max_dimension or image.width * image.height > 4_000_000:
                raise HTTPException(status_code=422, detail=f"{label} dimensions are invalid")
            image.load()
            canonical = image.convert("RGBA")
            output = io.BytesIO()
            canonical.save(output, format="PNG", optimize=True)
            data = output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f"{label} cannot be decoded") from exc
    if len(data) > max_bytes:
        raise HTTPException(status_code=422, detail=f"Canonical {label.lower()} exceeds its size limit")
    fallback = "logo" if label == "Logo image" else "signature"
    filename = re.sub(r"[^A-Za-z0-9_.-]", "-", str(value.get("filename") or fallback))[:80]
    return {
        "filename": f"{filename.rsplit('.', 1)[0] or fallback}.png",
        "content_type": "image/png",
        "byte_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": data,
    }


def canonicalize_signature_image(value: dict | None) -> dict | None:
    return _canonicalize_inline_image(value, label="Signature image", max_bytes=96 * 1024, max_dimension=2048)


def canonicalize_logo_image(value: dict | None) -> dict | None:
    return _canonicalize_inline_image(value, label="Logo image", max_bytes=64 * 1024, max_dimension=2048)


def _idempotency_conflict() -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": "This idempotency key was already used with different data"})


def validate_invoice_date(invoice_date: str | date, today: str | date, closed_through: str | date | None = None, last_invoice_date: str | date | None = None) -> None:
    value = invoice_date if isinstance(invoice_date, date) else date.fromisoformat(str(invoice_date)[:10])
    current = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
    closed = closed_through if isinstance(closed_through, date) else date.fromisoformat(str(closed_through)[:10]) if closed_through else None
    last = last_invoice_date if isinstance(last_invoice_date, date) else date.fromisoformat(str(last_invoice_date)[:10]) if last_invoice_date else None
    if value > current:
        raise HTTPException(status_code=422, detail="Invoice date cannot be in the future")
    if closed and value <= closed:
        raise HTTPException(status_code=409, detail="The invoice date is in a closed accounting period")
    if last and value < last:
        raise HTTPException(status_code=409, detail="Invoice date cannot precede the latest issued invoice in this series")


def validate_payment_date(paid_on: str | date, invoice_date: str | date, today: str | date, closed_through: str | date | None = None) -> None:
    paid = paid_on if isinstance(paid_on, date) else date.fromisoformat(str(paid_on)[:10])
    issued = invoice_date if isinstance(invoice_date, date) else date.fromisoformat(str(invoice_date)[:10])
    current = today if isinstance(today, date) else date.fromisoformat(str(today)[:10])
    closed = closed_through if isinstance(closed_through, date) else date.fromisoformat(str(closed_through)[:10]) if closed_through else None
    if paid < issued or paid > current:
        raise HTTPException(status_code=422, detail="Payment date must be between the invoice date and today")
    if closed and paid <= closed:
        raise HTTPException(status_code=409, detail="The payment date is in a closed accounting period")


async def _closed_through(org_id: str, session=None) -> str | None:
    settings = await db[f"quote_settings_{org_id}"].find_one(
        {"_id": "branding"}, {"invoice_controls.closed_through": 1}, session=session,
    )
    return ((settings or {}).get("invoice_controls") or {}).get("closed_through")


async def configured_seller(org_id: str, session=None) -> dict:
    settings = await db[f"quote_settings_{org_id}"].find_one(
        {"_id": "branding"}, {"seller": 1}, session=session,
    )
    seller = (settings or {}).get("seller") or {}
    snapshot = {key: seller.get(key, "") for key in (
        "name", "legal_name", "trade_name", "gstin", "address", "state_code", "location", "pincode",
        "phone", "email", "payment_details",
    )}
    snapshot["country_code"] = "IN"
    if not all(snapshot.get(key) for key in ("name", "gstin", "address", "state_code")):
        raise HTTPException(status_code=409, detail="Complete the organization seller profile before creating invoices")
    return snapshot


def normalize_numbering_config(document_kind: str, value: dict | None = None) -> dict:
    if document_kind not in DEFAULT_NUMBERING:
        raise HTTPException(status_code=422, detail="Unsupported document numbering series")
    config = {**DEFAULT_NUMBERING[document_kind], **(value or {})}
    series = str(config.get("series") or "").strip().upper()
    prefix = str(config.get("prefix") or "").strip().upper()
    suffix = str(config.get("suffix") or "").strip().upper()
    zeros = int(config.get("leading_zeros") or 0)
    if not re.fullmatch(r"[A-Z0-9-]{1,12}", series):
        raise HTTPException(status_code=422, detail="Numbering series must contain 1-12 uppercase letters, digits, or hyphens")
    if not re.fullmatch(r"[A-Z0-9/.-]{0,8}", prefix) or not re.fullmatch(r"[A-Z0-9/.-]{0,4}", suffix):
        raise HTTPException(status_code=422, detail="Invoice number prefix or suffix is invalid")
    if not 1 <= zeros <= 8 or len(prefix) + 6 + zeros + len(suffix) > 16:
        raise HTTPException(status_code=422, detail="Formatted GST document numbers must be no more than 16 characters")
    return {"series": series, "prefix": prefix, "suffix": suffix, "leading_zeros": zeros}


def format_document_number(config: dict, year: str, sequence: int) -> str:
    normalized = normalize_numbering_config(str(config.get("document_kind") or "invoice"), config)
    number = f"{normalized['prefix']}{year}/{sequence:0{int(normalized['leading_zeros'])}d}{normalized['suffix']}"
    if len(number) > 16:
        raise HTTPException(status_code=409, detail="The configured GST document number exceeds 16 characters")
    return number


async def numbering_config(org_id: str, document_kind: str, session=None) -> dict:
    settings = await db[f"quote_settings_{org_id}"].find_one(
        {"_id": "branding"}, {f"invoice_controls.numbering.{document_kind}": 1}, session=session,
    )
    configured = (((settings or {}).get("invoice_controls") or {}).get("numbering") or {}).get(document_kind)
    return normalize_numbering_config(document_kind, configured)


def validate_einvoice_applicability(assessment: dict, invoice: dict, configured_seller: dict | None = None) -> None:
    status = assessment.get("status") or "unassessed"
    seller = invoice.get("seller") or {}
    seller_gstin = str(seller.get("gstin") or "").upper()
    if configured_seller is not None and any(
        str(seller.get(field) or "").strip() != str(configured_seller.get(field) or "").strip()
        for field in ("name", "gstin", "address", "state_code")
    ):
        raise HTTPException(status_code=409, detail="The seller profile changed. Save this draft again before issuing")
    if status not in {"not_required", "required"} or assessment.get("gstin") != seller_gstin:
        raise HTTPException(status_code=409, detail="Assess e-invoice applicability for this seller GSTIN before issuing")
    if status == "not_required" and assessment.get("not_required_reason") not in {"below_threshold", "notified_exemption"}:
        raise HTTPException(status_code=409, detail="Reassess why e-invoicing is not required before issuing")
    customer_registered = bool((invoice.get("customer") or {}).get("gstin"))
    if status == "required" and customer_registered:
        raise HTTPException(status_code=503, detail="This seller requires live compliance integration; issuance remains blocked until IRP submission is verified")
    if status == "required" and not customer_registered:
        if "b2c_dynamic_qr_required" not in assessment or assessment.get("b2c_dynamic_qr_required") is None:
            raise HTTPException(status_code=409, detail="Reassess whether B2C dynamic QR requirements apply before issuing")
        if assessment["b2c_dynamic_qr_required"]:
            raise HTTPException(status_code=503, detail="This B2C invoice requires a compliant dynamic QR code; issuance remains blocked until that integration is verified")


async def _validate_einvoice_applicability(org_id: str, invoice: dict, session) -> dict:
    settings = await db[f"quote_settings_{org_id}"].find_one(
        {"_id": "branding"}, {"e_invoice_applicability": 1, "seller": 1}, session=session,
    )
    assessment = (settings or {}).get("e_invoice_applicability") or {}
    validate_einvoice_applicability(assessment, invoice, (settings or {}).get("seller") or {})
    return assessment


def quote_claim_id(org_id: str, quote_id: str, quote_revision: int) -> str:
    return hashlib.sha256(f"{org_id}\0{quote_id}\0{quote_revision}".encode("utf-8")).hexdigest()


async def claim_quote_revision(
    org_id: str, quote_id: str, quote_revision: int, provider: str, resource_id: str,
    actor_id: str, session,
) -> dict:
    claim_id = quote_claim_id(org_id, quote_id, quote_revision)
    now = datetime.now(timezone.utc)
    await conversion_claims.update_one(
        {"_id": claim_id},
        {"$setOnInsert": {
            "org_id": org_id, "quote_id": quote_id, "quote_revision": quote_revision,
            "provider": provider, "resource_id": resource_id, "claimed_by": actor_id, "claimed_at": now,
        }},
        upsert=True,
        session=session,
    )
    claim = await conversion_claims.find_one({"_id": claim_id}, session=session)
    if not claim or claim.get("provider") != provider or claim.get("resource_id") != resource_id:
        owner = (claim or {}).get("provider") or "another provider"
        raise HTTPException(status_code=409, detail={"code": "invoice_source_already_claimed", "message": f"This quote revision is already claimed by {owner}"})
    return claim


def calculate_invoice_totals(payload: dict) -> dict:
    rows = []
    for source in payload.get("items") or []:
        item = deepcopy(source)
        quantity = Decimal(str(item["quantity"]))
        unit_price = int(item["unit_price_paise"])
        rate = int(item.get("gst_rate_bps") or 0)
        if quantity <= 0 or quantity > 9999 or unit_price < 0 or unit_price > MAX_PAISE or not 0 <= rate <= 10_000:
            raise HTTPException(status_code=422, detail="Invoice line quantity, price, or GST rate is invalid")
        entered = _round(quantity * unit_price)
        base = _round(Decimal(entered) * 10_000 / (10_000 + rate)) if item.get("gst_inclusive") and rate else entered
        item["quantity"] = format(quantity, "f")
        rows.append((item, entered, base, rate))
    if not rows:
        raise HTTPException(status_code=422, detail="Add at least one invoice item")

    subtotal = sum(row[2] for row in rows)
    structured_discount = payload.get("discount") or {}
    if structured_discount.get("kind") == "percentage":
        percentage_bps = int(structured_discount.get("percentage_bps") or 0)
        if not 0 <= percentage_bps <= 10_000:
            raise HTTPException(status_code=422, detail="Discount percentage must be between 0% and 100%")
        discount = _round(Decimal(subtotal) * percentage_bps / 10_000)
    elif structured_discount.get("kind") == "fixed":
        discount = max(0, int(structured_discount.get("amount_paise") or 0))
    else:
        discount = max(0, int(payload.get("discount_paise") or 0))
    if discount > subtotal:
        raise HTTPException(status_code=422, detail="Discount cannot exceed the taxable subtotal")
    discountable = [index for index, row in enumerate(rows) if row[2] > 0]
    last_discountable = discountable[-1] if discountable else -1
    allocated = 0
    intrastate = payload["seller"]["state_code"] == payload["place_of_supply_state_code"]
    cgst = sgst = igst = 0
    items = []
    for index, (item, entered, base, rate) in enumerate(rows):
        if discount and subtotal:
            remaining = discount - allocated
            line_discount = remaining if index == last_discountable else _round(Decimal(discount) * base / subtotal)
            line_discount = min(line_discount, remaining, base)
        else:
            line_discount = 0
        allocated += line_discount
        taxable = base - line_discount
        gst = _round(Decimal(taxable) * rate / 10_000)
        line_cgst = gst // 2 if intrastate else 0
        line_sgst = gst - line_cgst if intrastate else 0
        line_igst = 0 if intrastate else gst
        cgst += line_cgst
        sgst += line_sgst
        igst += line_igst
        items.append({
            **item,
            "entered_value_paise": entered,
            "taxable_before_discount_paise": base,
            "discount_paise": line_discount,
            "taxable_value_paise": taxable,
            "cgst_paise": line_cgst,
            "sgst_paise": line_sgst,
            "igst_paise": line_igst,
            "total_paise": taxable + gst,
        })
    taxable = subtotal - discount
    return {
        "items": items,
        "subtotal_paise": subtotal,
        "discount_paise": discount,
        "taxable_value_paise": taxable,
        "cgst_paise": cgst,
        "sgst_paise": sgst,
        "igst_paise": igst,
        "gst_paise": cgst + sgst + igst,
        "total_paise": taxable + cgst + sgst + igst,
        "tax_mode": "cgst_sgst" if intrastate else "igst",
    }


def financial_year(value: str | date) -> str:
    parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    start = parsed.year if parsed.month >= 4 else parsed.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def _event(
    action: str, actor_id: str, reason: str = "", *, actor_name: str = "", actor_role: str = "",
    revision_before: int | None = None, revision_after: int | None = None, details: dict | None = None,
) -> dict:
    return {
        "event_id": str(uuid.uuid4()), "action": action, "actor_id": actor_id,
        "actor_name": actor_name[:120] or None, "actor_role": actor_role[:50] or None,
        "at": datetime.now(timezone.utc), "reason": reason[:500] or None,
        "revision_before": revision_before, "revision_after": revision_after, "details": details or {},
    }


def invoice_view(document: dict) -> dict:
    value = deepcopy(document)
    value.pop("_id", None)
    value.pop("audit", None)
    value.pop("create_idempotency_key", None)
    value.pop("create_request_hash", None)
    value.pop("issue_idempotency_key", None)
    value.pop("issue_request_hash", None)
    value.pop("pdf", None)
    value.pop("receipt", None)
    value.pop("legacy_pdfs", None)
    # Signed IRP invoice/QR values are server-side compliance artifacts.  They
    # must be decrypted only in-memory for PDF rendering, never exposed by API.
    value.pop("irp_artifact", None)
    for field in tuple(value):
        if field.endswith("_idempotency_key") or field.endswith("_request_hash"):
            value.pop(field, None)
    signature = value.pop("signature_image", None)
    if signature:
        value["signature_image"] = {
            key: signature.get(key) for key in ("filename", "content_type", "byte_size", "sha256")
        }
    logo = value.pop("logo_image", None)
    if logo:
        value["logo_image"] = {
            key: logo.get(key) for key in ("filename", "content_type", "byte_size", "sha256")
        }
    payments = []
    for payment in value.get("payments") or []:
        payment = deepcopy(payment)
        for field in tuple(payment):
            if field.endswith("idempotency_key") or field.endswith("request_hash"):
                payment.pop(field, None)
        payments.append(payment)
    value["payments"] = payments
    refunds = []
    for refund in value.get("refunds") or []:
        refund = deepcopy(refund)
        for field in tuple(refund):
            if field.endswith("idempotency_key") or field.endswith("request_hash"):
                refund.pop(field, None)
        refunds.append(refund)
    value["refunds"] = refunds
    original_total = int(value.get("total_paise") or 0)
    credits = int(value.get("credit_total_paise") or 0)
    debits = int(value.get("debit_total_paise") or 0)
    adjusted_total = max(0, original_total + debits - credits)
    paid = int(value.get("amount_paid_paise") or 0)
    refunded = int(value.get("amount_refunded_paise") or 0)
    net_paid = max(0, paid - refunded)
    balance = max(0, adjusted_total - net_paid)
    refundable = max(0, net_paid - adjusted_total)
    lifecycle = value.get("lifecycle") or "draft"
    if lifecycle == "void":
        payment_state = "void"
    elif lifecycle == "issued" and balance and value.get("due_date") and str(value["due_date"])[:10] < _today():
        payment_state = "overdue"
    elif refundable:
        payment_state = "credit_balance"
    elif net_paid <= 0:
        payment_state = "unpaid"
    elif balance:
        payment_state = "partially_paid"
    else:
        payment_state = "paid"
    value["amount_paid_paise"] = paid
    value["original_total_paise"] = original_total
    value["credit_total_paise"] = credits
    value["debit_total_paise"] = debits
    value["adjusted_total_paise"] = adjusted_total
    value["amount_refunded_paise"] = refunded
    value["net_paid_paise"] = net_paid
    value["balance_paise"] = balance
    value["refundable_paise"] = refundable
    value["payment_state"] = payment_state
    value["is_archived"] = bool(value.get("archived_at"))
    value["is_closed"] = bool(value.get("closed_at"))
    value["pdf_state"] = value.get("pdf_state") or "pending"
    return serialize_mongo(value)


def invoice_amounts(document: dict) -> dict:
    original = int(document.get("total_paise") or 0)
    adjusted = max(0, original + int(document.get("debit_total_paise") or 0) - int(document.get("credit_total_paise") or 0))
    net_paid = max(0, int(document.get("amount_paid_paise") or 0) - int(document.get("amount_refunded_paise") or 0))
    return {
        "adjusted_total_paise": adjusted,
        "net_paid_paise": net_paid,
        "balance_paise": max(0, adjusted - net_paid),
        "refundable_paise": max(0, net_paid - adjusted),
    }


async def get_invoice(org_id: str, invoice_id: str) -> dict:
    document = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return document


async def create_invoice(org_id: str, actor_id: str, actor_name: str, payload: dict, idempotency_key: str, actor_role: str = "") -> dict:
    payload = deepcopy(payload)
    payload["seller"] = await configured_seller(org_id)
    payload["signature_image"] = canonicalize_signature_image(payload.get("signature_image"))
    payload["logo_image"] = canonicalize_logo_image(payload.get("logo_image"))
    request_hash = request_fingerprint("create_invoice", payload)
    existing = await invoices.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
    if existing:
        if existing.get("create_request_hash") and existing["create_request_hash"] != request_hash:
            raise _idempotency_conflict()
        return invoice_view(existing)
    validate_invoice_date(payload["invoice_date"], _today(), await _closed_through(org_id))
    if payload.get("source_quote_id"):
        quote = await db[f"quotes_{org_id}"].find_one({"org_id": org_id, "quote_id": payload["source_quote_id"]}, {"revision_number": 1, "conversation_id": 1})
        if not quote or int(quote.get("revision_number") or 1) != int(payload.get("source_quote_revision") or 0):
            raise HTTPException(status_code=409, detail="The source quote changed. Reopen the latest revision before invoicing.")
        tally_invoice = await db.tally_bill_jobs.find_one({
            "org_id": org_id,
            "quote_id": payload["source_quote_id"],
            "quote_revision": int(payload["source_quote_revision"]),
        }, {"_id": 1})
        if tally_invoice:
            raise HTTPException(status_code=409, detail="This quote revision already has a Tally invoice")
        claim = await conversion_claims.find_one({"_id": quote_claim_id(org_id, payload["source_quote_id"], int(payload["source_quote_revision"]))})
        if claim:
            raise HTTPException(status_code=409, detail={"code": "invoice_source_already_claimed", "message": f"This quote revision is already claimed by {claim.get('provider') or 'another provider'}"})
        payload["conversation_id"] = quote.get("conversation_id")
    now = datetime.now(timezone.utc)
    totals = calculate_invoice_totals(payload)
    document = {
        **payload,
        **totals,
        "org_id": org_id,
        "invoice_id": str(uuid.uuid4()),
        "invoice_number": None,
        "financial_year": None,
        "series": "INV",
        "lifecycle": "draft",
        "revision": 1,
        "pdf_state": "pending",
        "amount_paid_paise": 0,
        "amount_refunded_paise": 0,
        "credit_total_paise": 0,
        "debit_total_paise": 0,
        "adjusted_total_paise": totals["total_paise"],
        "payments": [],
        "refunds": [],
        "created_at": now,
        "created_by": actor_id,
        "created_by_name": actor_name,
        "updated_at": now,
        "updated_by": actor_id,
        "create_idempotency_key": idempotency_key,
        "create_request_hash": request_hash,
        "audit": [_event("draft_created", actor_id, actor_name=actor_name, actor_role=actor_role, revision_after=1)],
    }
    try:
        await invoices.insert_one(document)
    except DuplicateKeyError as exc:
        retry = await invoices.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
        if retry:
            if retry.get("create_request_hash") and retry["create_request_hash"] != request_hash:
                raise _idempotency_conflict() from exc
            return invoice_view(retry)
        if payload.get("source_quote_id"):
            raise HTTPException(status_code=409, detail="This quote revision already has a native invoice") from exc
        raise
    return invoice_view(document)


async def update_invoice(
    org_id: str, invoice_id: str, actor_id: str, payload: dict, expected_revision: int,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    payload = deepcopy(payload)
    current = await get_invoice(org_id, invoice_id)
    payload["seller"] = await configured_seller(org_id)
    if "signature_image" in payload and payload["signature_image"] is not None:
        payload["signature_image"] = canonicalize_signature_image(payload["signature_image"])
    if "logo_image" in payload and payload["logo_image"] is not None:
        payload["logo_image"] = canonicalize_logo_image(payload["logo_image"])
    if current.get("lifecycle") != "draft":
        raise HTTPException(status_code=409, detail="Issued invoices are immutable")
    if int(current.get("revision") or 1) != expected_revision:
        raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before saving.")
    if current.get("source_quote_id") and (
        payload.get("source_quote_id") != current.get("source_quote_id")
        or int(payload.get("source_quote_revision") or 0) != int(current.get("source_quote_revision") or 0)
    ):
        raise HTTPException(status_code=409, detail="An invoice cannot be moved to another quote revision")
    validate_invoice_date(payload["invoice_date"], _today(), await _closed_through(org_id))
    totals = calculate_invoice_totals(payload)
    now = datetime.now(timezone.utc)
    updated = await invoices.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id, "lifecycle": "draft", "revision": expected_revision},
        {"$set": {**payload, **totals, "adjusted_total_paise": totals["total_paise"], "updated_at": now, "updated_by": actor_id}, "$inc": {"revision": 1}, "$push": {"audit": _event(
            "draft_updated", actor_id, actor_name=actor_name, actor_role=actor_role,
            revision_before=expected_revision, revision_after=expected_revision + 1,
        )}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before saving.")
    return invoice_view(updated)


async def duplicate_invoice(
    org_id: str, invoice_id: str, actor_id: str, expected_revision: int, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    request_hash = request_fingerprint("duplicate_invoice", {"invoice_id": invoice_id, "expected_revision": expected_revision})
    replay = await invoices.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
    if replay:
        if replay.get("create_request_hash") != request_hash:
            raise _idempotency_conflict()
        return invoice_view(replay)
    source = await get_invoice(org_id, invoice_id)
    if int(source.get("revision") or 1) != expected_revision:
        raise HTTPException(status_code=409, detail="This invoice changed. Reload before duplicating it.")
    today = date.fromisoformat(_today())
    due_date = None
    if source.get("due_date"):
        original_date = date.fromisoformat(str(source["invoice_date"])[:10])
        original_due = date.fromisoformat(str(source["due_date"])[:10])
        due_date = (today + max(timedelta(), original_due - original_date)).isoformat()
    draft_fields = (
        "seller", "customer", "place_of_supply_state_code", "shipping_address", "shipping_state_code",
        "authorized_signatory", "reverse_charge", "currency", "supply_category", "items", "discount_paise", "discount",
        "notes", "terms", "document_references", "conversation_id", "presentation", "signature_image", "logo_image",
        "payment_profile",
    )
    now = datetime.now(timezone.utc)
    draft = {key: deepcopy(source.get(key)) for key in draft_fields if key in source}
    draft.update({
        "org_id": org_id, "invoice_id": str(uuid.uuid4()), "invoice_date": today.isoformat(), "due_date": due_date,
        "source_quote_id": None, "source_quote_revision": None, "duplicated_from_invoice_id": invoice_id,
        "invoice_number": None, "financial_year": None, "series": None, "numbering": None,
        "lifecycle": "draft", "revision": 1, "pdf_state": "pending", "amount_paid_paise": 0,
        "amount_refunded_paise": 0, "credit_total_paise": 0, "debit_total_paise": 0, "payments": [], "refunds": [],
        "created_at": now, "created_by": actor_id, "created_by_name": actor_name, "updated_at": now, "updated_by": actor_id,
        "create_idempotency_key": idempotency_key, "create_request_hash": request_hash,
        "audit": [_event("draft_duplicated", actor_id, actor_name=actor_name, actor_role=actor_role,
            revision_after=1, details={"source_invoice_id": invoice_id})],
    })
    totals = calculate_invoice_totals(draft)
    draft.update(totals)
    draft["adjusted_total_paise"] = totals["total_paise"]
    try:
        await invoices.insert_one(draft)
    except DuplicateKeyError as exc:
        replay = await invoices.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
        if replay and replay.get("create_request_hash") == request_hash:
            return invoice_view(replay)
        raise _idempotency_conflict() from exc
    return invoice_view(draft)


async def set_invoice_archived(
    org_id: str, invoice_id: str, actor_id: str, expected_revision: int, archived: bool, idempotency_key: str,
    reason: str = "", actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_invoice(org_id, invoice_id)
    operation = "archive_invoice" if archived else "unarchive_invoice"
    key_field = f"{operation}_idempotency_key"
    hash_field = f"{operation}_request_hash"
    request_hash = request_fingerprint(operation, {"invoice_id": invoice_id, "expected_revision": expected_revision, "reason": reason})
    if bool(current.get("archived_at")) == archived:
        if current.get(key_field) == idempotency_key and current.get(hash_field) != request_hash:
            raise _idempotency_conflict()
        return invoice_view(current)
    now = datetime.now(timezone.utc)
    set_values = {
        key_field: idempotency_key, hash_field: request_hash, "updated_at": now, "updated_by": actor_id,
        "archived_at": now if archived else None, "archived_by": actor_id if archived else None,
    }
    updated = await invoices.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id, "revision": expected_revision},
        {"$set": set_values, "$inc": {"revision": 1}, "$push": {"audit": _event(
            "archived" if archived else "unarchived", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
            revision_before=expected_revision, revision_after=expected_revision + 1,
        )}}, return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice changed. Reload before changing its archive state.")
    return invoice_view(updated)


async def set_invoice_closed(
    org_id: str, invoice_id: str, actor_id: str, expected_revision: int, closed: bool, idempotency_key: str,
    reason: str, actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_invoice(org_id, invoice_id)
    operation = "close_invoice" if closed else "reopen_invoice"
    key_field = f"{operation}_idempotency_key"
    hash_field = f"{operation}_request_hash"
    request_hash = request_fingerprint(operation, {"invoice_id": invoice_id, "expected_revision": expected_revision, "reason": reason})
    if bool(current.get("closed_at")) == closed:
        if current.get(key_field) == idempotency_key and current.get(hash_field) != request_hash:
            raise _idempotency_conflict()
        return invoice_view(current)
    if closed:
        if current.get("lifecycle") not in {"issued", "void"}:
            raise HTTPException(status_code=409, detail="Only issued or void invoices can be closed")
        amounts = invoice_amounts(current)
        if amounts["balance_paise"] or amounts["refundable_paise"]:
            raise HTTPException(status_code=409, detail="Settle the invoice balance or refund balance before closing")
    now = datetime.now(timezone.utc)
    updated = await invoices.find_one_and_update(
        {"org_id": org_id, "invoice_id": invoice_id, "revision": expected_revision},
        {"$set": {
            key_field: idempotency_key, hash_field: request_hash, "closed_at": now if closed else None,
            "closed_by": actor_id if closed else None, "close_reason": reason if closed else None,
            "updated_at": now, "updated_by": actor_id,
        }, "$inc": {"revision": 1}, "$push": {"audit": _event(
            "closed" if closed else "reopened", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
            revision_before=expected_revision, revision_after=expected_revision + 1,
        )}}, return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice changed. Reload before changing its close state.")
    return invoice_view(updated)


async def list_invoices(
    org_id: str, *, search: str = "", lifecycle: str = "", payment_state: str = "",
    archived: str = "active", limit: int = 100, cursor: str = "", sort: str = "updated_at",
    direction: str = "desc",
) -> dict:
    query: dict = {"org_id": org_id}
    clauses = []
    if lifecycle:
        query["lifecycle"] = lifecycle
    if archived == "active":
        query["archived_at"] = None
    elif archived == "archived":
        query["archived_at"] = {"$ne": None}
    if search:
        escaped = re.escape(search[:100])
        clauses.append({"$or": [{"invoice_number": {"$regex": escaped, "$options": "i"}}, {"customer.name": {"$regex": escaped, "$options": "i"}}, {"customer.gstin": {"$regex": escaped, "$options": "i"}}]})
    if payment_state:
        query["lifecycle"] = "issued"
    adjusted = {"$add": ["$total_paise", {"$ifNull": ["$debit_total_paise", 0]}, {"$multiply": [-1, {"$ifNull": ["$credit_total_paise", 0]}]}]}
    net_paid = {"$subtract": [{"$ifNull": ["$amount_paid_paise", 0]}, {"$ifNull": ["$amount_refunded_paise", 0]}]}
    if payment_state == "paid":
        query["$expr"] = {"$eq": [net_paid, adjusted]}
    elif payment_state == "partially_paid":
        query["$expr"] = {"$and": [{"$gt": [net_paid, 0]}, {"$lt": [net_paid, adjusted]}]}
    elif payment_state == "unpaid":
        query["$expr"] = {"$lte": [net_paid, 0]}
        clauses.append({"$or": [{"due_date": None}, {"due_date": {"$gte": _today()}}]})
    elif payment_state == "overdue":
        query["due_date"] = {"$lt": _today()}
        query["$expr"] = {"$lt": [net_paid, adjusted]}
    elif payment_state == "credit_balance":
        query["$expr"] = {"$gt": [net_paid, adjusted]}
    if clauses:
        query["$and"] = clauses
    total = await invoices.count_documents(query)
    sort_fields = {
        "updated_at": "updated_at", "invoice_date": "invoice_date", "number": "invoice_number",
        "customer": "customer.name", "total": "adjusted_total_paise",
    }
    sort_field = sort_fields.get(sort, "updated_at")
    sort_direction = 1 if direction == "asc" else -1
    if cursor:
        try:
            padding = "=" * (-len(cursor) % 4)
            marker = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
            marker_value = marker["value"]
            marker_id = str(marker["invoice_id"])
            if sort_field == "updated_at" and marker_value:
                marker_value = datetime.fromisoformat(str(marker_value).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Invoice cursor is invalid") from exc
        comparison = "$gt" if sort_direction == 1 else "$lt"
        cursor_clause = {"$or": [
            {sort_field: {comparison: marker_value}},
            {sort_field: marker_value, "invoice_id": {comparison: marker_id}},
        ]}
        query.setdefault("$and", []).append(cursor_clause)
    rows = await invoices.find(query).sort([(sort_field, sort_direction), ("invoice_id", sort_direction)]).to_list(length=limit + 1)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        value = rows[-1]
        for part in sort_field.split("."):
            value = (value or {}).get(part) if isinstance(value, dict) else None
        marker = json.dumps({"value": value, "invoice_id": rows[-1]["invoice_id"]}, default=str, separators=(",", ":"))
        next_cursor = base64.urlsafe_b64encode(marker.encode("utf-8")).decode("ascii").rstrip("=")
    return {"invoices": [invoice_view(row) for row in rows], "total": total, "next_cursor": next_cursor}


async def invoice_summary(org_id: str) -> dict:
    today = _today()
    rows = await invoices.aggregate([
        {"$match": {"org_id": org_id, "lifecycle": "issued", "archived_at": None}},
        {"$project": {
            "total": {"$max": [0, {"$add": ["$total_paise", {"$ifNull": ["$debit_total_paise", 0]}, {"$multiply": [-1, {"$ifNull": ["$credit_total_paise", 0]}]}]}]},
            "net_paid": {"$max": [0, {"$subtract": [{"$ifNull": ["$amount_paid_paise", 0]}, {"$ifNull": ["$amount_refunded_paise", 0]}]}]},
            "due_date": 1,
        }},
        {"$project": {"total": 1, "balance": {"$max": [0, {"$subtract": ["$total", "$net_paid"]}]}, "due_date": 1}},
        {"$group": {"_id": None, "issued_count": {"$sum": 1}, "outstanding_count": {"$sum": {"$cond": [{"$gt": ["$balance", 0]}, 1, 0]}}, "overdue_count": {"$sum": {"$cond": [{"$and": [{"$gt": ["$balance", 0]}, {"$ne": ["$due_date", None]}, {"$lt": ["$due_date", today]}]}, 1, 0]}}, "issued_total_paise": {"$sum": "$total"}, "outstanding_total_paise": {"$sum": "$balance"}, "overdue_total_paise": {"$sum": {"$cond": [{"$and": [{"$gt": ["$balance", 0]}, {"$ne": ["$due_date", None]}, {"$lt": ["$due_date", today]}]}, "$balance", 0]}}}},
        {"$project": {"_id": 0}},
    ]).to_list(length=1)
    return rows[0] if rows else {"issued_count": 0, "outstanding_count": 0, "overdue_count": 0, "issued_total_paise": 0, "outstanding_total_paise": 0, "overdue_total_paise": 0}


def reporting_period(from_date: str | date | None = None, to_date: str | date | None = None) -> tuple[str, str]:
    today = date.fromisoformat(_today())
    default_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    start = from_date if isinstance(from_date, date) else date.fromisoformat(str(from_date)[:10]) if from_date else default_start
    end = to_date if isinstance(to_date, date) else date.fromisoformat(str(to_date)[:10]) if to_date else today
    if start > end:
        raise HTTPException(status_code=422, detail="Report start date cannot be after end date")
    if end > today:
        raise HTTPException(status_code=422, detail="Report end date cannot be in the future")
    if (end - start).days > 731:
        raise HTTPException(status_code=422, detail="Report range cannot exceed two years")
    return start.isoformat(), end.isoformat()


async def invoice_report(org_id: str, from_date: str | date | None = None, to_date: str | date | None = None) -> dict:
    start, end = reporting_period(from_date, to_date)
    adjusted = {"$max": [0, {"$add": [
        "$total_paise", {"$ifNull": ["$debit_total_paise", 0]},
        {"$multiply": [-1, {"$ifNull": ["$credit_total_paise", 0]}]},
    ]}]}
    net_paid = {"$max": [0, {"$subtract": [
        {"$ifNull": ["$amount_paid_paise", 0]}, {"$ifNull": ["$amount_refunded_paise", 0]},
    ]}]}
    rows = await invoices.aggregate([
        {"$match": {"org_id": org_id, "lifecycle": "issued", "invoice_date": {"$gte": start, "$lte": end}}},
        {"$project": {
            "customer": 1, "invoice_date": 1, "due_date": 1, "total_paise": 1,
            "credit_total_paise": {"$ifNull": ["$credit_total_paise", 0]},
            "debit_total_paise": {"$ifNull": ["$debit_total_paise", 0]},
            "amount_refunded_paise": {"$ifNull": ["$amount_refunded_paise", 0]},
            "adjusted": adjusted, "net_paid": net_paid,
        }},
        {"$project": {
            "customer": 1, "invoice_date": 1, "due_date": 1, "total_paise": 1,
            "credit_total_paise": 1, "debit_total_paise": 1, "amount_refunded_paise": 1,
            "adjusted": 1, "net_paid": 1,
            "balance": {"$max": [0, {"$subtract": ["$adjusted", "$net_paid"]}]},
            "month": {"$substrBytes": ["$invoice_date", 0, 7]},
        }},
        {"$facet": {
            "totals": [{"$group": {
                "_id": None,
                "invoice_count": {"$sum": 1},
                "invoice_total_paise": {"$sum": "$total_paise"},
                "credit_total_paise": {"$sum": "$credit_total_paise"},
                "debit_total_paise": {"$sum": "$debit_total_paise"},
                "adjusted_total_paise": {"$sum": "$adjusted"},
                "net_received_paise": {"$sum": "$net_paid"},
                "refunded_paise": {"$sum": "$amount_refunded_paise"},
                "outstanding_paise": {"$sum": "$balance"},
                "overdue_paise": {"$sum": {"$cond": [
                    {"$and": [{"$gt": ["$balance", 0]}, {"$ne": ["$due_date", None]}, {"$lt": ["$due_date", _today()]}]},
                    "$balance", 0,
                ]}},
            }}],
            "top_customers": [
                {"$group": {
                    "_id": {"name": "$customer.name", "gstin": {"$ifNull": ["$customer.gstin", ""]}},
                    "invoice_count": {"$sum": 1},
                    "adjusted_total_paise": {"$sum": "$adjusted"},
                    "outstanding_paise": {"$sum": "$balance"},
                }},
                {"$sort": {"adjusted_total_paise": -1, "_id.name": 1}},
                {"$limit": 10},
                {"$project": {"_id": 0, "name": "$_id.name", "gstin": "$_id.gstin", "invoice_count": 1, "adjusted_total_paise": 1, "outstanding_paise": 1}},
            ],
            "monthly": [
                {"$group": {"_id": "$month", "invoice_count": {"$sum": 1}, "adjusted_total_paise": {"$sum": "$adjusted"}, "net_received_paise": {"$sum": "$net_paid"}}},
                {"$sort": {"_id": 1}},
                {"$project": {"_id": 0, "month": "$_id", "invoice_count": 1, "adjusted_total_paise": 1, "net_received_paise": 1}},
            ],
        }},
    ]).to_list(length=1)
    result = rows[0] if rows else {}
    totals = (result.get("totals") or [{}])[0]
    return serialize_mongo({
        "from_date": start,
        "to_date": end,
        "invoice_count": int(totals.get("invoice_count") or 0),
        "invoice_total_paise": int(totals.get("invoice_total_paise") or 0),
        "credit_total_paise": int(totals.get("credit_total_paise") or 0),
        "debit_total_paise": int(totals.get("debit_total_paise") or 0),
        "adjusted_total_paise": int(totals.get("adjusted_total_paise") or 0),
        "net_received_paise": int(totals.get("net_received_paise") or 0),
        "receipt_basis": "lifetime_net_receipts_for_invoices_in_period",
        "refunded_paise": int(totals.get("refunded_paise") or 0),
        "outstanding_paise": int(totals.get("outstanding_paise") or 0),
        "overdue_paise": int(totals.get("overdue_paise") or 0),
        "top_customers": result.get("top_customers") or [],
        "monthly": result.get("monthly") or [],
    })


def csv_cell(value) -> str:
    text = str(value if value is not None else "").replace("\x00", "")
    return f"'{text}" if text.lstrip().startswith(("=", "+", "-", "@")) else text


async def export_invoices_csv(
    org_id: str, from_date: str | date | None = None, to_date: str | date | None = None,
) -> tuple[bytes, str]:
    start, end = reporting_period(from_date, to_date)
    rows = await invoices.find(
        {"org_id": org_id, "invoice_date": {"$gte": start, "$lte": end}},
        {"audit": 0, "pdf": 0, "legacy_pdfs": 0, "signature_image": 0, "irp_artifact": 0},
    ).sort([("invoice_date", 1), ("invoice_id", 1)]).to_list(length=10_001)
    if len(rows) > 10_000:
        raise HTTPException(status_code=422, detail="CSV export exceeds 10,000 invoices; choose a narrower date range")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "invoice_number", "invoice_date", "due_date", "lifecycle", "payment_state", "customer_name", "customer_gstin",
        "taxable_value_inr", "cgst_inr", "sgst_inr", "igst_inr", "original_total_inr", "credit_notes_inr",
        "debit_notes_inr", "adjusted_total_inr", "net_paid_inr", "balance_inr", "archived", "closed",
    ])
    for document in rows:
        view = invoice_view(document)
        money = lambda paise: f"{int(paise or 0) / 100:.2f}"
        writer.writerow([csv_cell(value) for value in (
            view.get("invoice_number") or "", view.get("invoice_date") or "", view.get("due_date") or "",
            view.get("lifecycle") or "", view.get("payment_state") or "", (view.get("customer") or {}).get("name") or "",
            (view.get("customer") or {}).get("gstin") or "", money(view.get("taxable_value_paise")), money(view.get("cgst_paise")),
            money(view.get("sgst_paise")), money(view.get("igst_paise")), money(view.get("original_total_paise")),
            money(view.get("credit_total_paise")), money(view.get("debit_total_paise")), money(view.get("adjusted_total_paise")),
            money(view.get("net_paid_paise")), money(view.get("balance_paise")), "yes" if view.get("is_archived") else "no",
            "yes" if view.get("is_closed") else "no",
        )])
    filename = f"native-invoices-{start}-to-{end}.csv"
    return ("\ufeff" + output.getvalue()).encode("utf-8"), filename


async def _export_invoice_views(
    org_id: str, from_date: str | date | None = None, to_date: str | date | None = None,
) -> tuple[list[dict], str, str]:
    start, end = reporting_period(from_date, to_date)
    rows = await invoices.find(
        {"org_id": org_id, "invoice_date": {"$gte": start, "$lte": end}},
        {"audit": 0, "pdf": 0, "legacy_pdfs": 0, "irp_artifact": 0},
    ).sort([("invoice_date", 1), ("invoice_id", 1)]).to_list(length=10_001)
    if len(rows) > 10_000:
        raise HTTPException(status_code=422, detail="Export exceeds 10,000 invoices; choose a narrower date range")
    return [invoice_view(row) for row in rows], start, end


async def export_invoices_json(
    org_id: str, from_date: str | date | None = None, to_date: str | date | None = None,
) -> tuple[bytes, str]:
    rows, start, end = await _export_invoice_views(org_id, from_date, to_date)
    payload = {"schema_version": 1, "from_date": start, "to_date": end, "invoices": rows}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), f"native-invoices-{start}-to-{end}.json"


async def export_invoices_xlsx(
    org_id: str, from_date: str | date | None = None, to_date: str | date | None = None,
) -> tuple[bytes, str]:
    from openpyxl import Workbook

    rows, start, end = await _export_invoice_views(org_id, from_date, to_date)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Invoices")
    headers = (
        "invoice_number", "invoice_date", "due_date", "lifecycle", "payment_state", "customer_name",
        "customer_gstin", "taxable_value_inr", "cgst_inr", "sgst_inr", "igst_inr", "original_total_inr",
        "credit_notes_inr", "debit_notes_inr", "adjusted_total_inr", "net_paid_inr", "balance_inr",
        "archived", "closed",
    )
    sheet.append(headers)
    for view in rows:
        money = lambda paise: int(paise or 0) / 100
        sheet.append(tuple(csv_cell(value) if isinstance(value, str) else value for value in (
            view.get("invoice_number") or "", view.get("invoice_date") or "", view.get("due_date") or "",
            view.get("lifecycle") or "", view.get("payment_state") or "", (view.get("customer") or {}).get("name") or "",
            (view.get("customer") or {}).get("gstin") or "", money(view.get("taxable_value_paise")), money(view.get("cgst_paise")),
            money(view.get("sgst_paise")), money(view.get("igst_paise")), money(view.get("original_total_paise")),
            money(view.get("credit_total_paise")), money(view.get("debit_total_paise")), money(view.get("adjusted_total_paise")),
            money(view.get("net_paid_paise")), money(view.get("balance_paise")), "yes" if view.get("is_archived") else "no",
            "yes" if view.get("is_closed") else "no",
        )))
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue(), f"native-invoices-{start}-to-{end}.xlsx"


async def render_invoice_preview(
    org_id: str, payload: dict, *, invoice_id: str = "", expected_revision: int | None = None,
) -> bytes:
    values: dict = {}
    if invoice_id:
        current = await get_invoice(org_id, invoice_id)
        if current.get("lifecycle") != "draft":
            raise HTTPException(status_code=409, detail="Only a draft invoice can be previewed")
        if expected_revision is None or int(current.get("revision") or 0) != expected_revision:
            raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before previewing.")
        values = deepcopy(current)
        values["attachments"] = await load_invoice_attachment_images(org_id, invoice_id)
    elif expected_revision is not None:
        raise HTTPException(status_code=422, detail="expected_revision requires invoice_id")
    values.update(deepcopy(payload))
    values["seller"] = await configured_seller(org_id)
    if "signature_image" in payload:
        values["signature_image"] = canonicalize_signature_image(payload.get("signature_image"))
    if "logo_image" in payload:
        values["logo_image"] = canonicalize_logo_image(payload.get("logo_image"))
    values.update(calculate_invoice_totals(values))
    values.update({"lifecycle": "draft", "invoice_number": None, "issued_at": None})
    try:
        return await asyncio.to_thread(render_native_invoice_pdf, values, final=False)
    except InvoicePdfError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def invoice_audit(org_id: str, invoice_id: str, limit: int = 200) -> list[dict]:
    document = await get_invoice(org_id, invoice_id)
    events = list(document.get("audit") or [])[-limit:]
    events.reverse()
    allowed = (
        "event_id", "action", "actor_id", "actor_name", "actor_role", "at", "reason",
        "revision_before", "revision_after",
    )
    return serialize_mongo([{key: event.get(key) for key in allowed} for event in events])


async def issue_invoice(
    org_id: str, invoice_id: str, actor_id: str, expected_revision: int, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    client = get_async_mongo_client()
    request_hash = request_fingerprint("issue_invoice", {"invoice_id": invoice_id, "expected_revision": expected_revision})

    async def transaction(session):
        current = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, session=session)
        if not current:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if current.get("lifecycle") == "issued" and current.get("issue_idempotency_key") == idempotency_key:
            if current.get("issue_request_hash") and current["issue_request_hash"] != request_hash:
                raise _idempotency_conflict()
            return current
        if current.get("lifecycle") != "draft":
            raise HTTPException(status_code=409, detail="Only a draft invoice can be issued")
        if int(current.get("revision") or 1) != expected_revision:
            raise HTTPException(status_code=409, detail="This invoice draft changed. Reload before issuing.")
        if not current.get("signature_image"):
            raise HTTPException(status_code=409, detail="Add an authorised signature before issuing this invoice")
        assessment = await _validate_einvoice_applicability(org_id, current, session)
        try:
            await asyncio.to_thread(assert_invoice_text_supported, current)
        except InvoicePdfError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        key_owner = await invoices.find_one({"org_id": org_id, "issue_idempotency_key": idempotency_key}, {"_id": 1}, session=session)
        if key_owner and key_owner.get("_id") != current.get("_id"):
            raise _idempotency_conflict()
        if current.get("source_quote_id"):
            quote_revision = int(current.get("source_quote_revision") or 1)
            quote = await db[f"quotes_{org_id}"].find_one(
                {"org_id": org_id, "quote_id": current["source_quote_id"]}, {"revision_number": 1}, session=session,
            )
            if not quote or int(quote.get("revision_number") or 1) != quote_revision:
                raise HTTPException(status_code=409, detail="The source quote changed. Reopen the latest revision before invoicing.")
            if await db.tally_bill_jobs.find_one({
                "org_id": org_id, "quote_id": current["source_quote_id"], "quote_revision": quote_revision,
            }, {"_id": 1}, session=session):
                raise HTTPException(status_code=409, detail="This quote revision already has a Tally invoice")
            await claim_quote_revision(
                org_id, current["source_quote_id"], quote_revision, "native", invoice_id, actor_id, session,
            )
        year = financial_year(current["invoice_date"])
        numbering = await numbering_config(org_id, "invoice", session)
        sequence_id = f"{org_id}:{year}:invoice:{numbering['series']}"
        previous_sequence = await sequences.find_one({"_id": sequence_id}, {"last_invoice_date": 1}, session=session)
        validate_invoice_date(
            current["invoice_date"], _today(), await _closed_through(org_id, session),
            (previous_sequence or {}).get("last_invoice_date"),
        )
        sequence = await sequences.find_one_and_update(
            {"_id": sequence_id},
            {"$inc": {"value": 1}, "$set": {"last_invoice_date": str(current["invoice_date"])[:10]}, "$setOnInsert": {
                "org_id": org_id, "financial_year": year, "document_kind": "invoice", "series": numbering["series"],
            }},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        value = int(sequence["value"])
        if value > 999_999_999_999:
            raise HTTPException(status_code=409, detail="The invoice series is exhausted for this financial year")
        now = datetime.now(timezone.utc)
        number = format_document_number({**numbering, "document_kind": "invoice"}, year, value)
        attachment_snapshot = await snapshot_invoice_attachments(org_id, invoice_id, expected_revision + 1, session)
        updated = await invoices.find_one_and_update(
            {"_id": current["_id"], "lifecycle": "draft", "revision": expected_revision},
            {"$set": {"lifecycle": "issued", "invoice_number": number, "financial_year": year,
                "series": numbering["series"], "numbering": numbering, "attachments": attachment_snapshot,
                "einvoice_exemption_declaration": EINVOICE_EXEMPTION_DECLARATION if assessment.get("not_required_reason") == "notified_exemption" else None,
                "issued_at": now, "issued_by": actor_id, "issue_idempotency_key": idempotency_key, "issue_request_hash": request_hash, "pdf_state": "pending", "pdf_renderer_version": RENDERER_VERSION, "issued_revision": expected_revision + 1, "updated_at": now, "updated_by": actor_id}, "$inc": {"revision": 1}, "$push": {"audit": _event(
                "issued", actor_id, actor_name=actor_name, actor_role=actor_role,
                revision_before=expected_revision, revision_after=expected_revision + 1,
                details={"invoice_number": number, "financial_year": year},
            )}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="This invoice changed while it was being issued")
        return updated

    try:
        async with await client.start_session() as session:
            document = await session.with_transaction(transaction)
    except DuplicateKeyError as exc:
        current = await get_invoice(org_id, invoice_id)
        if current.get("source_quote_id"):
            claim = await conversion_claims.find_one({
                "_id": quote_claim_id(org_id, current["source_quote_id"], int(current.get("source_quote_revision") or 1)),
            })
            if claim and (claim.get("provider") != "native" or claim.get("resource_id") != invoice_id):
                raise HTTPException(status_code=409, detail={
                    "code": "invoice_source_already_claimed",
                    "message": f"This quote revision is already claimed by {claim.get('provider') or 'another provider'}",
                }) from exc
        key_owner = await invoices.find_one({"org_id": org_id, "issue_idempotency_key": idempotency_key}, {"invoice_id": 1})
        if key_owner and key_owner.get("invoice_id") != invoice_id:
            raise _idempotency_conflict() from exc
        raise
    try:
        await render_invoice_pdf(org_id, invoice_id, actor_id)
    except Exception:
        pass
    document = await get_invoice(org_id, invoice_id)
    return invoice_view(document)


async def record_payment(
    org_id: str, invoice_id: str, actor_id: str, payload: dict, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_invoice(org_id, invoice_id)
    request_hash = request_fingerprint("record_payment", {"invoice_id": invoice_id, **payload})
    replay = next((payment for payment in current.get("payments") or [] if payment.get("idempotency_key") == idempotency_key), None)
    if replay:
        if replay.get("request_hash") and replay["request_hash"] != request_hash:
            raise _idempotency_conflict()
        return invoice_view(current)
    if current.get("lifecycle") != "issued":
        raise HTTPException(status_code=409, detail="Payments can be recorded only against an issued invoice")
    if current.get("closed_at"):
        raise HTTPException(status_code=409, detail="A closed invoice must be reopened before recording payments")
    amount = int(payload["amount_paise"])
    paid = int(current.get("amount_paid_paise") or 0)
    refunded = int(current.get("amount_refunded_paise") or 0)
    adjusted_total = invoice_amounts(current)["adjusted_total_paise"]
    if amount <= 0 or paid - refunded + amount > adjusted_total:
        raise HTTPException(status_code=422, detail="Payment exceeds the invoice balance")
    validate_payment_date(payload["paid_on"], current["invoice_date"], _today(), await _closed_through(org_id))
    payment = {**payload, "payment_id": str(uuid.uuid4()), "idempotency_key": idempotency_key, "request_hash": request_hash, "recorded_at": datetime.now(timezone.utc), "recorded_by": actor_id, "reversed_at": None}
    updated = await invoices.find_one_and_update(
        {"_id": current["_id"], "lifecycle": "issued", "revision": current["revision"], "amount_paid_paise": paid},
        {"$push": {"payments": payment, "audit": _event(
            "payment_recorded", actor_id, actor_name=actor_name, actor_role=actor_role,
            revision_before=int(current["revision"]), revision_after=int(current["revision"]) + 1,
            details={key: payment.get(key) for key in ("payment_id", "amount_paise", "paid_on", "method", "reference")},
        )}, "$inc": {"amount_paid_paise": amount, "revision": 1}, "$set": {"updated_at": datetime.now(timezone.utc), "updated_by": actor_id}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        latest = await get_invoice(org_id, invoice_id)
        replay = next((row for row in latest.get("payments") or [] if row.get("idempotency_key") == idempotency_key), None)
        if replay:
            if replay.get("request_hash") and replay["request_hash"] != request_hash:
                raise _idempotency_conflict()
            return invoice_view(latest)
        raise HTTPException(status_code=409, detail="This invoice changed while the payment was being recorded")
    return invoice_view(updated)


async def reverse_payment(
    org_id: str, invoice_id: str, payment_id: str, actor_id: str, reason: str,
    idempotency_key: str, actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_invoice(org_id, invoice_id)
    request_hash = request_fingerprint("reverse_payment", {
        "invoice_id": invoice_id, "payment_id": payment_id, "reason": reason,
    })
    payment = next((row for row in current.get("payments") or [] if row.get("payment_id") == payment_id), None)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.get("reversed_at"):
        if payment.get("reversal_idempotency_key") == idempotency_key and payment.get("reversal_request_hash") != request_hash:
            raise _idempotency_conflict()
        return invoice_view(current)
    if current.get("closed_at"):
        raise HTTPException(status_code=409, detail="A closed invoice must be reopened before reversing payments")
    amount = int(payment["amount_paise"])
    if int(current.get("amount_paid_paise") or 0) - amount < int(current.get("amount_refunded_paise") or 0):
        raise HTTPException(status_code=409, detail="Reverse the related refunds before reversing this payment")
    validate_payment_date(_today(), current["invoice_date"], _today(), await _closed_through(org_id))
    now = datetime.now(timezone.utc)
    updated = await invoices.find_one_and_update(
        {"_id": current["_id"], "revision": current["revision"],
         "amount_paid_paise": {"$gte": amount + int(current.get("amount_refunded_paise") or 0)},
         "payments": {"$elemMatch": {"payment_id": payment_id, "reversed_at": None}}},
        {"$set": {"payments.$[payment].reversed_at": now, "payments.$[payment].reversed_by": actor_id,
            "payments.$[payment].reversal_reason": reason, "payments.$[payment].reversal_idempotency_key": idempotency_key,
            "payments.$[payment].reversal_request_hash": request_hash, "updated_at": now, "updated_by": actor_id},
         "$inc": {"amount_paid_paise": -amount, "revision": 1}, "$push": {"audit": _event(
            "payment_reversed", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
            revision_before=int(current["revision"]), revision_after=int(current["revision"]) + 1,
            details={"payment_id": payment_id, "amount_paise": amount},
        )}},
        array_filters=[{"payment.payment_id": payment_id, "payment.reversed_at": None}],
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice changed while the payment was being reversed")
    return invoice_view(updated)


async def void_invoice(
    org_id: str, invoice_id: str, actor_id: str, expected_revision: int, reason: str,
    idempotency_key: str, actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_invoice(org_id, invoice_id)
    request_hash = request_fingerprint("void_invoice", {
        "invoice_id": invoice_id, "expected_revision": expected_revision, "reason": reason,
    })
    if current.get("lifecycle") == "void":
        if current.get("void_idempotency_key") == idempotency_key and current.get("void_request_hash") != request_hash:
            raise _idempotency_conflict()
        return invoice_view(current)
    validate_invoice_date(current["invoice_date"], _today(), await _closed_through(org_id))
    if current.get("closed_at"):
        raise HTTPException(status_code=409, detail="A closed invoice must be reopened before voiding")
    if int(current.get("credit_total_paise") or 0) or int(current.get("debit_total_paise") or 0):
        raise HTTPException(status_code=409, detail="An invoice with issued credit or debit notes cannot be voided")
    try:
        updated = await invoices.find_one_and_update(
            {"org_id": org_id, "invoice_id": invoice_id, "lifecycle": "issued", "amount_paid_paise": 0, "revision": expected_revision},
            {"$set": {"lifecycle": "void", "voided_at": datetime.now(timezone.utc), "voided_by": actor_id,
                "void_reason": reason, "void_idempotency_key": idempotency_key, "void_request_hash": request_hash,
                "voided_revision": expected_revision + 1, "pdf_state": "pending", "updated_at": datetime.now(timezone.utc), "updated_by": actor_id}, "$inc": {"revision": 1}, "$push": {"audit": _event(
                "voided", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
                revision_before=expected_revision, revision_after=expected_revision + 1,
            )}},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        key_owner = await invoices.find_one(
            {"org_id": org_id, "void_idempotency_key": idempotency_key}, {"invoice_id": 1},
        )
        if key_owner and key_owner.get("invoice_id") != invoice_id:
            raise _idempotency_conflict() from exc
        raise
    if not updated:
        current = await get_invoice(org_id, invoice_id)
        if int(current.get("amount_paid_paise") or 0) > 0:
            raise HTTPException(status_code=409, detail="A paid invoice cannot be voided; use the applicable correction process")
        raise HTTPException(status_code=409, detail="Only an unchanged, unpaid issued invoice can be voided")
    try:
        await render_invoice_pdf(org_id, invoice_id, actor_id)
    except Exception:
        pass
    return invoice_view(await get_invoice(org_id, invoice_id))


async def render_invoice_pdf(org_id: str, invoice_id: str, actor_id: str) -> dict:
    current = await get_invoice(org_id, invoice_id)
    if current.get("lifecycle") not in {"issued", "void"}:
        raise HTTPException(status_code=409, detail="Only a finalized invoice can have a final PDF")
    existing = current.get("pdf") or {}
    verified_fields = ("sha256", "version", "byte_size", "renderer_version", "issued_revision")
    artifact_revision = int(current.get("voided_revision") or current.get("issued_revision") or current.get("revision") or 1)
    artifact_field = "voided_revision" if current["lifecycle"] == "void" else "issued_revision"
    artifact_match = {"_id": current["_id"], "lifecycle": current["lifecycle"], artifact_field: artifact_revision}
    if current.get("pdf_state") == "ready" and int(existing.get("issued_revision") or 0) == artifact_revision and re.fullmatch(r"[0-9a-f]{64}", str(existing.get("sha256") or "")) and all(
        existing.get(field) is not None for field in verified_fields[1:]
    ):
        return {"state": "ready", "generated_at": existing.get("generated_at")}
    renderer_version = int(current.get("pdf_renderer_version") or RENDERER_VERSION)
    if renderer_version != RENDERER_VERSION:
        raise HTTPException(status_code=409, detail="This invoice requires its original PDF renderer version")
    try:
        render_copy = deepcopy(current)
        render_copy["attachments"] = await load_invoice_attachment_images(
            org_id, invoice_id, issued_revision=int(current.get("issued_revision") or current.get("revision") or 1),
        )
        data = await asyncio.to_thread(render_native_invoice_pdf, render_copy)
        stored = await upload_native_invoice_pdf(
            data, org_id, invoice_id, current["invoice_number"],
            renderer_version=renderer_version, issued_revision=artifact_revision,
        )
    except Exception as exc:
        await invoices.update_one(
            {**artifact_match, "$or": [{"pdf_state": {"$ne": "ready"}}, {"pdf.sha256": {"$exists": False}}]},
            {"$set": {"pdf_state": "failed", "pdf_error": type(exc).__name__, "updated_at": datetime.now(timezone.utc)}},
        )
        raise
    pushed = {"audit": _event(
        "pdf_generated", actor_id, revision_before=int(current.get("revision") or 1),
        revision_after=int(current.get("revision") or 1),
        details={"sha256": stored.get("sha256"), "renderer_version": renderer_version},
    )}
    if existing:
        pushed["legacy_pdfs"] = {"pdf": existing, "replaced_at": datetime.now(timezone.utc), "replaced_by": actor_id}
    updated = await invoices.find_one_and_update(
        {**artifact_match, "$or": [{"pdf_state": {"$ne": "ready"}}, {"pdf.sha256": {"$exists": False}}]},
        {"$set": {
            "pdf": stored, "pdf_state": "ready", "pdf_updated_by": actor_id,
            "pdf_renderer_version": renderer_version, "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"pdf_error": ""}, "$push": pushed},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        updated = await get_invoice(org_id, invoice_id)
    return {"state": updated.get("pdf_state"), "generated_at": (updated.get("pdf") or {}).get("generated_at")}


async def invoice_pdf_link(org_id: str, invoice_id: str) -> dict:
    current = await get_invoice(org_id, invoice_id)
    pdf = current.get("pdf") or {}
    artifact_revision = int(current.get("voided_revision") or current.get("issued_revision") or 0)
    if current.get("lifecycle") not in {"issued", "void"} or current.get("pdf_state") != "ready" or int(pdf.get("issued_revision") or -1) != artifact_revision or not re.fullmatch(r"[0-9a-f]{64}", str(pdf.get("sha256") or "")) or not all(
        pdf.get(field) is not None for field in ("version", "byte_size", "renderer_version", "issued_revision")
    ):
        raise HTTPException(status_code=404, detail="Invoice PDF is not ready")
    return {"url": native_invoice_pdf_download_url(pdf), "expires_in": 60}


async def render_invoice_receipt(org_id: str, invoice_id: str, actor_id: str) -> dict:
    current = await get_invoice(org_id, invoice_id)
    if current.get("lifecycle") != "issued" or invoice_amounts(current)["net_paid_paise"] <= 0:
        raise HTTPException(status_code=409, detail="A receipt requires payments on an issued invoice")
    revision = int(current.get("revision") or 0)
    existing = current.get("receipt") or {}
    if (
        int(existing.get("issued_revision") or -1) == revision
        and re.fullmatch(r"[0-9a-f]{64}", str(existing.get("sha256") or ""))
        and all(existing.get(field) is not None for field in ("version", "byte_size", "renderer_version"))
    ):
        return {"state": "ready", "generated_at": existing.get("generated_at")}
    data = await asyncio.to_thread(render_native_receipt_pdf, deepcopy(current))
    stored = await upload_native_invoice_pdf(
        data,
        org_id,
        f"{invoice_id}-receipt-r{revision}",
        f"{current.get('invoice_number') or invoice_id}-receipt",
        renderer_version=RENDERER_VERSION,
        issued_revision=revision,
    )
    updated = await invoices.find_one_and_update(
        {"_id": current["_id"], "org_id": org_id, "lifecycle": "issued", "revision": revision},
        {"$set": {"receipt": stored, "receipt_updated_by": actor_id, "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice changed while its receipt was being generated")
    return {"state": "ready", "generated_at": stored.get("generated_at")}


async def invoice_receipt_link(org_id: str, invoice_id: str) -> dict:
    current = await get_invoice(org_id, invoice_id)
    receipt = current.get("receipt") or {}
    revision = int(current.get("revision") or 0)
    if (
        current.get("lifecycle") != "issued"
        or invoice_amounts(current)["net_paid_paise"] <= 0
        or int(receipt.get("issued_revision") or -1) != revision
        or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256") or ""))
        or not all(receipt.get(field) is not None for field in ("version", "byte_size", "renderer_version"))
    ):
        raise HTTPException(status_code=404, detail="Payment receipt is not ready")
    return {
        "url": native_invoice_pdf_download_url(receipt),
        "filename": f"{current.get('invoice_number') or invoice_id}-receipt.pdf",
        "expires_in": 60,
    }
