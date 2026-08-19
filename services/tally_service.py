import hashlib
import re
import secrets
import shlex
import uuid
import xml.etree.ElementTree as ET
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
SNAPSHOT_STALE_AFTER = timedelta(minutes=15)
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


def parse_invoice_command(command: str) -> dict:
    try:
        parts = shlex.split((command or "").strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid /invoice command: {exc}") from exc
    if not parts or parts[0].lower() != "/invoice":
        raise HTTPException(status_code=422, detail="Use /invoice <model-or-SKU> [qty=<number>]")
    quantity = Decimal("1")
    model = []
    for part in parts[1:]:
        if part.lower().startswith("qty="):
            try:
                quantity = Decimal(part.split("=", 1)[1])
            except Exception as exc:
                raise HTTPException(status_code=422, detail="Invoice quantity must be a number") from exc
        else:
            model.append(part)
    model_text = " ".join(model).strip()
    if not model_text or quantity <= 0 or quantity > 10000:
        raise HTTPException(status_code=422, detail="Use /invoice <model-or-SKU> [qty=<positive number>]")
    return {"model": model_text, "quantity": str(quantity.normalize())}


def _export_collection_xml(settings: dict, collection: str, fetch: list[str], *, filters: str = "") -> str:
    settings = normalized_settings(settings)
    tdl = ""
    collection_id = collection
    if filters:
        collection_id = f"HeidelAI{collection}"
        tdl = (
            "<TDL><TDLMESSAGE>"
            f"<COLLECTION NAME=\"{collection_id}\"><TYPE>{_xml(collection)}</TYPE>"
            f"<FILTER>{_xml(filters)}</FILTER><FETCH>{_xml(','.join(fetch))}</FETCH></COLLECTION>"
            f"<SYSTEM TYPE=\"Formulae\" NAME=\"{_xml(filters)}\">$VoucherTypeName = \"{_xml(settings['voucher_type'])}\"</SYSTEM>"
            "</TDLMESSAGE></TDL>"
        )
    fetch_xml = "".join(f"<FETCH>{_xml(field)}</FETCH>" for field in fetch)
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>COLLECTION</TYPE><ID>{_xml(collection_id)}</ID></HEADER><BODY><DESC>"
        "<STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{_xml(settings['company_name'])}</SVCURRENTCOMPANY>"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        "</STATICVARIABLES>"
        f"<FETCHLIST>{fetch_xml}</FETCHLIST>{tdl}</DESC></BODY></ENVELOPE>"
    )


def build_snapshot_requests(settings: dict) -> list[dict]:
    return [
        {"key": "ledgers", "xml": _export_collection_xml(settings, "Ledger", [
            "Name", "Parent", "GUID", "MasterID", "AlterID", "ClosingBalance", "OpeningBalance",
            "LedgerMobile", "LedgerPhone", "Email", "PartyGSTIN", "LedgerStateName", "GSTRegistrationType",
        ])},
        {"key": "stock_items", "xml": _export_collection_xml(settings, "StockItem", [
            "Name", "Alias", "Parent", "GUID", "MasterID", "AlterID", "BaseUnits", "ClosingBalance",
            "ClosingValue", "StandardPrice", "GSTApplicable", "GSTTaxability", "GSTHSNName", "GSTIGSTRate",
        ])},
        {"key": "units", "xml": _export_collection_xml(settings, "Unit", [
            "Name", "GUID", "MasterID", "AlterID", "OriginalName", "GSTRepUOM",
        ])},
        {"key": "invoices", "xml": _export_collection_xml(settings, "Voucher", [
            "Date", "VoucherNumber", "Reference", "VoucherTypeName", "PartyLedgerName", "GUID", "MasterID",
            "AlterID", "Amount", "AllLedgerEntries", "AllInventoryEntries",
        ], filters="HeidelAISalesVoucher")},
    ]


def _node_text(node: ET.Element, *names: str) -> str:
    wanted = {name.upper() for name in names}
    for child in node.iter():
        tag = child.tag.rsplit("}", 1)[-1].upper()
        if tag in wanted and child.text:
            return child.text.strip()
    return ""


def _decimal_text(value: str) -> Decimal:
    match = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", value or "")
    return Decimal(match.group(0).replace(",", "")) if match else Decimal("0")


