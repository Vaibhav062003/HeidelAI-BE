"""GST credit/debit note and cash-refund lifecycle for native invoices."""

import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import re
import uuid

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_client, get_async_mongo_db
from services.cloudinary_service import native_invoice_pdf_download_url, upload_native_invoice_pdf
from services.native_invoice_pdf import (
    RENDERER_VERSION,
    InvoicePdfError,
    assert_invoice_text_supported,
    render_native_adjustment_pdf,
)
from services.native_invoice_service import (
    _closed_through,
    _event,
    _idempotency_conflict,
    _today,
    _validate_einvoice_applicability,
    EINVOICE_EXEMPTION_DECLARATION,
    adjustments,
    calculate_invoice_totals,
    financial_year,
    format_document_number,
    get_invoice,
    invoice_amounts,
    invoice_view,
    invoices,
    numbering_config,
    request_fingerprint,
    sequences,
    validate_invoice_date,
    validate_payment_date,
)
from services.utils import serialize_mongo


db = get_async_mongo_db()


def adjustment_view(document: dict) -> dict:
    value = deepcopy(document)
    value.pop("_id", None)
    value.pop("audit", None)
    value.pop("pdf", None)
    value.pop("legacy_pdfs", None)
    for field in tuple(value):
        if field.endswith("_idempotency_key") or field.endswith("_request_hash"):
            value.pop(field, None)
    signature = value.pop("signature_image", None)
    if signature:
        value["signature_image"] = {
            key: signature.get(key) for key in ("filename", "content_type", "byte_size", "sha256")
        }
    value["refunded_paise"] = int(value.get("refunded_paise") or 0)
    value["refundable_paise"] = max(0, int(value.get("total_paise") or 0) - value["refunded_paise"])
    value["pdf_state"] = value.get("pdf_state") or "pending"
    return serialize_mongo(value)


async def get_adjustment(org_id: str, adjustment_id: str, *, session=None) -> dict:
    document = await adjustments.find_one({"org_id": org_id, "adjustment_id": adjustment_id}, session=session)
    if not document:
        raise HTTPException(status_code=404, detail="Credit or debit note not found")
    return document


async def list_adjustments(org_id: str, source_invoice_id: str) -> list[dict]:
    await get_invoice(org_id, source_invoice_id)
    rows = await adjustments.find({"org_id": org_id, "source_invoice_id": source_invoice_id}).sort("updated_at", -1).to_list(length=100)
    return [adjustment_view(row) for row in rows]


def _validated_note_payload(source: dict, payload: dict) -> tuple[dict, dict]:
    note_date = str(payload["note_date"])[:10]
    validate_invoice_date(note_date, _today())
    if date.fromisoformat(note_date) < date.fromisoformat(str(source["invoice_date"])[:10]):
        raise HTTPException(status_code=422, detail="Credit/debit note date cannot precede the invoice date")
    source_items = {str(item.get("id")): item for item in source.get("items") or []}
    seen = set()
    items = []
    for item in payload.get("items") or []:
        source_item_id = str(item.get("source_item_id") or "")
        original = source_items.get(source_item_id)
        if not original or source_item_id in seen:
            raise HTTPException(status_code=422, detail="Each adjustment line must reference one distinct invoice line")
        seen.add(source_item_id)
        for field in ("hsn_sac", "supply_type", "gst_rate_bps", "uqc"):
            if str(item.get(field)) != str(original.get(field)):
                raise HTTPException(status_code=422, detail=f"Adjustment line {field} must match the source invoice")
        items.append({**item, "id": str(item.get("id") or uuid.uuid4()), "source_item_id": source_item_id})
    calculation = {
        "seller": deepcopy(source["seller"]),
        "place_of_supply_state_code": source["place_of_supply_state_code"],
        "items": items,
        "discount_paise": int(payload.get("discount_paise") or 0),
    }
    return {**payload, "note_date": note_date, "items": items}, calculate_invoice_totals(calculation)


