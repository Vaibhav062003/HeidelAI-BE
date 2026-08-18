import hashlib
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


db = get_async_mongo_db()
CLAIM_TIMEOUT = timedelta(minutes=15)
DEFAULT_SETTINGS = {
    "company_name": "",
    "voucher_type": "Sales",
    "sales_ledger": "Sales",
    "stock_unit": "Nos",
    "tax_mode": "cgst_sgst",
    "cgst_ledger": "CGST",
    "sgst_ledger": "SGST",
    "igst_ledger": "IGST",
    "shipping_ledger": "",
    "discount_ledger": "",
    "reminder_days_after": [0, 3, 7],
}


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _number(value: Decimal) -> str:
    return f"{value:.2f}"


def _xml(value) -> str:
    return escape(str(value or "").strip(), quote=True)


def connector_key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_remote_id(org_id: str, quote_id: str, revision: int) -> str:
    digest = hashlib.sha256(f"{org_id}:{quote_id}:{revision}".encode()).hexdigest()
    return f"heidelai-{digest[:40]}"


def normalized_settings(value: dict | None) -> dict:
    source = value or {}
    settings = {key: source.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
    settings["reminder_days_after"] = sorted({
        int(day) for day in settings.get("reminder_days_after", [])
        if isinstance(day, int) and 0 <= day <= 365
    })[:10]
    return settings


def _ledger(
    name: str,
    amount: Decimal,
    deemed_positive: bool = False,
    party: bool = False,
    bill_reference: str = "",
) -> str:
    bill_allocation = (
        "<BILLALLOCATIONS.LIST>"
        f"<NAME>{_xml(bill_reference)}</NAME><BILLTYPE>New Ref</BILLTYPE>"
        f"<AMOUNT>{_number(amount)}</AMOUNT>"
        "</BILLALLOCATIONS.LIST>"
        if party and bill_reference else ""
    )
    return (
        "<LEDGERENTRIES.LIST>"
        f"<LEDGERNAME>{_xml(name)}</LEDGERNAME>"
        f"<ISDEEMEDPOSITIVE>{'Yes' if deemed_positive else 'No'}</ISDEEMEDPOSITIVE>"
        f"<ISPARTYLEDGER>{'Yes' if party else 'No'}</ISPARTYLEDGER>"
        f"<AMOUNT>{_number(amount)}</AMOUNT>"
        f"{bill_allocation}"
        "</LEDGERENTRIES.LIST>"
    )


def build_sales_voucher_xml(quote: dict, settings: dict, party_ledger: str, tax_mode: str) -> str:
    settings = normalized_settings(settings)
    items = quote.get("items") or []
    if not items:
        raise HTTPException(status_code=422, detail="This quotation has no billable items")
    if tax_mode not in {"none", "cgst_sgst", "igst"}:
        raise HTTPException(status_code=422, detail="Choose how GST should be posted in Tally")

    subtotal = _money(quote.get("private_subtotal"))
    gst_total = _money(quote.get("private_gst_total"))
    shipping = _money(quote.get("shipping"))
    discount = _money(quote.get("discount"))
    grand_total = _money(quote.get("public_grand_total"))
    expected = subtotal + gst_total + shipping - discount
    if expected != grand_total:
        raise HTTPException(status_code=409, detail="The quotation totals no longer reconcile. Save it again before billing.")
    if shipping and not settings.get("shipping_ledger"):
        raise HTTPException(status_code=422, detail="Configure a shipping ledger before billing this quotation")
    if discount and not settings.get("discount_ledger"):
        raise HTTPException(status_code=422, detail="Configure a discount ledger before billing this quotation")
    if gst_total and tax_mode == "cgst_sgst" and (not settings.get("cgst_ledger") or not settings.get("sgst_ledger")):
        raise HTTPException(status_code=422, detail="Configure the CGST and SGST ledgers first")
    if gst_total and tax_mode == "igst" and not settings.get("igst_ledger"):
        raise HTTPException(status_code=422, detail="Configure the IGST ledger first")

    inventory = []
    inventory_total = Decimal("0.00")
    for item in items:
        quantity = Decimal(str(item.get("quantity") or 0))
        if quantity <= 0:
            raise HTTPException(status_code=422, detail="Every billed item needs a positive quantity")
        line_base = _money(item.get("private_line_subtotal"))
        line_gross = _money(item.get("public_line_total"))
        line_amount = line_gross if tax_mode == "none" else line_base
        inventory_total += line_amount
        rate = (line_amount / quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unit = settings["stock_unit"]
        inventory.append(
            "<ALLINVENTORYENTRIES.LIST>"
            f"<STOCKITEMNAME>{_xml(item.get('description'))}</STOCKITEMNAME>"
            "<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
            f"<RATE>{_number(rate)}/{_xml(unit)}</RATE>"
            f"<AMOUNT>{_number(line_amount)}</AMOUNT>"
            f"<ACTUALQTY>{quantity.normalize()} {_xml(unit)}</ACTUALQTY>"
            f"<BILLEDQTY>{quantity.normalize()} {_xml(unit)}</BILLEDQTY>"
            "<ACCOUNTINGALLOCATIONS.LIST>"
            f"<LEDGERNAME>{_xml(settings['sales_ledger'])}</LEDGERNAME>"
            "<ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>"
            f"<AMOUNT>{_number(line_amount)}</AMOUNT>"
            "</ACCOUNTINGALLOCATIONS.LIST>"
            "</ALLINVENTORYENTRIES.LIST>"
        )

    ledgers = [_ledger(
        party_ledger,
        -grand_total,
        deemed_positive=True,
        party=True,
        bill_reference=quote.get("quote_number") or quote["quote_id"],
    )]
    if tax_mode == "cgst_sgst" and gst_total:
        cgst = (gst_total / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ledgers.extend([
            _ledger(settings["cgst_ledger"], cgst),
            _ledger(settings["sgst_ledger"], gst_total - cgst),
        ])
    elif tax_mode == "igst" and gst_total:
        ledgers.append(_ledger(settings["igst_ledger"], gst_total))
    if shipping:
        ledgers.append(_ledger(settings["shipping_ledger"], shipping))
    if discount:
        ledgers.append(_ledger(settings["discount_ledger"], -discount, deemed_positive=True))

    ledger_total = inventory_total + (Decimal("0.00") if tax_mode == "none" else gst_total) + shipping - discount
    if ledger_total != grand_total:
        raise HTTPException(status_code=409, detail="The Tally ledger entries do not match the quotation total")

    issued = str(quote.get("issued_on") or date.today().isoformat()).replace("-", "")
    revision = int(quote.get("revision_number") or 1)
    remote_id = stable_remote_id(quote["org_id"], quote["quote_id"], revision)
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{_xml(settings['company_name'])}</SVCURRENTCOMPANY>"
        "</STATICVARIABLES></REQUESTDESC><REQUESTDATA>"
        "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\">"
        f"<VOUCHER REMOTEID=\"{remote_id}\" VCHTYPE=\"{_xml(settings['voucher_type'])}\" ACTION=\"Create\">"
        f"<DATE>{issued}</DATE><VOUCHERTYPENAME>{_xml(settings['voucher_type'])}</VOUCHERTYPENAME>"
        f"<VOUCHERNUMBER>{_xml(quote.get('quote_number'))}</VOUCHERNUMBER>"
        f"<PARTYLEDGERNAME>{_xml(party_ledger)}</PARTYLEDGERNAME>"
        f"<REFERENCE>{_xml(quote.get('quote_number'))}</REFERENCE>"
        "<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW><ISINVOICE>Yes</ISINVOICE>"
        "<OBJVIEW>Invoice Voucher View</OBJVIEW>"
        + "".join(inventory) + "".join(ledgers)
        + "</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


async def ensure_tally_indexes() -> None:
    await db.tally_integrations.create_index("org_id", unique=True)
    await db.tally_integrations.create_index("connector_key_hash", unique=True, sparse=True)
    await db.tally_bill_jobs.create_index("job_id", unique=True)
    await db.tally_bill_jobs.create_index(
        [("org_id", 1), ("quote_id", 1), ("quote_revision", 1)], unique=True
    )
    await db.tally_bill_jobs.create_index([("org_id", 1), ("conversation_id", 1), ("created_at", -1)])
    await db.tally_bill_jobs.create_index([("org_id", 1), ("status", 1), ("created_at", 1)])
    await db.tally_bill_jobs.create_index([("status", 1), ("claimed_at", 1)])


async def get_integration(org_id: str) -> dict:
    return await db.tally_integrations.find_one({"org_id": org_id}) or {"org_id": org_id}


async def save_integration(org_id: str, user_id: str, settings: dict) -> dict:
    settings = normalized_settings(settings)
    return await db.tally_integrations.find_one_and_update(
        {"org_id": org_id},
        {"$set": {**settings, "updated_at": datetime.now(timezone.utc), "updated_by": user_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def create_connector_key(org_id: str, user_id: str) -> str:
    raw = secrets.token_urlsafe(32)
    await db.tally_integrations.update_one(
        {"org_id": org_id},
        {"$set": {
            "connector_key_hash": connector_key_hash(raw),
            "connector_created_at": datetime.now(timezone.utc),
            "connector_created_by": user_id,
            "last_error": None,
        }},
        upsert=True,
    )
    return raw


async def authenticate_connector(raw_key: str | None) -> dict:
    if not raw_key or len(raw_key) > 200:
        raise HTTPException(status_code=401, detail="Invalid Tally connector key")
    integration = await db.tally_integrations.find_one({"connector_key_hash": connector_key_hash(raw_key)})
    if not integration:
        raise HTTPException(status_code=401, detail="Invalid Tally connector key")
    await db.tally_integrations.update_one(
        {"_id": integration["_id"], "connector_key_hash": integration["connector_key_hash"]},
        {"$set": {"last_seen_at": datetime.now(timezone.utc)}},
    )
    return integration


async def queue_bill(org_id: str, user_id: str, user_name: str, role: str, quote_id: str, payload: dict) -> tuple[dict, bool]:
    quote = await db[f"quotes_{org_id}"].find_one({"org_id": org_id, "quote_id": quote_id})
    if not quote:
        raise HTTPException(status_code=404, detail="Quotation not found")
    await require_conversation_access(org_id, quote["conversation_id"], user_id, role)
    integration = await get_integration(org_id)
    settings = normalized_settings(integration)
    if not settings["company_name"].strip():
        raise HTTPException(status_code=409, detail="Configure the Tally company in Settings first")
    if not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Connect the Windows Tally connector first")
    request_xml = build_sales_voucher_xml(quote, settings, payload["party_ledger"], payload["tax_mode"])
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()),
        "org_id": org_id,
        "conversation_id": quote["conversation_id"],
        "quote_id": quote_id,
        "quote_revision": int(quote.get("revision_number") or 1),
        "quote_number": quote.get("quote_number") or quote_id,
        "amount": float(_money(quote.get("public_grand_total"))),
        "party_ledger": payload["party_ledger"],
        "tax_mode": payload["tax_mode"],
        "status": "pending",
        "request_xml": request_xml,
        "due_date": quote.get("due_date"),
        "outstanding_amount": float(_money(quote.get("public_grand_total"))),
        "reminder_days_after": settings["reminder_days_after"],
        "created_at": now,
        "created_by": user_id,
        "created_by_name": user_name,
        "updated_at": now,
        "attempts": 0,
    }
    try:
        await db.tally_bill_jobs.insert_one(job)
        return serialize_mongo(job), True
    except DuplicateKeyError:
        existing = await db.tally_bill_jobs.find_one({
            "org_id": org_id,
            "quote_id": quote_id,
            "quote_revision": job["quote_revision"],
        })
        return serialize_mongo(existing), False


async def list_bills(org_id: str, conversation_id: str, user_id: str, role: str) -> list[dict]:
    await require_conversation_access(org_id, conversation_id, user_id, role)
    rows = await db.tally_bill_jobs.find(
        {"org_id": org_id, "conversation_id": conversation_id},
        {"_id": 0, "request_xml": 0, "connector_key_hash": 0, "tally_response": 0},
    ).sort("created_at", -1).to_list(length=100)
    return serialize_mongo(rows)


async def retry_bill(org_id: str, job_id: str, user_id: str, role: str) -> dict:
    job = await db.tally_bill_jobs.find_one({"org_id": org_id, "job_id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Tally bill not found")
    await require_conversation_access(org_id, job["conversation_id"], user_id, role)
    if job.get("status") not in {"failed", "delivery_unknown"}:
        raise HTTPException(status_code=409, detail="Only a failed or unconfirmed Tally bill can be retried")
    updated = await db.tally_bill_jobs.find_one_and_update(
        {"_id": job["_id"], "status": job["status"]},
        {"$set": {"status": "pending", "error": None, "updated_at": datetime.now(timezone.utc)},
         "$unset": {"claim_id": "", "claimed_at": ""}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "request_xml": 0, "tally_response": 0},
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This Tally bill changed. Refresh before retrying")
    return serialize_mongo(updated)


async def claim_next_job(integration: dict) -> dict | None:
    claim_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    job = await db.tally_bill_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "status": "pending"},
        {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now},
         "$inc": {"attempts": 1}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    return serialize_mongo(job) if job else None


async def finish_job(integration: dict, job_id: str, claim_id: str | None, result: dict) -> dict:
    if not claim_id:
        raise HTTPException(status_code=409, detail="Missing Tally claim")
    outcome = result["outcome"]
    status = {"success": "completed", "failed": "failed", "unknown": "delivery_unknown"}[outcome]
    now = datetime.now(timezone.utc)
    error = None if status == "completed" else (result.get("error") or "Tally did not confirm this invoice")[:500]
    updated = await db.tally_bill_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "job_id": job_id, "status": "processing", "claim_id": claim_id},
        {"$set": {
            "status": status,
            "voucher_number": result.get("voucher_number"),
            "master_id": result.get("master_id"),
            "error": error,
            "tally_response": (result.get("tally_response") or "")[:4000],
            "result_at": now,
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": ""}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "request_xml": 0, "tally_response": 0},
    )
    if not updated:
        existing = await db.tally_bill_jobs.find_one(
            {"org_id": integration["org_id"], "job_id": job_id},
            {"_id": 0, "request_xml": 0, "tally_response": 0},
        )
        if existing and existing.get("status") == status:
            return serialize_mongo(existing)
        raise HTTPException(status_code=409, detail="This Tally claim is no longer active")
    await db.tally_integrations.update_one(
        {"org_id": integration["org_id"]},
        {"$set": {"last_error": error, "last_result_at": now}},
    )
    return serialize_mongo(updated)


async def reconcile_stale_tally_jobs() -> int:
    now = datetime.now(timezone.utc)
    result = await db.tally_bill_jobs.update_many(
        {"status": "processing", "claimed_at": {"$lt": now - CLAIM_TIMEOUT}},
        {"$set": {
            "status": "delivery_unknown",
            "error": "The connector stopped before confirming the result. Verify Tally Day Book before retrying.",
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": ""}},
    )
    return result.modified_count