def parse_snapshot_responses(responses: list[dict]) -> dict:
    parsed = {"ledgers": [], "stock_items": [], "units": [], "invoices": []}
    tags = {"ledgers": "LEDGER", "stock_items": "STOCKITEM", "units": "UNIT", "invoices": "VOUCHER"}
    for response in responses or []:
        key = response.get("key")
        if key not in tags:
            continue
        try:
            root = ET.fromstring(response.get("xml") or "")
        except ET.ParseError as exc:
            raise HTTPException(status_code=502, detail=f"Tally returned invalid {key} XML") from exc
        seen = set()
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].upper() != tags[key]:
                continue
            name = _node_text(node, "NAME", "VOUCHERNUMBER") or node.attrib.get("NAME", "")
            master_id = _node_text(node, "MASTERID")
            identity = master_id or _node_text(node, "GUID") or name
            if not identity or identity in seen:
                continue
            seen.add(identity)
            common = {
                "name": name,
                "master_id": master_id,
                "guid": _node_text(node, "GUID"),
                "alter_id": _node_text(node, "ALTERID"),
            }
            if key == "ledgers":
                parsed[key].append({**common,
                    "parent": _node_text(node, "PARENT"),
                    "closing_balance": float(_decimal_text(_node_text(node, "CLOSINGBALANCE"))),
                    "opening_balance": float(_decimal_text(_node_text(node, "OPENINGBALANCE"))),
                    "phone": _node_text(node, "LEDGERMOBILE", "LEDGERPHONE"),
                    "email": _node_text(node, "EMAIL"),
                    "gstin": _node_text(node, "PARTYGSTIN"),
                    "state": _node_text(node, "LEDGERSTATENAME", "LEDSTATENAME"),
                    "registration_type": _node_text(node, "GSTREGISTRATIONTYPE"),
                })
            elif key == "stock_items":
                aliases = [value.strip() for value in _node_text(node, "ALIAS").split(",") if value.strip()]
                parsed[key].append({**common,
                    "aliases": aliases,
                    "model_number": aliases[0] if aliases else name,
                    "parent": _node_text(node, "PARENT"),
                    "unit": _node_text(node, "BASEUNITS"),
                    "closing_quantity": float(_decimal_text(_node_text(node, "CLOSINGBALANCE"))),
                    "closing_value": float(_decimal_text(_node_text(node, "CLOSINGVALUE"))),
                    "standard_rate": float(_decimal_text(_node_text(node, "STANDARDPRICE"))),
                    "hsn": _node_text(node, "GSTHSNNAME", "HSNCODE"),
                    "gst_rate": float(_decimal_text(_node_text(node, "GSTIGSTRATE"))),
                    "gst_applicable": _node_text(node, "GSTAPPLICABLE"),
                })
            elif key == "units":
                parsed[key].append({**common,
                    "formal_name": _node_text(node, "ORIGINALNAME"),
                    "uqc": _node_text(node, "GSTREPUOM"),
                })
            else:
                amount = _decimal_text(_node_text(node, "AMOUNT"))
                parsed[key].append({**common,
                    "voucher_number": _node_text(node, "VOUCHERNUMBER"),
                    "date": _node_text(node, "DATE"),
                    "reference": _node_text(node, "REFERENCE"),
                    "voucher_type": _node_text(node, "VOUCHERTYPENAME"),
                    "party_ledger": _node_text(node, "PARTYLEDGERNAME"),
                    "amount": float(abs(amount)),
                })
    return parsed


def _customer_snapshot(quote: dict, conversation: dict | None = None) -> dict:
    customer = quote.get("customer") or {}
    conversation = conversation or {}
    values = {
        "name": customer.get("name") or conversation.get("customer_name"),
        "phone": customer.get("phone") or conversation.get("customer_id"),
        "email": customer.get("email"),
        "gstin": customer.get("gstin"),
        "address": customer.get("address"),
    }
    return {key: str(value).strip() for key, value in values.items() if value}


def _tally_escalation_payload(job: dict, status: str, error: str | None) -> dict:
    customer = job.get("customer") or {}
    uncertain = status == "delivery_unknown"
    reason = "Tally invoice delivery needs verification" if uncertain else "Tally invoice creation failed"
    action = "Verify Tally Day Book before retrying" if uncertain else "Fix the missing Tally master or mapping, then retry"
    quote_number = job.get("quote_number") or job.get("quote_id") or "quotation"
    detail = error or "Tally did not confirm this invoice"
    return {
        "id": f"tally:{job['job_id']}",
        "user_name": customer.get("name") or "Customer",
        "customer_number": customer.get("phone") or job.get("conversation_id"),
        "customer_text": f"Quotation {quote_number} could not be invoiced in Tally. {detail}",
        "inferred_intent": "Tally invoice needs billing review",
        "priority": "high",
        "kind": "actionable",
        "metadata": {
            "reason": reason,
            "action": action,
            "conversation_id": job.get("conversation_id"),
            "tally_job_id": job.get("job_id"),
            "quote_id": job.get("quote_id"),
            "quote_number": quote_number,
            "party_ledger": job.get("party_ledger"),
            "amount": job.get("amount"),
            "error": detail,
            "customer": customer,
        },
    }


async def _sync_tally_escalation(org_id: str, job: dict) -> None:
    from services.team_service import create_ai_escalation, update_escalation_status

    if job.get("status") == "completed" and int(job.get("attempts") or 0) > 1:
        await update_escalation_status(org_id, f"tally:{job['job_id']}", "reviewed")
    elif job.get("status") in {"failed", "delivery_unknown"}:
        await create_ai_escalation(
            org_id,
            _tally_escalation_payload(job, job["status"], job.get("error")),
        )


async def _remember_customer_mapping(org_id: str, job: dict) -> None:
    mapping = {
        "party_ledger": job["party_ledger"],
        "customer": job.get("customer") or {},
        "updated_at": datetime.now(timezone.utc),
    }
    query = {"conversation_id": job["conversation_id"]}
    update = {"$set": {"tally_customer_mapping": mapping}}
    await db[f"conversations_{org_id}"].update_one(query, update)
    await db[f"contacts_{org_id}"].update_one(query, update)