async def validate_credit_line_capacity(
    org_id: str, source: dict, credit_items: list[dict], session,
) -> None:
    remaining = {
        str(item.get("id")): {
            "quantity": Decimal(str(item.get("quantity") or 0)),
            "taxable_value_paise": int(item.get("taxable_value_paise") or 0),
            "total_paise": int(item.get("total_paise") or 0),
        }
        for item in source.get("items") or []
    }
    issued_notes = adjustments.find({
        "org_id": org_id, "source_invoice_id": source["invoice_id"], "lifecycle": "issued",
    }, {"document_kind": 1, "items": 1}, session=session)
    async for note in issued_notes:
        direction = 1 if note.get("document_kind") == "debit_note" else -1
        for item in note.get("items") or []:
            capacity = remaining.get(str(item.get("source_item_id") or ""))
            if not capacity:
                continue
            capacity["quantity"] += direction * Decimal(str(item.get("quantity") or 0))
            capacity["taxable_value_paise"] += direction * int(item.get("taxable_value_paise") or 0)
            capacity["total_paise"] += direction * int(item.get("total_paise") or 0)
    for item in credit_items:
        capacity = remaining.get(str(item.get("source_item_id") or ""))
        if not capacity or Decimal(str(item.get("quantity") or 0)) > capacity["quantity"]:
            raise HTTPException(status_code=422, detail="Credit-note quantity exceeds the remaining source-line quantity")
        if int(item.get("taxable_value_paise") or 0) > capacity["taxable_value_paise"] or int(item.get("total_paise") or 0) > capacity["total_paise"]:
            raise HTTPException(status_code=422, detail="Credit-note value exceeds the remaining source-line value")


async def create_adjustment(
    org_id: str, invoice_id: str, actor_id: str, payload: dict, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    request_hash = request_fingerprint("create_adjustment", {"invoice_id": invoice_id, **payload})
    replay = await adjustments.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
    if replay:
        if replay.get("create_request_hash") != request_hash:
            raise _idempotency_conflict()
        return adjustment_view(replay)
    source = await get_invoice(org_id, invoice_id)
    if source.get("lifecycle") != "issued":
        raise HTTPException(status_code=409, detail="Credit/debit notes require an issued source invoice")
    if source.get("closed_at"):
        raise HTTPException(status_code=409, detail="Reopen the invoice before creating a credit/debit note")
    normalized, totals = _validated_note_payload(source, payload)
    validate_invoice_date(normalized["note_date"], _today(), await _closed_through(org_id))
    now = datetime.now(timezone.utc)
    document = {
        **normalized, **totals,
        "org_id": org_id, "adjustment_id": str(uuid.uuid4()), "source_invoice_id": invoice_id,
        "source_invoice_number": source.get("invoice_number"), "source_invoice_date": source.get("invoice_date"),
        "document_kind": normalized["kind"], "lifecycle": "draft", "revision": 1,
        "note_number": None, "financial_year": None, "series": None, "numbering": None,
        "seller": deepcopy(source["seller"]), "customer": deepcopy(source["customer"]),
        "place_of_supply_state_code": source["place_of_supply_state_code"],
        "shipping_address": source.get("shipping_address") or "",
        "shipping_state_code": source.get("shipping_state_code"),
        "authorized_signatory": source.get("authorized_signatory") or "",
        "einvoice_exemption_declaration": source.get("einvoice_exemption_declaration"),
        "reverse_charge": bool(source.get("reverse_charge")), "currency": "INR", "supply_category": "domestic",
        "presentation": deepcopy(source.get("presentation")), "signature_image": deepcopy(source.get("signature_image")),
        "refunded_paise": 0, "pdf_state": "pending", "created_at": now, "created_by": actor_id,
        "updated_at": now, "updated_by": actor_id, "create_idempotency_key": idempotency_key,
        "create_request_hash": request_hash,
        "audit": [_event("adjustment_draft_created", actor_id, actor_name=actor_name, actor_role=actor_role,
            revision_after=1, details={"source_invoice_id": invoice_id, "kind": normalized["kind"]})],
    }
    try:
        await adjustments.insert_one(document)
    except DuplicateKeyError as exc:
        replay = await adjustments.find_one({"org_id": org_id, "create_idempotency_key": idempotency_key})
        if replay and replay.get("create_request_hash") == request_hash:
            return adjustment_view(replay)
        raise _idempotency_conflict() from exc
    return adjustment_view(document)


async def update_adjustment(
    org_id: str, adjustment_id: str, actor_id: str, payload: dict, expected_revision: int,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    current = await get_adjustment(org_id, adjustment_id)
    if current.get("lifecycle") != "draft":
        raise HTTPException(status_code=409, detail="Issued credit/debit notes are immutable")
    if int(current.get("revision") or 1) != expected_revision:
        raise HTTPException(status_code=409, detail="This credit/debit note changed. Reload before saving.")
    source = await get_invoice(org_id, current["source_invoice_id"])
    if payload.get("kind") != current.get("document_kind"):
        raise HTTPException(status_code=409, detail="A credit note cannot be changed into a debit note")
    normalized, totals = _validated_note_payload(source, payload)
    validate_invoice_date(normalized["note_date"], _today(), await _closed_through(org_id))
    updated = await adjustments.find_one_and_update(
        {"org_id": org_id, "adjustment_id": adjustment_id, "lifecycle": "draft", "revision": expected_revision},
        {"$set": {**normalized, **totals, "updated_at": datetime.now(timezone.utc), "updated_by": actor_id},
         "$inc": {"revision": 1}, "$push": {"audit": _event(
             "adjustment_draft_updated", actor_id, actor_name=actor_name, actor_role=actor_role,
             revision_before=expected_revision, revision_after=expected_revision + 1,
         )}}, return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This credit/debit note changed. Reload before saving.")
    return adjustment_view(updated)


async def issue_adjustment(
    org_id: str, adjustment_id: str, actor_id: str, expected_revision: int, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    client = get_async_mongo_client()
    request_hash = request_fingerprint("issue_adjustment", {"adjustment_id": adjustment_id, "expected_revision": expected_revision})

    async def transaction(session):
        current = await get_adjustment(org_id, adjustment_id, session=session)
        if current.get("lifecycle") == "issued" and current.get("issue_idempotency_key") == idempotency_key:
            if current.get("issue_request_hash") != request_hash:
                raise _idempotency_conflict()
            return current
        if current.get("lifecycle") != "draft" or int(current.get("revision") or 1) != expected_revision:
            raise HTTPException(status_code=409, detail="Only an unchanged draft credit/debit note can be issued")
        source = await invoices.find_one({"org_id": org_id, "invoice_id": current["source_invoice_id"]}, session=session)
        if not source or source.get("lifecycle") != "issued" or source.get("closed_at"):
            raise HTTPException(status_code=409, detail="The source invoice is not open for adjustments")
        if not current.get("signature_image"):
            raise HTTPException(status_code=409, detail="Add an authorised signature before issuing this credit/debit note")
        try:
            assert_invoice_text_supported(current)
        except InvoicePdfError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        key_owner = await adjustments.find_one(
            {"org_id": org_id, "issue_idempotency_key": idempotency_key}, {"_id": 1}, session=session,
        )
        if key_owner and key_owner.get("_id") != current.get("_id"):
            raise _idempotency_conflict()
        assessment = await _validate_einvoice_applicability(org_id, current, session)
        if current["document_kind"] == "credit_note":
            available = int(source.get("total_paise") or 0) + int(source.get("debit_total_paise") or 0) - int(source.get("credit_total_paise") or 0)
            if int(current["total_paise"]) > available:
                raise HTTPException(status_code=422, detail="Credit note exceeds the remaining adjusted invoice value")
            await validate_credit_line_capacity(org_id, source, current.get("items") or [], session)
        year = financial_year(current["note_date"])
        kind = current["document_kind"]
        config = await numbering_config(org_id, kind, session)
        sequence_id = f"{org_id}:{year}:{kind}:{config['series']}"
        previous = await sequences.find_one({"_id": sequence_id}, {"last_invoice_date": 1}, session=session)
        validate_invoice_date(current["note_date"], _today(), await _closed_through(org_id, session), (previous or {}).get("last_invoice_date"))
        sequence = await sequences.find_one_and_update(
            {"_id": sequence_id},
            {"$inc": {"value": 1}, "$set": {"last_invoice_date": str(current["note_date"])[:10]}, "$setOnInsert": {
                "org_id": org_id, "financial_year": year, "document_kind": kind, "series": config["series"],
            }}, upsert=True, return_document=ReturnDocument.AFTER, session=session,
        )
        number = format_document_number({**config, "document_kind": kind}, year, int(sequence["value"]))
        now = datetime.now(timezone.utc)
        aggregate_field = "credit_total_paise" if kind == "credit_note" else "debit_total_paise"
        new_adjusted_total = invoice_amounts(source)["adjusted_total_paise"] + (
            -int(current["total_paise"]) if kind == "credit_note" else int(current["total_paise"])
        )
        source_updated = await invoices.find_one_and_update(
            {"_id": source["_id"], "revision": source["revision"], "closed_at": None, "lifecycle": "issued"},
            {"$inc": {aggregate_field: int(current["total_paise"]), "revision": 1}, "$set": {
                "adjusted_total_paise": new_adjusted_total, "updated_at": now, "updated_by": actor_id,
            },
             "$push": {"audit": _event(
                 f"{kind}_issued", actor_id, actor_name=actor_name, actor_role=actor_role,
                 revision_before=int(source["revision"]), revision_after=int(source["revision"]) + 1,
                 details={"adjustment_id": adjustment_id, "note_number": number, "amount_paise": int(current["total_paise"])},
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not source_updated:
            raise HTTPException(status_code=409, detail="The source invoice changed while issuing the note")
        updated = await adjustments.find_one_and_update(
            {"_id": current["_id"], "lifecycle": "draft", "revision": expected_revision},
            {"$set": {
                "lifecycle": "issued", "note_number": number, "financial_year": year, "series": config["series"],
                "numbering": config, "issued_at": now, "issued_by": actor_id, "issue_idempotency_key": idempotency_key,
                "einvoice_exemption_declaration": EINVOICE_EXEMPTION_DECLARATION if assessment.get("not_required_reason") == "notified_exemption" else None,
                "issue_request_hash": request_hash, "issued_revision": expected_revision + 1, "pdf_state": "pending",
                "pdf_renderer_version": RENDERER_VERSION,
                "updated_at": now, "updated_by": actor_id,
            }, "$inc": {"revision": 1}, "$push": {"audit": _event(
                "adjustment_issued", actor_id, actor_name=actor_name, actor_role=actor_role,
                revision_before=expected_revision, revision_after=expected_revision + 1,
                details={"note_number": number, "source_invoice_id": current["source_invoice_id"]},
            )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="The note changed while it was being issued")
        return updated

    try:
        async with await client.start_session() as session:
            document = await session.with_transaction(transaction)
    except DuplicateKeyError as exc:
        key_owner = await adjustments.find_one(
            {"org_id": org_id, "issue_idempotency_key": idempotency_key}, {"adjustment_id": 1},
        )
        if key_owner and key_owner.get("adjustment_id") != adjustment_id:
            raise _idempotency_conflict() from exc
        raise
    try:
        await render_adjustment_pdf(org_id, adjustment_id, actor_id)
    except Exception:
        pass
    return adjustment_view(await get_adjustment(org_id, adjustment_id))


async def void_adjustment(
    org_id: str, adjustment_id: str, actor_id: str, expected_revision: int, reason: str,
    idempotency_key: str, actor_name: str = "", actor_role: str = "",
) -> dict:
    client = get_async_mongo_client()
    request_hash = request_fingerprint("void_adjustment", {
        "adjustment_id": adjustment_id, "expected_revision": expected_revision, "reason": reason,
    })

    async def transaction(session):
        current = await get_adjustment(org_id, adjustment_id, session=session)
        if current.get("lifecycle") == "void":
            if current.get("void_idempotency_key") == idempotency_key and current.get("void_request_hash") != request_hash:
                raise _idempotency_conflict()
            return current
        if current.get("lifecycle") != "issued" or int(current.get("revision") or 1) != expected_revision:
            raise HTTPException(status_code=409, detail="Only an unchanged issued note can be voided")
        key_owner = await adjustments.find_one(
            {"org_id": org_id, "void_idempotency_key": idempotency_key}, {"_id": 1}, session=session,
        )
        if key_owner and key_owner.get("_id") != current.get("_id"):
            raise _idempotency_conflict()
        if int(current.get("refunded_paise") or 0):
            raise HTTPException(status_code=409, detail="A credit note with refunds cannot be voided")
        validate_invoice_date(current["note_date"], _today(), await _closed_through(org_id, session))
        source = await invoices.find_one({"org_id": org_id, "invoice_id": current["source_invoice_id"]}, session=session)
        if not source or source.get("closed_at"):
            raise HTTPException(status_code=409, detail="The source invoice must be open before voiding a note")
        field = "credit_total_paise" if current["document_kind"] == "credit_note" else "debit_total_paise"
        new_adjusted_total = invoice_amounts(source)["adjusted_total_paise"] + (
            int(current["total_paise"]) if current["document_kind"] == "credit_note" else -int(current["total_paise"])
        )
        if current["document_kind"] == "debit_note":
            after = deepcopy(source)
            after[field] = int(source.get(field) or 0) - int(current["total_paise"])
            if invoice_amounts(after)["refundable_paise"]:
                raise HTTPException(status_code=409, detail="Refund excess payment before voiding this debit note")
        now = datetime.now(timezone.utc)
        source_updated = await invoices.find_one_and_update(
            {"_id": source["_id"], "revision": source["revision"], field: {"$gte": int(current["total_paise"])}},
            {"$inc": {field: -int(current["total_paise"]), "revision": 1}, "$set": {
                "adjusted_total_paise": new_adjusted_total, "updated_at": now, "updated_by": actor_id,
            },
             "$push": {"audit": _event(
                 f"{current['document_kind']}_voided", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
                 revision_before=int(source["revision"]), revision_after=int(source["revision"]) + 1,
                 details={"adjustment_id": adjustment_id, "amount_paise": int(current["total_paise"])},
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not source_updated:
            raise HTTPException(status_code=409, detail="The source invoice changed while voiding the note")
        updated = await adjustments.find_one_and_update(
            {"_id": current["_id"], "lifecycle": "issued", "revision": expected_revision},
            {"$set": {"lifecycle": "void", "voided_at": now, "voided_by": actor_id, "void_reason": reason,
                      "void_idempotency_key": idempotency_key, "void_request_hash": request_hash,
                      "voided_revision": expected_revision + 1, "updated_at": now, "updated_by": actor_id,
                      "pdf_state": "pending", "pdf_renderer_version": RENDERER_VERSION},
             "$inc": {"revision": 1}, "$push": {"audit": _event(
                 "adjustment_voided", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
                 revision_before=expected_revision, revision_after=expected_revision + 1,
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="The note changed while it was being voided")
        return updated

    try:
        async with await client.start_session() as session:
            document = await session.with_transaction(transaction)
    except DuplicateKeyError as exc:
        key_owner = await adjustments.find_one(
            {"org_id": org_id, "void_idempotency_key": idempotency_key}, {"adjustment_id": 1},
        )
        if key_owner and key_owner.get("adjustment_id") != adjustment_id:
            raise _idempotency_conflict() from exc
        raise
    try:
        await render_adjustment_pdf(org_id, adjustment_id, actor_id)
    except Exception:
        pass
    return adjustment_view(await get_adjustment(org_id, adjustment_id))


async def record_refund(
    org_id: str, invoice_id: str, actor_id: str, payload: dict, idempotency_key: str,
    actor_name: str = "", actor_role: str = "",
) -> dict:
    client = get_async_mongo_client()
    request_hash = request_fingerprint("record_refund", {"invoice_id": invoice_id, **payload})

    async def transaction(session):
        source = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, session=session)
        if not source:
            raise HTTPException(status_code=404, detail="Invoice not found")
        replay = next((row for row in source.get("refunds") or [] if row.get("idempotency_key") == idempotency_key), None)
        if replay:
            if replay.get("request_hash") != request_hash:
                raise _idempotency_conflict()
            return source
        if source.get("lifecycle") != "issued" or source.get("closed_at"):
            raise HTTPException(status_code=409, detail="Refunds require an open issued invoice")
        note = await adjustments.find_one({
            "org_id": org_id, "adjustment_id": payload["credit_note_id"], "source_invoice_id": invoice_id,
            "document_kind": "credit_note", "lifecycle": "issued",
        }, session=session)
        if not note:
            raise HTTPException(status_code=404, detail="Issued credit note not found for this invoice")
        amount = int(payload["amount_paise"])
        available_note = int(note["total_paise"]) - int(note.get("refunded_paise") or 0)
        available_invoice = invoice_amounts(source)["refundable_paise"]
        if amount <= 0 or amount > available_note or amount > available_invoice:
            raise HTTPException(status_code=422, detail="Refund exceeds the refundable invoice or credit-note balance")
        validate_payment_date(payload["refunded_on"], note["note_date"], _today(), await _closed_through(org_id, session))
        now = datetime.now(timezone.utc)
        refund = {
            **payload, "refund_id": str(uuid.uuid4()), "idempotency_key": idempotency_key,
            "request_hash": request_hash, "recorded_at": now, "recorded_by": actor_id,
        }
        note_updated = await adjustments.find_one_and_update(
            {"_id": note["_id"], "revision": note["revision"], "refunded_paise": int(note.get("refunded_paise") or 0)},
            {"$inc": {"refunded_paise": amount, "revision": 1}, "$set": {"updated_at": now, "updated_by": actor_id},
             "$push": {"audit": _event(
                 "refund_recorded", actor_id, actor_name=actor_name, actor_role=actor_role,
                 revision_before=int(note["revision"]), revision_after=int(note["revision"]) + 1,
                 details={"refund_id": refund["refund_id"], "amount_paise": amount, "source_invoice_id": invoice_id},
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not note_updated:
            raise HTTPException(status_code=409, detail="The credit note changed while recording the refund")
        updated = await invoices.find_one_and_update(
            {"_id": source["_id"], "revision": source["revision"], "closed_at": None},
            {"$inc": {"amount_refunded_paise": amount, "revision": 1}, "$set": {"updated_at": now, "updated_by": actor_id},
             "$push": {"refunds": refund, "audit": _event(
                 "refund_recorded", actor_id, actor_name=actor_name, actor_role=actor_role,
                 revision_before=int(source["revision"]), revision_after=int(source["revision"]) + 1,
                 details={"refund_id": refund["refund_id"], "credit_note_id": note["adjustment_id"], "amount_paise": amount},
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="The invoice changed while recording the refund")
        return updated

    async with await client.start_session() as session:
        document = await session.with_transaction(transaction)
    return invoice_view(document)


async def reverse_refund(
    org_id: str,
    invoice_id: str,
    refund_id: str,
    actor_id: str,
    reason: str,
    idempotency_key: str,
    actor_name: str = "",
    actor_role: str = "",
) -> dict:
    client = get_async_mongo_client()
    request_hash = request_fingerprint("reverse_refund", {
        "invoice_id": invoice_id,
        "refund_id": refund_id,
        "reason": reason,
    })

    async def transaction(session):
        source = await invoices.find_one({"org_id": org_id, "invoice_id": invoice_id}, session=session)
        if not source:
            raise HTTPException(status_code=404, detail="Invoice not found")
        refund = next((row for row in source.get("refunds") or [] if row.get("refund_id") == refund_id), None)
        if not refund:
            raise HTTPException(status_code=404, detail="Refund not found")
        if refund.get("reversed_at"):
            if refund.get("reversal_idempotency_key") == idempotency_key and refund.get("reversal_request_hash") != request_hash:
                raise _idempotency_conflict()
            return source
        if source.get("lifecycle") != "issued" or source.get("closed_at"):
            raise HTTPException(status_code=409, detail="Reopen the issued invoice before reversing a refund")
        note = await adjustments.find_one({
            "org_id": org_id,
            "adjustment_id": refund.get("credit_note_id"),
            "source_invoice_id": invoice_id,
            "document_kind": "credit_note",
            "lifecycle": "issued",
        }, session=session)
        if not note:
            raise HTTPException(status_code=409, detail="The issued credit note for this refund is unavailable")
        amount = int(refund.get("amount_paise") or 0)
        if amount <= 0 or int(source.get("amount_refunded_paise") or 0) < amount or int(note.get("refunded_paise") or 0) < amount:
            raise HTTPException(status_code=409, detail="Refund balances are inconsistent; reversal was not applied")
        validate_payment_date(_today(), note["note_date"], _today(), await _closed_through(org_id, session))
        now = datetime.now(timezone.utc)
        note_updated = await adjustments.find_one_and_update(
            {"_id": note["_id"], "revision": note["revision"], "refunded_paise": {"$gte": amount}},
            {"$inc": {"refunded_paise": -amount, "revision": 1}, "$set": {"updated_at": now, "updated_by": actor_id},
             "$push": {"audit": _event(
                 "refund_reversed", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
                 revision_before=int(note["revision"]), revision_after=int(note["revision"]) + 1,
                 details={"refund_id": refund_id, "amount_paise": amount, "source_invoice_id": invoice_id},
             )}}, return_document=ReturnDocument.AFTER, session=session,
        )
        if not note_updated:
            raise HTTPException(status_code=409, detail="The credit note changed while reversing the refund")
        updated = await invoices.find_one_and_update(
            {
                "_id": source["_id"],
                "revision": source["revision"],
                "amount_refunded_paise": {"$gte": amount},
                "refunds": {"$elemMatch": {"refund_id": refund_id, "reversed_at": None}},
            },
            {"$set": {
                "refunds.$[refund].reversed_at": now,
                "refunds.$[refund].reversed_by": actor_id,
                "refunds.$[refund].reversal_reason": reason,
                "refunds.$[refund].reversal_idempotency_key": idempotency_key,
                "refunds.$[refund].reversal_request_hash": request_hash,
                "updated_at": now,
                "updated_by": actor_id,
            }, "$inc": {"amount_refunded_paise": -amount, "revision": 1}, "$push": {"audit": _event(
                "refund_reversed", actor_id, reason, actor_name=actor_name, actor_role=actor_role,
                revision_before=int(source["revision"]), revision_after=int(source["revision"]) + 1,
                details={"refund_id": refund_id, "credit_note_id": note["adjustment_id"], "amount_paise": amount},
            )}},
            array_filters=[{"refund.refund_id": refund_id, "refund.reversed_at": None}],
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not updated:
            raise HTTPException(status_code=409, detail="The invoice changed while reversing the refund")
        return updated

    async with await client.start_session() as session:
        document = await session.with_transaction(transaction)
    return invoice_view(document)


async def render_adjustment_pdf(org_id: str, adjustment_id: str, actor_id: str) -> dict:
    current = await get_adjustment(org_id, adjustment_id)
    if current.get("lifecycle") not in {"issued", "void"}:
        raise HTTPException(status_code=409, detail="Only a finalized credit/debit note can have a final PDF")
    existing = current.get("pdf") or {}
    artifact_revision = int(current.get("voided_revision") or current.get("issued_revision") or current.get("revision") or 1)
    artifact_field = "voided_revision" if current["lifecycle"] == "void" else "issued_revision"
    artifact_match = {"_id": current["_id"], "lifecycle": current["lifecycle"], artifact_field: artifact_revision}
    if (
        current.get("pdf_state") == "ready"
        and int(existing.get("issued_revision") or 0) == artifact_revision
        and re.fullmatch(r"[0-9a-f]{64}", str(existing.get("sha256") or ""))
        and all(existing.get(field) is not None for field in ("version", "byte_size", "renderer_version"))
    ):
        return {"state": "ready", "generated_at": existing.get("generated_at")}
    renderer_version = int(current.get("pdf_renderer_version") or RENDERER_VERSION)
    if renderer_version != RENDERER_VERSION:
        raise HTTPException(status_code=409, detail="This credit/debit note requires its original PDF renderer version")
    try:
        data = await asyncio.to_thread(render_native_adjustment_pdf, deepcopy(current))
        stored = await upload_native_invoice_pdf(
            data,
            org_id,
            adjustment_id,
            current.get("note_number") or adjustment_id,
            renderer_version=renderer_version,
            issued_revision=artifact_revision,
        )
    except Exception as exc:
        await adjustments.update_one(
            {**artifact_match, "$or": [{"pdf_state": {"$ne": "ready"}}, {"pdf.sha256": {"$exists": False}}]},
            {"$set": {"pdf_state": "failed", "pdf_error": type(exc).__name__, "updated_at": datetime.now(timezone.utc)}},
        )
        raise
    pushed = {"audit": _event(
        "adjustment_pdf_generated",
        actor_id,
        revision_before=int(current.get("revision") or 1),
        revision_after=int(current.get("revision") or 1),
        details={"sha256": stored.get("sha256"), "renderer_version": renderer_version},
    )}
    if existing:
        pushed["legacy_pdfs"] = {"pdf": existing, "replaced_at": datetime.now(timezone.utc), "replaced_by": actor_id}
    updated = await adjustments.find_one_and_update(
        {**artifact_match, "$or": [{"pdf_state": {"$ne": "ready"}}, {"pdf.sha256": {"$exists": False}}]},
        {"$set": {
            "pdf": stored,
            "pdf_state": "ready",
            "pdf_updated_by": actor_id,
            "pdf_renderer_version": renderer_version,
            "updated_at": datetime.now(timezone.utc),
        }, "$unset": {"pdf_error": ""}, "$push": pushed},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        updated = await get_adjustment(org_id, adjustment_id)
    return {"state": updated.get("pdf_state"), "generated_at": (updated.get("pdf") or {}).get("generated_at")}


async def adjustment_pdf_link(org_id: str, adjustment_id: str) -> dict:
    current = await get_adjustment(org_id, adjustment_id)
    pdf = current.get("pdf") or {}
    artifact_revision = int(current.get("voided_revision") or current.get("issued_revision") or 0)
    if current.get("lifecycle") not in {"issued", "void"} or current.get("pdf_state") != "ready" or int(pdf.get("issued_revision") or -1) != artifact_revision:
        raise HTTPException(status_code=409, detail="Credit/debit note PDF is not ready")
    try:
        url = native_invoice_pdf_download_url(pdf, expires_in_seconds=60)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Credit/debit note PDF metadata is incomplete") from exc
    return {"url": url, "expires_in": 60}