async def get_customer_mapping(org_id: str, conversation_id: str) -> dict:
    conversation = await db[f"conversations_{org_id}"].find_one(
        {"conversation_id": conversation_id}, {"_id": 0, "tally_customer_mapping": 1}
    )
    mapping = (conversation or {}).get("tally_customer_mapping")
    if mapping:
        return serialize_mongo(mapping)
    latest = await db.tally_bill_jobs.find_one(
        {"org_id": org_id, "conversation_id": conversation_id, "status": "completed"},
        {"_id": 0, "party_ledger": 1, "customer": 1},
        sort=[("created_at", -1)],
    )
    return serialize_mongo({
        "party_ledger": (latest or {}).get("party_ledger", ""),
        "customer": (latest or {}).get("customer", {}),
    })


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
        unit = item.get("unit") or item.get("tally_unit") or settings["stock_unit"]
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
        f"<PARTYLEDGERNAME>{_xml(party_ledger)}</PARTYLEDGERNAME>"
        f"<REFERENCE>{_xml(quote.get('quote_number'))}</REFERENCE>"
        "<PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW><ISINVOICE>Yes</ISINVOICE>"
        "<OBJVIEW>Invoice Voucher View</OBJVIEW>"
        + "".join(inventory) + "".join(ledgers)
        + "</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def build_master_xml(
    kind: str, value: dict, *, action: str = "Create", current_name: str = "", settings: dict | None = None,
) -> str:
    if action not in {"Create", "Alter"}:
        raise HTTPException(status_code=422, detail="Unsupported Tally master action")
    name = str(value.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Master name is required")
    target = current_name or name
    if kind == "party":
        body = (
            f"<LEDGER NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            "<PARENT>Sundry Debtors</PARENT><ISBILLWISEON>Yes</ISBILLWISEON>"
            f"<LEDSTATENAME>{_xml(value.get('state'))}</LEDSTATENAME>"
            f"<PARTYGSTIN>{_xml(value.get('gstin'))}</PARTYGSTIN>"
            f"<GSTREGISTRATIONTYPE>{_xml(value.get('registration_type'))}</GSTREGISTRATIONTYPE>"
            f"<LEDGERMOBILE>{_xml(value.get('phone'))}</LEDGERMOBILE><EMAIL>{_xml(value.get('email'))}</EMAIL>"
            f"<ADDRESS.LIST TYPE=\"String\"><ADDRESS>{_xml(value.get('address'))}</ADDRESS></ADDRESS.LIST></LEDGER>"
        )
    elif kind == "sales":
        body = (
            f"<LEDGER NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            "<PARENT>Sales Accounts</PARENT><AFFECTSSTOCK>Yes</AFFECTSSTOCK>"
            f"<GSTAPPLICABLE>{_xml(value.get('gst_applicable') or 'Applicable')}</GSTAPPLICABLE></LEDGER>"
        )
    elif kind == "tax":
        duty_head = {"cgst": "Central Tax", "sgst": "State Tax", "igst": "Integrated Tax"}.get(
            str(value.get("tax_type") or "").lower()
        )
        if not duty_head:
            raise HTTPException(status_code=422, detail="Tax ledger type must be CGST, SGST, or IGST")
        body = (
            f"<LEDGER NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            "<PARENT>Duties &amp; Taxes</PARENT><TAXTYPE>GST</TAXTYPE>"
            f"<GSTDUTYHEAD>{duty_head}</GSTDUTYHEAD></LEDGER>"
        )
    elif kind == "charge":
        parent = value.get("parent") or "Indirect Expenses"
        if parent not in {"Direct Expenses", "Indirect Expenses", "Direct Incomes", "Indirect Incomes"}:
            raise HTTPException(status_code=422, detail="Choose a valid charge ledger group")
        body = (
            f"<LEDGER NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            f"<PARENT>{_xml(parent)}</PARENT><TYPEOFLEDGER>{_xml(value.get('ledger_type') or 'Not Applicable')}</TYPEOFLEDGER>"
            "</LEDGER>"
        )
    elif kind == "unit":
        body = (
            f"<UNIT NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            "<ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>"
            f"<ORIGINALNAME>{_xml(value.get('formal_name') or name)}</ORIGINALNAME>"
            f"<GSTREPUOM>{_xml(value.get('uqc'))}</GSTREPUOM></UNIT>"
        )
    elif kind == "stock_item":
        body = (
            f"<STOCKITEM NAME=\"{_xml(target)}\" ACTION=\"{action}\"><NAME>{_xml(name)}</NAME>"
            f"<PARENT>{_xml(value.get('parent') or 'Primary')}</PARENT>"
            f"<ALIAS>{_xml(value.get('model_number'))}</ALIAS><BASEUNITS>{_xml(value.get('unit'))}</BASEUNITS>"
            f"<GSTAPPLICABLE>{_xml(value.get('gst_applicable') or 'Applicable')}</GSTAPPLICABLE>"
            f"<GSTHSNNAME>{_xml(value.get('hsn'))}</GSTHSNNAME>"
            f"<STANDARDPRICELIST.LIST><DATE>{date.today().strftime('%Y%m%d')}</DATE>"
            f"<RATE>{_number(_money(value.get('standard_rate')))}/{_xml(value.get('unit'))}</RATE>"
            "</STANDARDPRICELIST.LIST></STOCKITEM>"
        )
    else:
        raise HTTPException(status_code=422, detail="Unsupported Tally master type")
    company = _xml(normalized_settings(settings)["company_name"]) if settings else ""
    static_variables = f"<STATICVARIABLES><SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES>" if company else ""
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        f"<REQUESTDESC><REPORTNAME>All Masters</REPORTNAME>{static_variables}</REQUESTDESC><REQUESTDATA>"
        f"<TALLYMESSAGE xmlns:UDF=\"TallyUDF\">{body}</TALLYMESSAGE>"
        "</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
    )


def _snapshot_is_stale(integration: dict, now: datetime | None = None) -> bool:
    value = integration.get("last_sync_at")
    if not isinstance(value, datetime):
        return True
    value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return value < (now or datetime.now(timezone.utc)) - SNAPSHOT_STALE_AFTER


async def queue_tally_sync(org_id: str, user_id: str = "system", *, force: bool = False) -> tuple[dict, bool]:
    integration = await get_integration(org_id)
    settings = normalized_settings(integration)
    if not settings["company_name"].strip() or not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Configure and connect TallyPrime first")
    active = await db.tally_command_jobs.find_one({
        "org_id": org_id, "job_type": "sync_snapshot", "status": {"$in": ["pending", "processing"]},
    }, {"_id": 0})
    if active:
        return serialize_mongo(active), False
    if not force and not _snapshot_is_stale(integration):
        return {"status": "completed", "last_sync_at": serialize_mongo(integration.get("last_sync_at"))}, False
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()), "job_type": "sync_snapshot", "org_id": org_id,
        "status": "pending", "requests": build_snapshot_requests(settings), "attempts": 0,
        "created_at": now, "updated_at": now, "created_by": user_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return serialize_mongo(job), True


async def queue_tally_master(org_id: str, user_id: str, kind: str, value: dict, *, master_id: str = "") -> dict:
    integration = await get_integration(org_id)
    if not normalized_settings(integration)["company_name"].strip() or not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Configure and connect TallyPrime first")
    collection = {
        "stock_item": db.tally_stock_items,
        "unit": db.tally_units,
    }.get(kind, db.tally_ledgers)
    current = None
    if master_id:
        current = await collection.find_one({"org_id": org_id, "master_id": master_id})
        if not current:
            raise HTTPException(status_code=404, detail="Tally master not found in the latest snapshot")
    else:
        existing = await collection.find_one({"org_id": org_id, "name": {"$regex": f"^{re.escape(value['name'])}$", "$options": "i"}})
        if existing:
            raise HTTPException(status_code=409, detail=f"{existing['name']} already exists in Tally")
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()), "job_type": "master_update" if current else "master_create",
        "master_type": kind, "org_id": org_id, "status": "pending", "attempts": 0,
        "requests": [{"key": "master", "xml": build_master_xml(
            kind, value, action="Alter" if current else "Create", current_name=(current or {}).get("name", ""),
            settings=integration,
        )}],
        "master": value, "master_id": master_id or None, "created_at": now, "updated_at": now,
        "created_by": user_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return serialize_mongo(job)


async def _store_tally_snapshot(org_id: str, responses: list[dict]) -> dict:
    parsed = parse_snapshot_responses(responses)
    sync_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    for key, collection in (
        ("ledgers", db.tally_ledgers), ("stock_items", db.tally_stock_items),
        ("units", db.tally_units), ("invoices", db.tally_invoices),
    ):
        rows = [{**row, "org_id": org_id, "sync_id": sync_id, "synced_at": now} for row in parsed[key]]
        if rows:
            await collection.insert_many(rows)
        await collection.delete_many({"org_id": org_id, "sync_id": {"$ne": sync_id}})
    await db.tally_integrations.update_one(
        {"org_id": org_id}, {"$set": {"last_sync_at": now, "last_sync_error": None}}
    )
    return {"counts": {key: len(value) for key, value in parsed.items()}, "last_sync_at": now}


async def list_tally_ledgers(org_id: str, kind: str = "", query: str = "") -> list[dict]:
    match: dict = {"org_id": org_id}
    parents = {
        "party": ["Sundry Debtors"], "sales": ["Sales Accounts"], "tax": ["Duties & Taxes"],
        "charge": ["Direct Expenses", "Indirect Expenses", "Direct Incomes", "Indirect Incomes"],
    }
    if kind in parents:
        match["parent"] = {"$in": parents[kind]}
    if query.strip():
        match["name"] = {"$regex": re.escape(query.strip()), "$options": "i"}
    rows = await db.tally_ledgers.find(match, {"_id": 0, "sync_id": 0}).sort("name", 1).to_list(length=500)
    return serialize_mongo(rows)


async def list_tally_stock_items(org_id: str, query: str = "") -> list[dict]:
    match: dict = {"org_id": org_id}
    if query.strip():
        pattern = {"$regex": re.escape(query.strip()), "$options": "i"}
        match["$or"] = [{"name": pattern}, {"model_number": pattern}, {"aliases": pattern}]
    rows = await db.tally_stock_items.find(match, {"_id": 0, "sync_id": 0}).sort("name", 1).to_list(length=500)
    return serialize_mongo(rows)


async def list_tally_units(org_id: str) -> list[dict]:
    rows = await db.tally_units.find(
        {"org_id": org_id}, {"_id": 0, "sync_id": 0}
    ).sort("name", 1).to_list(length=500)
    return serialize_mongo(rows)


async def list_tally_invoices(org_id: str) -> list[dict]:
    rows = await db.tally_invoices.find({"org_id": org_id}, {"_id": 0, "sync_id": 0}).sort("date", -1).to_list(length=500)
    return serialize_mongo(rows)


async def tally_overview(org_id: str) -> dict:
    integration = await get_integration(org_id)
    party_ledgers = await db.tally_ledgers.find(
        {"org_id": org_id, "parent": "Sundry Debtors"}, {"_id": 0, "closing_balance": 1}
    ).to_list(length=5000)
    stock = await db.tally_stock_items.find(
        {"org_id": org_id}, {"_id": 0, "closing_quantity": 1}
    ).to_list(length=5000)
    now = datetime.now(timezone.utc)
    last_seen = integration.get("last_seen_at")
    connected = isinstance(last_seen, datetime) and (
        last_seen.replace(tzinfo=timezone.utc) if last_seen.tzinfo is None else last_seen
    ) >= now - timedelta(seconds=90)
    return serialize_mongo({
        "connected": connected, "last_seen_at": last_seen, "last_sync_at": integration.get("last_sync_at"),
        "stale": _snapshot_is_stale(integration, now), "last_error": integration.get("last_sync_error") or integration.get("last_error"),
        "receivables": sum(abs(float(row.get("closing_balance") or 0)) for row in party_ledgers),
        "customers": len(party_ledgers), "products": len(stock),
        "low_stock": sum(1 for row in stock if float(row.get("closing_quantity") or 0) <= 1),
        "invoices": await db.tally_invoices.count_documents({"org_id": org_id}),
    })


async def _product_candidates(org_id: str, term: str) -> list[dict]:
    pattern = re.escape(term.strip())
    rows = await db.tally_stock_items.find({
        "org_id": org_id,
        "$or": [
            {"name": {"$regex": pattern, "$options": "i"}},
            {"model_number": {"$regex": pattern, "$options": "i"}},
            {"aliases": {"$regex": pattern, "$options": "i"}},
        ],
    }, {"_id": 0, "sync_id": 0}).limit(8).to_list(length=8)
    normalized = term.casefold().strip()
    rows.sort(key=lambda row: 0 if normalized in {
        str(row.get("name") or "").casefold(), str(row.get("model_number") or "").casefold(),
        *(str(alias).casefold() for alias in row.get("aliases") or []),
    } else 1)
    return serialize_mongo(rows)


async def _party_candidates(org_id: str, customer: dict, mapped_ledger: str = "") -> list[dict]:
    conditions = []
    if mapped_ledger:
        conditions.append({"name": {"$regex": f"^{re.escape(mapped_ledger)}$", "$options": "i"}})
    if customer.get("gstin"):
        conditions.append({"gstin": {"$regex": f"^{re.escape(customer['gstin'])}$", "$options": "i"}})
    if customer.get("phone"):
        digits = re.sub(r"\D", "", customer["phone"])[-10:]
        if digits:
            conditions.append({"phone": {"$regex": re.escape(digits[-7:]), "$options": "i"}})
    if customer.get("name"):
        conditions.append({"name": {"$regex": f"^{re.escape(customer['name'])}$", "$options": "i"}})
    if not conditions:
        return []
    rows = await db.tally_ledgers.find(
        {"org_id": org_id, "parent": "Sundry Debtors", "$or": conditions},
        {"_id": 0, "sync_id": 0},
    ).limit(8).to_list(length=8)
    return serialize_mongo(rows)


async def create_tally_invoice_draft(
    org_id: str, user_id: str, role: str, conversation_id: str, payload: dict,
) -> dict:
    conversation = await require_conversation_access(org_id, conversation_id, user_id, role)
    source = payload.get("source")
    items = []
    quote = None
    if source == "command":
        request = parse_invoice_command(payload.get("command") or "")
        items.append({
            "requested": request["model"], "quantity": request["quantity"],
            "candidates": await _product_candidates(org_id, request["model"]), "selected_master_id": "",
        })
        reference = f"HDL-{uuid.uuid4().hex[:8].upper()}"
    elif source == "quote":
        quote = await db[f"quotes_{org_id}"].find_one({
            "org_id": org_id, "conversation_id": conversation_id, "quote_id": payload.get("quote_id"),
        })
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        for item in quote.get("items") or []:
            term = item.get("sku") or item.get("description") or ""
            candidates = await _product_candidates(org_id, term)
            selected_master_id = item.get("tally_master_id") or ""
            if selected_master_id and not any(row.get("master_id") == selected_master_id for row in candidates):
                selected = await db.tally_stock_items.find_one(
                    {"org_id": org_id, "master_id": selected_master_id}, {"_id": 0, "sync_id": 0}
                )
                if selected:
                    candidates.insert(0, serialize_mongo(selected))
            items.append({
                "requested": term, "quantity": str(Decimal(str(item.get("quantity") or 1)).normalize()),
                "candidates": candidates,
                "selected_master_id": selected_master_id,
                "rate": float(_money(item.get("private_unit_price"))),
                "gst_rate": float(_money(item.get("gst_rate"))),
                "gst_inclusive": item.get("gst_inclusive") is True,
            })
        reference = quote.get("quote_number") or quote["quote_id"]
    else:
        raise HTTPException(status_code=422, detail="Invoice draft source must be command or quote")
    customer = _customer_snapshot(quote or {}, conversation)
    mapping = await get_customer_mapping(org_id, conversation_id)
    now = datetime.now(timezone.utc)
    draft = {
        "draft_id": str(uuid.uuid4()), "org_id": org_id, "conversation_id": conversation_id,
        "source": source, "reference": reference, "quote_id": (quote or {}).get("quote_id"),
        "quote_revision": int((quote or {}).get("revision_number") or 1), "customer": customer,
        "party_candidates": await _party_candidates(org_id, customer, mapping.get("party_ledger") or ""),
        "party_master_id": "", "items": items, "status": "needs_review",
        "shipping": float(_money((quote or {}).get("shipping"))),
        "discount": float(_money((quote or {}).get("discount"))),
        "created_by": user_id, "created_at": now, "updated_at": now,
    }
    await db.tally_invoice_drafts.insert_one(draft)
    draft.pop("_id", None)
    missing_products = [item["requested"] for item in items if not item["candidates"]]
    if missing_products or not draft["party_candidates"]:
        missing = []
        if not draft["party_candidates"]:
            missing.append("no Tally customer ledger matched")
        if missing_products:
            missing.append(f"no Tally stock item matched {', '.join(missing_products)}")
        await _sync_tally_escalation(org_id, {
            "job_id": f"draft:{draft['draft_id']}", "status": "failed", "attempts": 0,
            "conversation_id": conversation_id, "quote_id": draft.get("quote_id"),
            "quote_number": reference, "party_ledger": "", "amount": 0,
            "customer": customer, "error": "; ".join(missing),
        })
    return serialize_mongo(draft)


async def review_tally_invoice_draft(org_id: str, user_id: str, role: str, draft_id: str, payload: dict) -> dict:
    draft = await db.tally_invoice_drafts.find_one({"org_id": org_id, "draft_id": draft_id})
    if not draft:
        raise HTTPException(status_code=404, detail="Invoice draft not found")
    await require_conversation_access(org_id, draft["conversation_id"], user_id, role)
    party = await db.tally_ledgers.find_one({
        "org_id": org_id, "master_id": payload.get("party_master_id"), "parent": "Sundry Debtors",
    }, {"_id": 0, "sync_id": 0})
    if not party:
        raise HTTPException(status_code=422, detail="Choose a Tally customer ledger")
    sales_ledger = str(payload.get("sales_ledger") or "").strip()
    if not await db.tally_ledgers.find_one({"org_id": org_id, "name": sales_ledger, "parent": "Sales Accounts"}):
        raise HTTPException(status_code=422, detail="Choose a Tally sales ledger")
    tax_mode = payload.get("tax_mode")
    if tax_mode not in {"none", "cgst_sgst", "igst"}:
        raise HTTPException(status_code=422, detail="Choose the GST posting")
    items = []
    subtotal = Decimal("0")
    gst_total = Decimal("0")
    for selected in payload.get("items") or []:
        product = await db.tally_stock_items.find_one({
            "org_id": org_id, "master_id": selected.get("master_id"),
        }, {"_id": 0, "sync_id": 0})
        if not product:
            raise HTTPException(status_code=422, detail="Choose every Tally product")
        quantity = Decimal(str(selected.get("quantity") or 0))
        rate = _money(selected.get("rate"))
        gst_rate = _money(selected.get("gst_rate", product.get("gst_rate")))
        if quantity <= 0 or quantity > 10000 or rate <= 0 or gst_rate < 0 or gst_rate > 100:
            raise HTTPException(status_code=422, detail="Check product quantity, price, and GST rate")
        if Decimal(str(product.get("closing_quantity") or 0)) < quantity:
            raise HTTPException(status_code=409, detail=f"Insufficient Tally stock for {product['name']}")
        gross = (rate * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        inclusive = selected.get("gst_inclusive") is True
        base = gross if tax_mode == "none" else (
            (gross / (Decimal("1") + gst_rate / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if inclusive and gst_rate else gross
        )
        tax = Decimal("0") if tax_mode == "none" else (
            gross - base if inclusive else (base * gst_rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        subtotal += base
        gst_total += tax
        items.append({
            "master_id": product["master_id"], "guid": product.get("guid"), "alter_id": product.get("alter_id"),
            "name": product["name"], "model_number": product.get("model_number"), "unit": product.get("unit"),
            "available_quantity": product.get("closing_quantity"), "quantity": str(quantity.normalize()),
            "rate": float(rate), "gst_rate": float(gst_rate), "gst_inclusive": inclusive,
            "base_amount": float(base), "gst_amount": float(tax), "line_total": float(base + tax),
        })
    if not items:
        raise HTTPException(status_code=422, detail="Add at least one reviewed Tally product")
    shipping = _money(draft.get("shipping"))
    discount = _money(draft.get("discount"))
    total = subtotal + gst_total + shipping - discount
    now = datetime.now(timezone.utc)
    updated = await db.tally_invoice_drafts.find_one_and_update(
        {"_id": draft["_id"], "status": {"$in": ["needs_review", "ready"]}},
        {"$set": {
            "party_master_id": party["master_id"], "party_ledger": party["name"], "items": items,
            "sales_ledger": sales_ledger, "tax_mode": tax_mode, "subtotal": float(subtotal),
            "gst_total": float(gst_total), "total": float(total), "status": "ready",
            "reviewed_by": user_id, "reviewed_at": now, "updated_at": now,
        }},
        return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This invoice draft is no longer editable")
    from services.team_service import update_escalation_status
    await update_escalation_status(org_id, f"tally:draft:{draft_id}", "reviewed")
    return serialize_mongo(updated)


async def submit_tally_invoice_draft(
    org_id: str, user_id: str, user_name: str, role: str, draft_id: str,
) -> tuple[dict, bool]:
    draft = await db.tally_invoice_drafts.find_one({"org_id": org_id, "draft_id": draft_id})
    if not draft:
        raise HTTPException(status_code=404, detail="Invoice draft not found")
    conversation = await require_conversation_access(org_id, draft["conversation_id"], user_id, role)
    if draft.get("status") == "queued" and draft.get("job_id"):
        existing = await db.tally_bill_jobs.find_one({"org_id": org_id, "job_id": draft["job_id"]}, {"_id": 0})
        return serialize_mongo(existing), False
    if draft.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Review every invoice field before creating it in Tally")
    integration = await get_integration(org_id)
    now = datetime.now(timezone.utc)
    last_seen = integration.get("last_seen_at")
    if not isinstance(last_seen, datetime) or (last_seen.replace(tzinfo=timezone.utc) if last_seen.tzinfo is None else last_seen) < now - timedelta(seconds=90):
        raise HTTPException(status_code=409, detail="Tally connector is offline")
    if _snapshot_is_stale(integration, now):
        raise HTTPException(status_code=409, detail="Refresh Tally data before creating this invoice")
    quote_items = []
    for item in draft["items"]:
        current = await db.tally_stock_items.find_one({"org_id": org_id, "master_id": item["master_id"]})
        quantity = Decimal(item["quantity"])
        if not current or Decimal(str(current.get("closing_quantity") or 0)) < quantity:
            raise HTTPException(status_code=409, detail=f"Refresh or correct the stock for {item['name']}")
        quote_items.append({
            "description": current["name"], "quantity": str(quantity.normalize()),
            "unit": current.get("unit"), "private_line_subtotal": item["base_amount"],
            "public_line_total": item["line_total"],
        })
    quote_id = draft.get("quote_id") or f"draft:{draft_id}"
    quote = {
        "org_id": org_id, "quote_id": quote_id, "revision_number": draft.get("quote_revision") or 1,
        "quote_number": draft["reference"], "issued_on": date.today().isoformat(),
        "items": quote_items, "private_subtotal": draft["subtotal"], "private_gst_total": draft["gst_total"],
        "shipping": draft.get("shipping") or 0, "discount": draft.get("discount") or 0,
        "public_grand_total": draft["total"], "customer": draft.get("customer") or {},
    }
    settings = {**normalized_settings(integration), "sales_ledger": draft["sales_ledger"]}
    request_xml = build_sales_voucher_xml(quote, settings, draft["party_ledger"], draft["tax_mode"])
    job = {
        "job_id": str(uuid.uuid4()), "org_id": org_id, "conversation_id": draft["conversation_id"],
        "quote_id": quote_id, "quote_revision": int(draft.get("quote_revision") or 1),
        "quote_number": draft["reference"], "draft_id": draft_id, "amount": float(_money(draft["total"])),
        "customer": draft.get("customer") or _customer_snapshot({}, conversation), "party_ledger": draft["party_ledger"],
        "tax_mode": draft["tax_mode"], "status": "pending", "request_xml": request_xml,
        "outstanding_amount": float(_money(draft["total"])), "reminder_days_after": settings["reminder_days_after"],
        "created_at": now, "created_by": user_id, "created_by_name": user_name, "updated_at": now, "attempts": 0,
    }
    try:
        await db.tally_bill_jobs.insert_one(job)
        stored, created = job, True
    except DuplicateKeyError:
        stored = await db.tally_bill_jobs.find_one({
            "org_id": org_id, "quote_id": quote_id, "quote_revision": job["quote_revision"],
        })
        created = False
    await db.tally_invoice_drafts.update_one(
        {"_id": draft["_id"]}, {"$set": {"status": "queued", "job_id": stored["job_id"], "updated_at": now}}
    )
    return serialize_mongo(stored), created


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
    await db.tally_command_jobs.create_index("job_id", unique=True)
    await db.tally_command_jobs.create_index([("org_id", 1), ("status", 1), ("created_at", 1)])
    await db.tally_command_jobs.create_index([("status", 1), ("claimed_at", 1)])
    await db.tally_ledgers.create_index([("org_id", 1), ("name", 1)])
    await db.tally_stock_items.create_index([("org_id", 1), ("name", 1)])
    await db.tally_units.create_index([("org_id", 1), ("name", 1)])
    await db.tally_invoices.create_index([("org_id", 1), ("date", -1)])
    await db.tally_invoice_drafts.create_index("draft_id", unique=True)
    await db.tally_invoice_drafts.create_index([("org_id", 1), ("conversation_id", 1), ("created_at", -1)])


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
    now = datetime.now(timezone.utc)
    await db.tally_integrations.update_one(
        {"_id": integration["_id"], "connector_key_hash": integration["connector_key_hash"]},
        {"$set": {"last_seen_at": now}},
    )
    integration["last_seen_at"] = now
    if _snapshot_is_stale(integration, now):
        await queue_tally_sync(integration["org_id"], force=True)
    return integration


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
    job = await db.tally_command_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "status": "pending"},
        {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now},
         "$inc": {"attempts": 1}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if job:
        return serialize_mongo(job)
    job = await db.tally_bill_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "status": "pending"},
        {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now},
         "$inc": {"attempts": 1}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if job:
        job["job_type"] = "invoice_create"
        job["requests"] = [{"key": "invoice", "xml": job.pop("request_xml")}]
    return serialize_mongo(job) if job else None


async def finish_job(integration: dict, job_id: str, claim_id: str | None, result: dict) -> dict:
    if not claim_id:
        raise HTTPException(status_code=409, detail="Missing Tally claim")
    outcome = result["outcome"]
    status = {"success": "completed", "failed": "failed", "unknown": "delivery_unknown"}[outcome]
    now = datetime.now(timezone.utc)
    error = None if status == "completed" else (result.get("error") or "Tally did not confirm this invoice")[:500]
    command = await db.tally_command_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "job_id": job_id, "status": "processing", "claim_id": claim_id},
        {"$set": {
            "status": status,
            "error": error,
            "responses": result.get("responses") or [],
            "result_at": now,
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": ""}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "requests": 0, "responses.xml": 0},
    )
    if command:
        if status == "completed" and command["job_type"] == "sync_snapshot":
            try:
                command["sync"] = await _store_tally_snapshot(integration["org_id"], result.get("responses") or [])
            except HTTPException as exc:
                status, error = "failed", str(exc.detail)
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id},
                    {"$set": {"status": status, "error": error, "updated_at": now}},
                )
                command.update({"status": status, "error": error})
        elif status == "completed" and command["job_type"] in {"master_create", "master_update"}:
            await queue_tally_sync(integration["org_id"], force=True)
        await db.tally_integrations.update_one(
            {"org_id": integration["org_id"]},
            {"$set": {"last_error": error, "last_result_at": now}},
        )
        return serialize_mongo(command)
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
            if status == "completed":
                await _remember_customer_mapping(integration["org_id"], existing)
                await queue_tally_sync(integration["org_id"], force=True)
            if existing.get("draft_id"):
                await db.tally_invoice_drafts.update_one(
                    {"org_id": integration["org_id"], "draft_id": existing["draft_id"]},
                    {"$set": {
                        "status": status, "voucher_number": existing.get("voucher_number"),
                        "error": existing.get("error"), "updated_at": now,
                    }},
                )
            await _sync_tally_escalation(integration["org_id"], existing)
            return serialize_mongo(existing)
        raise HTTPException(status_code=409, detail="This Tally claim is no longer active")
    await db.tally_integrations.update_one(
        {"org_id": integration["org_id"]},
        {"$set": {"last_error": error, "last_result_at": now}},
    )
    if status == "completed":
        await _remember_customer_mapping(integration["org_id"], updated)
        await queue_tally_sync(integration["org_id"], force=True)
    if updated.get("draft_id"):
        await db.tally_invoice_drafts.update_one(
            {"org_id": integration["org_id"], "draft_id": updated["draft_id"]},
            {"$set": {
                "status": status, "voucher_number": updated.get("voucher_number"),
                "error": error, "updated_at": now,
            }},
        )
    await _sync_tally_escalation(integration["org_id"], updated)
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
    commands = await db.tally_command_jobs.update_many(
        {"status": "processing", "claimed_at": {"$lt": now - CLAIM_TIMEOUT}},
        {"$set": {
            "status": "failed",
            "error": "The connector stopped before confirming the Tally operation. Retry it from HeidelAI.",
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": ""}},
    )
    return result.modified_count + commands.modified_count
