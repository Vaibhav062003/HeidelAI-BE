import hashlib
import re
import secrets
import shlex
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    "reminders_enabled": False,
    "reminder_time": "10:00",
    "reminder_timezone": "Asia/Kolkata",
    "reminder_template_name": "",
    "reminder_template_language": "en",
}
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
INVALID_XML10_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
INVALID_XML10_REFERENCES = re.compile(
    r"&#(?:0*(?:[0-8]|1[12]|1[4-9]|2[0-9]|3[01])|x0*(?:[0-8bcef]|1[0-9a-f]));",
    re.IGNORECASE,
)
SNAPSHOT_KEYS = {"ledgers", "stock_items", "units", "invoices", "outstandings", "profit_loss"}


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


def valid_gstin_format(value: str) -> bool:
    return bool(GSTIN_PATTERN.fullmatch((value or "").strip().upper()))


def _tally_date(value: str) -> str:
    text = (value or "").strip()
    for pattern in ("%Y%m%d", "%d-%b-%Y", "%d-%b-%y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    return ""


def _financial_year_period(today: date | None = None) -> tuple[str, str]:
    current = today or date.today()
    start_year = current.year if current.month >= 4 else current.year - 1
    return date(start_year, 4, 1).isoformat(), current.isoformat()


def parse_invoice_command(command: str) -> dict:
    text = (command or "").strip()
    if not re.match(r"^/invoice(?:\s|$)", text, re.IGNORECASE):
        raise HTTPException(status_code=422, detail="Use /invoice <product> <quantity> <price>+")
    body = re.sub(r"^/invoice\b", "", text, count=1, flags=re.IGNORECASE).strip()
    try:
        tokens = shlex.split(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid /invoice command: {exc}") from exc
    compact_items, start = [], 0
    for index, token in enumerate(tokens):
        price_match = re.fullmatch(r"(?:₹)?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)\+", token)
        if not price_match:
            continue
        if index - start < 2:
            raise HTTPException(status_code=422, detail="Put product name and quantity before every price+")
        try:
            quantity = Decimal(tokens[index - 1].replace(",", ""))
            rate = Decimal(price_match.group(1).replace(",", ""))
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Check quantity or price near {token}") from exc
        model = " ".join(tokens[start:index - 1]).strip()
        if not model or quantity <= 0 or quantity > 10000 or rate <= 0 or rate > 1_000_000_000:
            raise HTTPException(status_code=422, detail=f"Use <product> <quantity> <price>+ near {token}")
        compact_items.append({
            "model": model, "quantity": str(quantity.normalize()), "rate": float(_money(rate)), "gst_inclusive": False,
        })
        start = index + 1
    if compact_items:
        if start != len(tokens) or len(compact_items) > 50:
            raise HTTPException(status_code=422, detail="Finish every product with quantity and price+")
        return {"items": compact_items}

    entries = [entry.strip() for entry in re.split(r"[;\r\n]+", body) if entry.strip()]
    if not entries or len(entries) > 50:
        raise HTTPException(status_code=422, detail="Add between 1 and 50 products after /invoice")
    items = []
    for entry in entries:
        try:
            parts = [part for part in shlex.split(entry) if part != "|"]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid /invoice product: {exc}") from exc
        quantity, rate, model = Decimal("1"), None, []
        for part in parts:
            key, separator, raw_value = part.partition("=")
            if separator and key.casefold() in {"qty", "quantity", "price", "rate"}:
                try:
                    value = Decimal(raw_value.replace(",", "").replace("₹", ""))
                except Exception as exc:
                    raise HTTPException(status_code=422, detail=f"Check quantity or price in: {entry}") from exc
                if key.casefold() in {"qty", "quantity"}:
                    quantity = value
                else:
                    rate = value
            else:
                model.append(part)
        model_text = " ".join(model).strip()
        if not model_text or quantity <= 0 or quantity > 10000 or rate is not None and (rate <= 0 or rate > 1_000_000_000):
            raise HTTPException(status_code=422, detail=f"Use <product> | qty=1 | price=45000 for: {entry}")
        item = {"model": model_text, "quantity": str(quantity.normalize())}
        if rate is not None:
            item["rate"] = float(_money(rate))
        items.append(item)
    return {"items": items}


def _export_collection_xml(settings: dict, collection: str, fetch: list[str], *, filters: str = "") -> str:
    settings = normalized_settings(settings)
    collection_id = f"HeidelAI{collection}"
    native_methods = "".join(f"<NATIVEMETHOD>{_xml(field)}</NATIVEMETHOD>" for field in fetch)
    filter_xml = f"<FILTER>{_xml(filters)}</FILTER>" if filters else ""
    formula_xml = (
        f"<SYSTEM TYPE=\"Formulae\" NAME=\"{_xml(filters)}\">$VoucherTypeName = \"{_xml(settings['voucher_type'])}\"</SYSTEM>"
        if filters else ""
    )
    tdl = (
        "<TDL><TDLMESSAGE>"
        f"<COLLECTION NAME=\"{collection_id}\" ISINITIALIZE=\"Yes\"><TYPE>{_xml(collection)}</TYPE>"
        f"{filter_xml}{native_methods}</COLLECTION>{formula_xml}"
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


def build_report_xml(settings: dict, report: str, from_date: str, to_date: str, export_format: str = "XML") -> str:
    settings = normalized_settings(settings)
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Data</TYPE><ID>{_xml(report)}</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{_xml(settings['company_name'])}</SVCURRENTCOMPANY>"
        f"<SVFROMDATE>{_xml(from_date.replace('-', ''))}</SVFROMDATE>"
        f"<SVTODATE>{_xml(to_date.replace('-', ''))}</SVTODATE>"
        f"<SVEXPORTFORMAT>$$SysName:{_xml(export_format)}</SVEXPORTFORMAT>"
        "</STATICVARIABLES></DESC></BODY></ENVELOPE>"
    )


def build_snapshot_requests(settings: dict) -> list[dict]:
    fy_from, fy_to = _financial_year_period()
    return [
        {"key": "ledgers", "xml": _export_collection_xml(settings, "Ledger", [
            "Name", "Parent", "GUID", "MasterID", "AlterID", "ClosingBalance", "OpeningBalance",
            "LedgerMobile", "LedgerPhone", "Email", "PartyGSTIN", "LedgerStateName", "GSTRegistrationType", "IsBillWiseOn",
        ])},
        {"key": "stock_items", "xml": _export_collection_xml(settings, "StockItem", [
            "Name", "Alias", "Parent", "GUID", "MasterID", "AlterID", "BaseUnits", "ClosingBalance",
            "ClosingValue", "StandardPrice", "GSTApplicable", "GSTTaxability", "GSTHSNName", "GSTIGSTRate",
        ])},
        {"key": "units", "xml": _export_collection_xml(settings, "Unit", [
            "Name", "GUID", "MasterID", "AlterID", "OriginalName", "GSTRepUOM",
        ])},
        {"key": "invoices", "xml": _export_collection_xml(settings, "Voucher", [
            "Date", "EffectiveDate", "VoucherNumber", "Reference", "VoucherTypeName", "PartyLedgerName", "GUID", "MasterID",
            "AlterID", "Amount", "AllLedgerEntries", "AllInventoryEntries",
        ], filters="HeidelAISalesVoucher")},
        {"key": "outstandings", "xml": build_report_xml(settings, "Bills Receivable", "1970-01-01", fy_to)},
        {"key": "profit_loss", "xml": build_report_xml(settings, "Profit and Loss", fy_from, fy_to)},
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


def _tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].upper()


def _direct_text(node: ET.Element, *names: str) -> str:
    wanted = {name.upper() for name in names}
    for child in node:
        if _tag(child) in wanted and child.text:
            return child.text.strip()
    return ""


def _parse_tally_xml(xml: str) -> ET.Element:
    text = INVALID_XML10_REFERENCES.sub("", xml or "")
    return ET.fromstring(INVALID_XML10_CHARACTERS.sub("", text))


def parse_outstanding_response(xml: str) -> list[dict]:
    try:
        root = _parse_tally_xml(xml)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Tally returned invalid outstandings XML") from exc
    fixed = [node for node in root.iter() if _tag(node) in {"BILLFIXED", "BILLOUTSTANDING"}]
    closing = ["".join(node.itertext()).strip() for node in root.iter() if _tag(node) in {"BILLCL", "PENDINGAMOUNT"}]
    rows = []
    for index, node in enumerate(fixed):
        reference = _node_text(node, "BILLREF", "REFERENCE", "NAME")
        party = _node_text(node, "BILLPARTY", "PARTYLEDGERNAME", "LEDGERNAME")
        amount_text = _node_text(node, "BILLCL", "PENDINGAMOUNT", "CLOSINGBALANCE")
        if not amount_text and index < len(closing):
            amount_text = closing[index]
        amount = float(abs(_decimal_text(amount_text)))
        if reference and party and amount > 0:
            rows.append({
                "bill_reference": reference,
                "party_ledger": party,
                "date": _tally_date(_node_text(node, "BILLDATE", "DATE")),
                "due_date": _tally_date(_node_text(node, "BILLDUE", "DUEDATE")),
                "outstanding_amount": amount,
            })
    return rows


def parse_profit_loss_response(xml: str) -> dict:
    try:
        root = _parse_tally_xml(xml)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Tally returned invalid Profit & Loss XML") from exc
    names = [
        " ".join((_node_text(node, "DSPDISPNAME") or "".join(node.itertext())).split())
        for node in root.iter() if _tag(node) == "DSPACCNAME"
    ]
    debits = [
        float(abs(_decimal_text("".join(node.itertext()))))
        for node in root.iter() if _tag(node) in {"DSPCLAMT", "DSPCLDRAMT"}
    ]
    credits = [
        float(abs(_decimal_text("".join(node.itertext()))))
        for node in root.iter() if _tag(node) in {"DSPCRAMT", "DSPCLCRAMT"}
    ]
    rows = []
    for index, label in enumerate(names):
        debit = debits[index] if index < len(debits) else 0.0
        credit = credits[index] if index < len(credits) else 0.0
        rows.append({"label": label, "debit": debit, "credit": credit, "amount": credit - debit})
    summary = {"sales": 0.0, "gross_profit": 0.0, "net_profit": 0.0}
    for row in rows:
        label = row["label"].casefold()
        value = row["credit"] or row["debit"] or abs(row["amount"])
        if not summary["sales"] and (label == "sales accounts" or label.startswith("sales")):
            summary["sales"] = value
        if "gross profit" in label or "gross loss" in label:
            summary["gross_profit"] = value if "profit" in label else -value
        if "net profit" in label or "nett profit" in label or "net loss" in label or "nett loss" in label:
            summary["net_profit"] = value if "profit" in label else -value
    return {"rows": rows, **summary}


def parse_snapshot_responses(responses: list[dict]) -> dict:
    parsed = {"ledgers": [], "stock_items": [], "units": [], "invoices": [], "outstandings": [], "profit_loss": {}}
    tags = {"ledgers": "LEDGER", "stock_items": "STOCKITEM", "units": "UNIT", "invoices": "VOUCHER"}
    for response in responses or []:
        key = response.get("key")
        if key == "outstandings":
            parsed[key] = parse_outstanding_response(response.get("xml") or "")
            continue
        if key == "profit_loss":
            parsed[key] = parse_profit_loss_response(response.get("xml") or "")
            continue
        if key not in tags:
            continue
        try:
            root = _parse_tally_xml(response.get("xml") or "")
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
                    "billwise": _node_text(node, "ISBILLWISEON").casefold() == "yes",
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
                amount = _decimal_text(_direct_text(node, "AMOUNT"))
                items = []
                bill_reference = ""
                due_date = ""
                for child in node.iter():
                    if _tag(child) == "ALLINVENTORYENTRIES.LIST":
                        items.append({
                            "name": _node_text(child, "STOCKITEMNAME"),
                            "quantity": float(abs(_decimal_text(_node_text(child, "BILLEDQTY", "ACTUALQTY")))),
                            "rate": float(abs(_decimal_text(_node_text(child, "RATE")))),
                            "amount": float(abs(_decimal_text(_direct_text(child, "AMOUNT")))),
                        })
                    elif _tag(child) == "BILLALLOCATIONS.LIST" and not bill_reference:
                        bill_reference = _node_text(child, "NAME")
                        due_date = _tally_date(_node_text(child, "BILLDUE", "DUEDATE"))
                invoice_date = _tally_date(_node_text(node, "EFFECTIVEDATE", "DATE"))
                parsed[key].append({**common,
                    "voucher_number": _node_text(node, "VOUCHERNUMBER"),
                    "date": invoice_date,
                    "reference": _node_text(node, "REFERENCE"),
                    "bill_reference": bill_reference or _node_text(node, "REFERENCE") or _node_text(node, "VOUCHERNUMBER"),
                    "due_date": due_date or invoice_date,
                    "voucher_type": _node_text(node, "VOUCHERTYPENAME"),
                    "party_ledger": _node_text(node, "PARTYLEDGERNAME"),
                    "amount": float(abs(amount)),
                    "items": items,
                })
    return parsed


def validate_snapshot_responses(responses: list[dict], parsed: dict) -> None:
    missing = SNAPSHOT_KEYS - {response.get("key") for response in responses or []}
    if missing:
        raise HTTPException(status_code=502, detail=f"Tally snapshot is incomplete: missing {', '.join(sorted(missing))}")
    # Every Tally company has built-in ledgers. An empty ledger export means the
    # collection request was not fulfilled and must not erase the last good cache.
    if not parsed.get("ledgers"):
        raise HTTPException(status_code=502, detail="Tally returned an empty ledger export; existing HeidelAI data was kept")


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
    ledger = await db.tally_ledgers.find_one({
        "org_id": org_id, "name": {"$regex": f"^{re.escape(job['party_ledger'])}$", "$options": "i"},
    }, {"master_id": 1})
    if ledger and ledger.get("master_id") and job.get("conversation_id", "").startswith("whatsapp_"):
        await db.tally_customer_links.update_one(
            {"org_id": org_id, "party_master_id": ledger["master_id"]},
            {"$set": {
                "party_ledger": job["party_ledger"], "conversation_id": job["conversation_id"],
                "phone": job["conversation_id"].removeprefix("whatsapp_"), "updated_at": datetime.now(timezone.utc),
                "updated_by": job.get("created_by") or "invoice",
            }},
            upsert=True,
        )


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


async def queue_tally_sync(
    org_id: str, user_id: str = "system", *, force: bool = False,
    force_new: bool = False, triggered_by_job_id: str | None = None,
) -> tuple[dict, bool]:
    integration = await get_integration(org_id)
    settings = normalized_settings(integration)
    if not settings["company_name"].strip() or not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Configure and connect TallyPrime first")
    if not force_new:
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
        "triggered_by_job_id": triggered_by_job_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return serialize_mongo(job), True


async def queue_tally_master(org_id: str, user_id: str, kind: str, value: dict, *, master_id: str = "") -> dict:
    integration = await get_integration(org_id)
    if not normalized_settings(integration)["company_name"].strip() or not integration.get("connector_key_hash"):
        raise HTTPException(status_code=409, detail="Configure and connect TallyPrime first")
    if kind == "party" and value.get("gstin"):
        value["gstin"] = str(value["gstin"]).strip().upper()
        if not valid_gstin_format(value["gstin"]):
            raise HTTPException(status_code=422, detail="Enter a valid 15-character GSTIN format")
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
    validate_snapshot_responses(responses, parsed)
    sync_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    previous_pdfs = {}
    async for row in db.tally_invoices.find(
        {"org_id": org_id, "pdf.url": {"$type": "string"}}, {"master_id": 1, "pdf": 1}
    ):
        if row.get("master_id"):
            previous_pdfs[row["master_id"]] = row.get("pdf")
    outstanding = {
        (row["party_ledger"].casefold(), row["bill_reference"].casefold()): row
        for row in parsed["outstandings"]
    }
    ledger_ids = {row["name"].casefold(): row.get("master_id", "") for row in parsed["ledgers"]}
    billwise_ledgers = {row["name"].casefold(): row.get("billwise") is True for row in parsed["ledgers"]}
    for invoice in parsed["invoices"]:
        invoice["invoice_id"] = invoice.get("master_id") or invoice.get("guid") or f"{invoice['voucher_number']}:{invoice['date']}"
        invoice["party_master_id"] = ledger_ids.get(invoice.get("party_ledger", "").casefold(), "")
        due = outstanding.get((invoice.get("party_ledger", "").casefold(), invoice.get("bill_reference", "").casefold()))
        invoice["outstanding_amount"] = float((due or {}).get("outstanding_amount") or 0)
        invoice["due_date"] = (due or {}).get("due_date") or invoice.get("due_date") or invoice.get("date")
        invoice["payment_status"] = (
            "overdue" if due and invoice["due_date"] and invoice["due_date"] < date.today().isoformat()
            else "outstanding" if due
            else "paid" if billwise_ledgers.get(invoice.get("party_ledger", "").casefold())
            else "unknown"
        )
        pdf = previous_pdfs.get(invoice.get("master_id"))
        if pdf and str(pdf.get("alter_id")) == str(invoice.get("alter_id")):
            invoice["pdf"] = pdf
    for key, collection in (("ledgers", db.tally_ledgers), ("stock_items", db.tally_stock_items), ("units", db.tally_units), ("invoices", db.tally_invoices), ("outstandings", db.tally_outstandings)):
        rows = [{**row, "org_id": org_id, "sync_id": sync_id, "synced_at": now} for row in parsed[key]]
        if rows:
            await collection.insert_many(rows)
        await collection.delete_many({"org_id": org_id, "sync_id": {"$ne": sync_id}})
    fy_from, fy_to = _financial_year_period()
    if parsed["profit_loss"].get("rows"):
        await db.tally_profit_loss_reports.update_one(
            {"org_id": org_id, "from_date": fy_from, "to_date": fy_to},
            {"$set": {**parsed["profit_loss"], "synced_at": now}},
            upsert=True,
        )
    counts = {key: len(value) for key, value in parsed.items() if isinstance(value, list)}
    await db.tally_integrations.update_one(
        {"org_id": org_id}, {"$set": {
            "last_sync_at": now, "last_sync_error": None, "last_sync_counts": counts,
        }}
    )
    await reconcile_default_payment_reminders(org_id)
    return {"counts": counts, "last_sync_at": now}


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


async def list_tally_invoices(
    org_id: str, page: int = 1, page_size: int = 50, query: str = "", status: str = "",
    from_date: str = "", to_date: str = "",
) -> dict:
    match: dict = {"org_id": org_id}
    if query.strip():
        pattern = {"$regex": re.escape(query.strip()), "$options": "i"}
        match["$or"] = [{"voucher_number": pattern}, {"party_ledger": pattern}, {"reference": pattern}]
    if status in {"paid", "outstanding", "overdue", "unknown"}:
        match["payment_status"] = status
    if from_date or to_date:
        match["date"] = {}
        if from_date:
            match["date"]["$gte"] = from_date
        if to_date:
            match["date"]["$lte"] = to_date
    total = await db.tally_invoices.count_documents(match)
    rows = await db.tally_invoices.find(match, {"_id": 0, "sync_id": 0}).sort([("date", -1), ("voucher_number", -1)]).skip((page - 1) * page_size).limit(page_size).to_list(length=page_size)
    invoice_ids = [row.get("invoice_id") for row in rows if row.get("invoice_id")]
    links = {row["party_master_id"]: row for row in await db.tally_customer_links.find(
        {"org_id": org_id, "party_master_id": {"$in": [row.get("party_master_id") for row in rows]}}, {"_id": 0}
    ).to_list(length=page_size)}
    reminders = {}
    async for reminder in db.tally_payment_reminders.find(
        {"org_id": org_id, "invoice_id": {"$in": invoice_ids}, "status": {"$in": ["pending", "needs_review"]}}, {"_id": 0}
    ).sort("scheduled_at", 1):
        reminders.setdefault(reminder["invoice_id"], reminder)
    for row in rows:
        row["customer_link"] = links.get(row.get("party_master_id"))
        row["next_reminder"] = reminders.get(row.get("invoice_id"))
    return serialize_mongo({"invoices": rows, "total": total, "page": page, "page_size": page_size})


async def tally_overview(org_id: str, include_financials: bool = False) -> dict:
    integration = await get_integration(org_id)
    customers = await db.tally_ledgers.count_documents({"org_id": org_id, "parent": "Sundry Debtors"})
    products = await db.tally_stock_items.count_documents({"org_id": org_id})
    low_stock = await db.tally_stock_items.count_documents({"org_id": org_id, "closing_quantity": {"$lte": 1}})
    fy_from, fy_to = _financial_year_period()
    report = await db.tally_profit_loss_reports.find_one({"org_id": org_id, "from_date": fy_from, "to_date": fy_to}, {"_id": 0}) or {}
    now = datetime.now(timezone.utc)
    last_seen = integration.get("last_seen_at")
    connected = isinstance(last_seen, datetime) and (
        last_seen.replace(tzinfo=timezone.utc) if last_seen.tzinfo is None else last_seen
    ) >= now - timedelta(seconds=90)
    receivable_totals = await db.tally_invoices.aggregate([
        {"$match": {"org_id": org_id, "payment_status": {"$in": ["outstanding", "overdue"]}}},
        {"$group": {"_id": None, "total": {"$sum": "$outstanding_amount"}}},
    ]).to_list(length=1)
    latest_sync = await db.tally_command_jobs.find_one(
        {"org_id": org_id, "job_type": "sync_snapshot"},
        {"_id": 0, "requests": 0, "responses": 0}, sort=[("created_at", -1)],
    )
    active_operations = await db.tally_command_jobs.count_documents({
        "org_id": org_id, "status": {"$in": ["pending", "processing"]},
    }) + await db.tally_bill_jobs.count_documents({
        "org_id": org_id, "status": {"$in": ["pending", "processing"]},
    })
    overview = {
        "connected": connected, "last_seen_at": last_seen, "last_sync_at": integration.get("last_sync_at"),
        "stale": _snapshot_is_stale(integration, now), "last_error": integration.get("last_sync_error"),
        "last_sync_counts": integration.get("last_sync_counts") or {},
        "sync_status": (latest_sync or {}).get("status"), "active_operations": active_operations,
        "receivables": float((receivable_totals[0] if receivable_totals else {}).get("total") or 0),
        "customers": customers, "products": products, "low_stock": low_stock,
        "invoices": await db.tally_invoices.count_documents({"org_id": org_id}),
    }
    if include_financials:
        overview.update({
            "sales": report.get("sales", 0), "gross_profit": report.get("gross_profit", 0),
            "net_profit": report.get("net_profit", 0), "report_from": fy_from, "report_to": fy_to,
        })
    return serialize_mongo(overview)


async def get_profit_loss_report(org_id: str, from_date: str, to_date: str) -> dict | None:
    row = await db.tally_profit_loss_reports.find_one(
        {"org_id": org_id, "from_date": from_date, "to_date": to_date}, {"_id": 0}
    )
    return serialize_mongo(row) if row else None


async def queue_profit_loss_report(org_id: str, user_id: str, from_date: str, to_date: str) -> tuple[dict, bool]:
    integration = await get_integration(org_id)
    if "reports" not in (integration.get("connector_capabilities") or []):
        raise HTTPException(status_code=409, detail="Download and restart the latest Tally connector to refresh reports")
    active = await db.tally_command_jobs.find_one({
        "org_id": org_id, "job_type": "profit_loss_report", "from_date": from_date, "to_date": to_date,
        "status": {"$in": ["pending", "processing"]},
    }, {"_id": 0, "requests": 0})
    if active:
        return serialize_mongo(active), False
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()), "job_type": "profit_loss_report", "org_id": org_id,
        "from_date": from_date, "to_date": to_date, "status": "pending", "attempts": 0,
        "requests": [{"key": "profit_loss", "xml": build_report_xml(integration, "Profit and Loss", from_date, to_date)}],
        "created_at": now, "updated_at": now, "created_by": user_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return serialize_mongo({key: value for key, value in job.items() if key != "requests"}), True


def _job_phase(job: dict, linked: dict | None = None, master_found: bool = True) -> tuple[str, str | None]:
    status = job.get("status")
    if status == "pending":
        return "queued", None
    if status == "processing":
        return ("reading_from_tally" if job.get("job_type") == "sync_snapshot" else "writing_to_tally"), None
    if status == "delivery_unknown":
        return "needs_verification", job.get("error")
    if status == "failed":
        return "failed", job.get("error")
    if status != "completed" or job.get("job_type") not in {"master_create", "master_update"} or not job.get("readback_job_id"):
        return "completed", None
    if not linked:
        return "failed", "The required Tally read-back job could not be found"
    if linked.get("status") in {"pending", "processing"}:
        return "reading_from_tally", None
    if linked.get("status") != "completed":
        return "failed", linked.get("error") or "Tally read-back failed"
    if not master_found:
        return "failed", f"Tally accepted {job.get('master', {}).get('name') or 'the master'}, but the read-back did not contain it"
    return "completed", None


async def _command_operation(org_id: str, job: dict, linked: dict | None = None) -> dict:
    found = True
    if job.get("job_type") in {"master_create", "master_update"} and linked and linked.get("status") == "completed":
        collection = {
            "stock_item": db.tally_stock_items,
            "unit": db.tally_units,
        }.get(job.get("master_type"), db.tally_ledgers)
        name = str(job.get("master", {}).get("name") or "").strip()
        found = bool(name and await collection.find_one({
            "org_id": org_id, "name": {"$regex": f"^{re.escape(name)}$", "$options": "i"},
        }, {"_id": 1}))
    phase, operation_error = _job_phase(job, linked, found)
    result = serialize_mongo(job)
    if linked and linked.get("sync"):
        result["sync"] = serialize_mongo(linked["sync"])
    result.update({"operation_phase": phase, "operation_error": operation_error})
    return result


async def get_tally_command_job(org_id: str, job_id: str) -> dict:
    job = await db.tally_command_jobs.find_one(
        {"org_id": org_id, "job_id": job_id}, {"_id": 0, "requests": 0, "responses": 0}
    )
    if not job:
        raise HTTPException(status_code=404, detail="Tally job not found")
    linked = None
    if job.get("readback_job_id"):
        linked = await db.tally_command_jobs.find_one(
            {"org_id": org_id, "job_id": job["readback_job_id"]}, {"_id": 0, "requests": 0, "responses": 0}
        )
    return await _command_operation(org_id, job, linked)


async def list_tally_operations(org_id: str, limit: int = 20) -> list[dict]:
    commands = await db.tally_command_jobs.find(
        {"org_id": org_id}, {"_id": 0, "requests": 0, "responses": 0}
    ).sort("created_at", -1).limit(limit * 2).to_list(length=limit * 2)
    readback_ids = [job.get("readback_job_id") for job in commands if job.get("readback_job_id")]
    linked = {
        job["job_id"]: job async for job in db.tally_command_jobs.find(
            {"org_id": org_id, "job_id": {"$in": readback_ids}}, {"_id": 0, "requests": 0, "responses": 0}
        )
    } if readback_ids else {}
    operations = []
    for job in commands:
        if job.get("triggered_by_job_id"):
            continue
        view = await _command_operation(org_id, job, linked.get(job.get("readback_job_id")))
        view["label"] = (
            str(job.get("master", {}).get("name") or job.get("master_type") or "Tally master")
            if job.get("job_type") in {"master_create", "master_update"}
            else "Sync Tally" if job.get("job_type") == "sync_snapshot"
            else "Profit & Loss" if job.get("job_type") == "profit_loss_report"
            else "Invoice PDF"
        )
        operations.append(view)
    bills = await db.tally_bill_jobs.find(
        {"org_id": org_id}, {"_id": 0, "request_xml": 0, "tally_response": 0, "customer": 0}
    ).sort("created_at", -1).limit(limit).to_list(length=limit)
    phases = {
        "pending": "queued", "processing": "writing_to_tally", "completed": "completed",
        "failed": "failed", "delivery_unknown": "needs_verification",
    }
    operations.extend({
        **serialize_mongo(job), "job_type": "invoice_create",
        "operation_phase": phases.get(job.get("status"), job.get("status")),
        "operation_error": job.get("error"),
        "label": f"Invoice {job.get('quote_number') or job.get('quote_id') or ''}".strip(),
    } for job in bills)
    operations.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
    return operations[:limit]


async def queue_invoice_pdf(org_id: str, user_id: str, invoice_id: str) -> tuple[dict, bool]:
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    pdf = invoice.get("pdf") or {}
    if pdf.get("url") and str(pdf.get("alter_id")) == str(invoice.get("alter_id")):
        return {"status": "completed", "pdf": pdf}, False
    raise HTTPException(
        status_code=409,
        detail="Export this invoice as PDF in TallyPrime, then upload it from HeidelAI's Invoices tab",
    )


def _validate_tally_pdf(data: bytes) -> None:
    if len(data) > 10_000_000 or not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Choose a valid PDF under 10 MB")


async def store_uploaded_tally_pdf(org_id: str, invoice_id: str, data: bytes) -> dict:
    _validate_tally_pdf(data)
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    from services.cloudinary_service import upload_tally_invoice_pdf
    pdf = {
        "url": await upload_tally_invoice_pdf(data, org_id, invoice_id, str(invoice.get("alter_id") or "current")),
        "alter_id": invoice.get("alter_id", ""),
        "generated_at": datetime.now(timezone.utc),
    }
    await db.tally_invoices.update_one({"_id": invoice["_id"]}, {"$set": {"pdf": pdf}})
    return serialize_mongo(pdf)


async def store_tally_pdf_artifact(org_id: str, job_id: str, claim_id: str, data: bytes) -> dict:
    _validate_tally_pdf(data)
    job = await db.tally_command_jobs.find_one({
        "org_id": org_id, "job_id": job_id, "job_type": "invoice_pdf", "status": "processing", "claim_id": claim_id,
    })
    if not job:
        raise HTTPException(status_code=409, detail="This Tally PDF claim is no longer active")
    from services.cloudinary_service import upload_tally_invoice_pdf
    url = await upload_tally_invoice_pdf(data, org_id, job["invoice_id"], str(job.get("invoice_alter_id") or "current"))
    pdf = {"url": url, "alter_id": job.get("invoice_alter_id", ""), "generated_at": datetime.now(timezone.utc)}
    await db.tally_invoices.update_one({"org_id": org_id, "invoice_id": job["invoice_id"]}, {"$set": {"pdf": pdf}})
    await db.tally_command_jobs.update_one({"_id": job["_id"], "claim_id": claim_id}, {"$set": {"pdf": pdf}})
    return serialize_mongo(pdf)


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


def _exact_product_master_id(candidates: list[dict], term: str) -> str:
    requested = term.casefold().strip()
    matches = [row for row in candidates if requested in {
        str(row.get("name") or "").casefold(), str(row.get("model_number") or "").casefold(),
        *(str(alias).casefold() for alias in row.get("aliases") or []),
    }]
    return str(matches[0].get("master_id") or "") if len(matches) == 1 else ""


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
        for request in parse_invoice_command(payload.get("command") or "")["items"]:
            candidates = await _product_candidates(org_id, request["model"])
            items.append({
                "requested": request["model"], "quantity": request["quantity"], "rate": request.get("rate"),
                "gst_inclusive": request.get("gst_inclusive", False),
                "candidates": candidates, "selected_master_id": _exact_product_master_id(candidates, request["model"]),
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
            selected_master_id = item.get("tally_master_id") or _exact_product_master_id(candidates, term)
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


def _default_reminder_time(due_date: str, days_after: int, settings: dict) -> datetime:
    try:
        zone = ZoneInfo(settings.get("reminder_timezone") or "Asia/Kolkata")
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Choose a valid reminder timezone") from exc
    try:
        hour, minute = map(int, str(settings.get("reminder_time") or "10:00").split(":"))
        local = datetime.combine(date.fromisoformat(due_date) + timedelta(days=days_after), dt_time(hour, minute), zone)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Choose a valid reminder date and time") from exc
    return local.astimezone(timezone.utc)


async def link_tally_customer(org_id: str, user_id: str, party_master_id: str, conversation_id: str) -> dict:
    party = await db.tally_ledgers.find_one({"org_id": org_id, "master_id": party_master_id, "parent": "Sundry Debtors"})
    conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
    if not party:
        raise HTTPException(status_code=404, detail="Tally customer ledger not found")
    if not conversation or not conversation_id.startswith("whatsapp_"):
        raise HTTPException(status_code=422, detail="Choose an existing WhatsApp customer")
    link = {
        "org_id": org_id, "party_master_id": party_master_id, "party_ledger": party["name"],
        "conversation_id": conversation_id, "phone": conversation_id.removeprefix("whatsapp_"),
        "updated_at": datetime.now(timezone.utc), "updated_by": user_id,
    }
    await db.tally_customer_links.update_one(
        {"org_id": org_id, "party_master_id": party_master_id}, {"$set": link}, upsert=True,
    )
    return serialize_mongo(link)


async def reconcile_default_payment_reminders(org_id: str) -> int:
    settings = normalized_settings(await get_integration(org_id))
    if not settings["reminders_enabled"] or not settings["reminder_template_name"]:
        await db.tally_payment_reminders.update_many(
            {"org_id": org_id, "source": "default", "status": "pending"},
            {"$set": {"status": "cancelled", "cancel_reason": "Default reminders disabled", "updated_at": datetime.now(timezone.utc)}},
        )
        return 0
    now = datetime.now(timezone.utc)
    await db.tally_payment_reminders.update_many(
        {"org_id": org_id, "source": "default", "status": "pending", "days_after": {"$nin": settings["reminder_days_after"]}},
        {"$set": {"status": "cancelled", "cancel_reason": "Default reminder rule changed", "updated_at": now}},
    )
    created = 0
    invoices = db.tally_invoices.find({
        "org_id": org_id, "payment_status": {"$in": ["outstanding", "overdue"]},
        "due_date": {"$type": "string", "$ne": ""}, "party_master_id": {"$type": "string", "$ne": ""},
    }, {"_id": 0})
    linked_ids = set(await db.tally_customer_links.distinct("party_master_id", {"org_id": org_id}))
    async for invoice in invoices:
        if invoice.get("party_master_id") not in linked_ids:
            continue
        for day in settings["reminder_days_after"]:
            try:
                scheduled_at = _default_reminder_time(invoice["due_date"], day, settings)
            except HTTPException:
                continue
            if scheduled_at <= now:
                continue
            key = f"{org_id}:{invoice['invoice_id']}:default:{day}"
            existing = await db.tally_payment_reminders.find_one({"default_key": key}, {"status": 1, "cancel_reason": 1})
            if existing:
                if existing.get("status") == "pending" or (
                    existing.get("status") == "cancelled" and str(existing.get("cancel_reason") or "").startswith("Default reminder")
                ):
                    await db.tally_payment_reminders.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {"scheduled_at": scheduled_at, "status": "pending", "updated_at": now}, "$unset": {"cancel_reason": "", "cancelled_at": ""}},
                    )
                continue
            try:
                await db.tally_payment_reminders.insert_one({
                    "reminder_id": str(uuid.uuid4()), "default_key": key, "org_id": org_id,
                    "invoice_id": invoice["invoice_id"], "source": "default", "days_after": day,
                    "scheduled_at": scheduled_at, "status": "pending", "created_at": now,
                    "created_by": "default-rule", "updated_at": now,
                })
                created += 1
            except DuplicateKeyError:
                pass
    return created


async def list_payment_reminders(org_id: str, invoice_id: str) -> list[dict]:
    rows = await db.tally_payment_reminders.find(
        {"org_id": org_id, "invoice_id": invoice_id}, {"_id": 0}
    ).sort("scheduled_at", 1).to_list(length=100)
    return serialize_mongo(rows)


async def create_payment_reminder(org_id: str, user_id: str, user_name: str, invoice_id: str, scheduled_at: datetime) -> dict:
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    if invoice.get("payment_status") == "unknown":
        raise HTTPException(status_code=409, detail="Enable bill-by-bill tracking for this Tally customer and sync again")
    if invoice.get("payment_status") == "paid" or float(invoice.get("outstanding_amount") or 0) <= 0:
        raise HTTPException(status_code=409, detail="This Tally invoice is already paid")
    if not await db.tally_customer_links.find_one({"org_id": org_id, "party_master_id": invoice.get("party_master_id")}):
        raise HTTPException(status_code=409, detail="Link this Tally party to a WhatsApp customer first")
    settings = normalized_settings(await get_integration(org_id))
    if not settings["reminder_template_name"]:
        raise HTTPException(status_code=409, detail="An admin must configure the payment reminder template first")
    scheduled_at = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at.tzinfo is None else scheduled_at.astimezone(timezone.utc)
    if scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Choose a reminder time in the future")
    now = datetime.now(timezone.utc)
    reminder = {
        "reminder_id": str(uuid.uuid4()), "org_id": org_id, "invoice_id": invoice_id,
        "source": "one_off", "scheduled_at": scheduled_at, "status": "pending",
        "created_at": now, "created_by": user_id, "created_by_name": user_name, "updated_at": now,
    }
    await queue_invoice_pdf(org_id, user_id, invoice_id)
    await db.tally_payment_reminders.insert_one(reminder)
    reminder.pop("_id", None)
    return serialize_mongo(reminder)


async def reschedule_payment_reminder(org_id: str, reminder_id: str, user_id: str, scheduled_at: datetime) -> dict:
    scheduled_at = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at.tzinfo is None else scheduled_at.astimezone(timezone.utc)
    if scheduled_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="Choose a reminder time in the future")
    row = await db.tally_payment_reminders.find_one_and_update(
        {"org_id": org_id, "reminder_id": reminder_id, "status": {"$in": ["pending", "needs_review", "failed"]}},
        {"$set": {"scheduled_at": scheduled_at, "status": "pending", "updated_at": datetime.now(timezone.utc), "updated_by": user_id}, "$unset": {"error": "", "claim_id": "", "claimed_at": ""}},
        return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=409, detail="Only an unsent reminder can be rescheduled")
    return serialize_mongo(row)


async def cancel_payment_reminder(org_id: str, reminder_id: str, user_id: str) -> dict:
    row = await db.tally_payment_reminders.find_one_and_update(
        {"org_id": org_id, "reminder_id": reminder_id, "status": {"$in": ["pending", "needs_review", "failed"]}},
        {"$set": {"status": "cancelled", "cancelled_at": datetime.now(timezone.utc), "cancelled_by": user_id, "updated_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if not row:
        raise HTTPException(status_code=409, detail="Only an unsent reminder can be cancelled")
    return serialize_mongo(row)


async def process_tally_payment_reminders() -> int:
    now = datetime.now(timezone.utc)
    due = await db.tally_payment_reminders.find({"status": "pending", "scheduled_at": {"$lte": now}}).sort("scheduled_at", 1).limit(50).to_list(length=50)
    processed = 0
    for item in due:
        claim_id = str(uuid.uuid4())
        claimed = await db.tally_payment_reminders.find_one_and_update(
            {"_id": item["_id"], "status": "pending"},
            {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            continue
        invoice = await db.tally_invoices.find_one({"org_id": claimed["org_id"], "invoice_id": claimed["invoice_id"]})
        integration = await get_integration(claimed["org_id"])
        error = None
        if not invoice:
            error = "The Tally invoice is no longer in the latest snapshot"
        elif invoice.get("payment_status") == "unknown":
            error = "Enable bill-by-bill tracking for this Tally customer and sync again"
        elif invoice.get("payment_status") == "paid" or float(invoice.get("outstanding_amount") or 0) <= 0:
            await db.tally_payment_reminders.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {"$set": {"status": "cancelled", "cancelled_at": now, "cancel_reason": "Paid in Tally", "updated_at": now}, "$unset": {"claim_id": "", "claimed_at": ""}},
            )
            processed += 1
            continue
        elif _snapshot_is_stale(integration, now):
            error = "Tally payment status is stale; sync before sending"
        link = await db.tally_customer_links.find_one({"org_id": claimed["org_id"], "party_master_id": (invoice or {}).get("party_master_id")}) if invoice else None
        if not error and not link:
            error = "The Tally party is not linked to a WhatsApp customer"
        settings = normalized_settings(integration)
        if not error and not settings["reminder_template_name"]:
            error = "Payment reminders are not configured"
        elif not error and claimed.get("source") == "default" and not settings["reminders_enabled"]:
            error = "Automatic payment reminders are disabled"
        pdf = (invoice or {}).get("pdf") or {}
        if not error and (not pdf.get("url") or str(pdf.get("alter_id")) != str(invoice.get("alter_id"))):
            try:
                await queue_invoice_pdf(claimed["org_id"], "payment-reminder", claimed["invoice_id"])
                waits = int(claimed.get("pdf_waits") or 0) + 1
                if waits <= 6:
                    await db.tally_payment_reminders.update_one(
                        {"_id": claimed["_id"], "claim_id": claim_id},
                        {"$set": {"status": "pending", "scheduled_at": now + timedelta(minutes=5), "pdf_waits": waits, "updated_at": now}, "$unset": {"claim_id": "", "claimed_at": ""}},
                    )
                    continue
            except HTTPException:
                pass
            error = "The Tally invoice PDF is not available"
        if error:
            await db.tally_payment_reminders.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {"$set": {"status": "needs_review", "error": error, "updated_at": now}, "$unset": {"claim_id": "", "claimed_at": ""}},
            )
            processed += 1
            continue
        organization = await db.organizations.find_one({"org_id": claimed["org_id"]}, {"wa_id": 1}) or {}
        template = {
            "name": settings["reminder_template_name"], "language": {"code": settings["reminder_template_language"]},
            "components": [
                {"type": "header", "parameters": [{"type": "document", "document": {"link": pdf["url"], "filename": f"Invoice-{invoice['voucher_number']}.pdf"}}]},
                {"type": "body", "parameters": [
                    {"type": "text", "parameter_name": "customer_name", "text": invoice.get("party_ledger") or "Customer"},
                    {"type": "text", "parameter_name": "invoice_number", "text": invoice.get("voucher_number") or invoice["invoice_id"]},
                    {"type": "text", "parameter_name": "outstanding_amount", "text": f"INR {float(invoice.get('outstanding_amount') or 0):,.2f}"},
                    {"type": "text", "parameter_name": "due_date", "text": invoice.get("due_date") or invoice.get("date") or ""},
                ]},
            ],
        }
        try:
            from services.wa_service import send_whatsapp_template_message
            response = await send_whatsapp_template_message(
                link["phone"], template, organization.get("wa_id"), claimed["org_id"],
                f"tally-reminder:{claimed['reminder_id']}", "reply", None, None, "Payment reminder",
            )
            await db.tally_payment_reminders.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {"$set": {"status": "sent", "sent_at": datetime.now(timezone.utc), "message_id": response.get("message_id"), "updated_at": datetime.now(timezone.utc)}, "$unset": {"claim_id": "", "claimed_at": "", "error": ""}},
            )
        except Exception as exc:
            await db.tally_payment_reminders.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {"$set": {"status": "failed", "error": str(exc)[:500], "updated_at": datetime.now(timezone.utc)}, "$unset": {"claim_id": "", "claimed_at": ""}},
            )
        processed += 1
    return processed


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
    await db.tally_invoices.create_index([("org_id", 1), ("invoice_id", 1)])
    await db.tally_invoices.create_index([("org_id", 1), ("date", -1)])
    await db.tally_invoices.create_index([("org_id", 1), ("payment_status", 1), ("due_date", 1)])
    await db.tally_customer_links.create_index([("org_id", 1), ("party_master_id", 1)], unique=True)
    await db.tally_payment_reminders.create_index("reminder_id", unique=True)
    await db.tally_payment_reminders.create_index("default_key", unique=True, sparse=True)
    await db.tally_payment_reminders.create_index([("status", 1), ("scheduled_at", 1)])
    await db.tally_profit_loss_reports.create_index([("org_id", 1), ("from_date", 1), ("to_date", 1)], unique=True)
    await db.tally_invoice_drafts.create_index("draft_id", unique=True)
    await db.tally_invoice_drafts.create_index([("org_id", 1), ("conversation_id", 1), ("created_at", -1)])


async def get_integration(org_id: str) -> dict:
    return await db.tally_integrations.find_one({"org_id": org_id}) or {"org_id": org_id}


async def save_integration(org_id: str, user_id: str, settings: dict) -> dict:
    settings = normalized_settings(settings)
    saved = await db.tally_integrations.find_one_and_update(
        {"org_id": org_id},
        {"$set": {**settings, "updated_at": datetime.now(timezone.utc), "updated_by": user_id}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    await reconcile_default_payment_reminders(org_id)
    return saved


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


async def authenticate_connector(raw_key: str | None, capabilities: str = "", version: str = "") -> dict:
    if not raw_key or len(raw_key) > 200:
        raise HTTPException(status_code=401, detail="Invalid Tally connector key")
    integration = await db.tally_integrations.find_one({"connector_key_hash": connector_key_hash(raw_key)})
    if not integration:
        raise HTTPException(status_code=401, detail="Invalid Tally connector key")
    now = datetime.now(timezone.utc)
    await db.tally_integrations.update_one(
        {"_id": integration["_id"], "connector_key_hash": integration["connector_key_hash"]},
        {"$set": {
            "last_seen_at": now, "connector_version": version[:40],
            "connector_capabilities": sorted({item.strip() for item in capabilities.split(",") if item.strip()}),
        }},
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
    response_meta = [{"key": item.get("key"), "bytes": len(item.get("xml") or "")} for item in result.get("responses") or []]
    command = await db.tally_command_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "job_id": job_id, "status": "processing", "claim_id": claim_id},
        {"$set": {
            "status": status,
            "error": error,
            "response_meta": response_meta,
            "result_at": now,
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": "", "responses": ""}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "requests": 0, "responses.xml": 0},
    )
    if command:
        if status == "completed" and command["job_type"] == "sync_snapshot":
            try:
                command["sync"] = await _store_tally_snapshot(integration["org_id"], result.get("responses") or [])
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id},
                    {"$set": {"sync": command["sync"], "updated_at": now}},
                )
            except Exception as exc:
                status = "failed"
                error = str(exc.detail) if isinstance(exc, HTTPException) else f"HeidelAI could not store the Tally snapshot: {type(exc).__name__}"
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id},
                    {"$set": {"status": status, "error": error, "updated_at": now}},
                )
                await db.tally_integrations.update_one(
                    {"org_id": integration["org_id"]}, {"$set": {"last_sync_error": error}}
                )
                command.update({"status": status, "error": error})
        elif status == "completed" and command["job_type"] == "profit_loss_report":
            response = next((item for item in result.get("responses") or [] if item.get("key") == "profit_loss"), None)
            try:
                report = parse_profit_loss_response((response or {}).get("xml") or "")
            except HTTPException:
                report = {}
            if not report.get("rows"):
                status, error = "failed", "Tally returned an empty Profit & Loss report"
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id}, {"$set": {"status": status, "error": error, "updated_at": now}}
                )
                command.update({"status": status, "error": error})
            else:
                await db.tally_profit_loss_reports.update_one(
                    {"org_id": integration["org_id"], "from_date": command["from_date"], "to_date": command["to_date"]},
                    {"$set": {**report, "synced_at": now}}, upsert=True,
                )
        elif status == "completed" and command["job_type"] == "invoice_pdf":
            stored = await db.tally_command_jobs.find_one({"org_id": integration["org_id"], "job_id": job_id}, {"pdf": 1})
            if not (stored or {}).get("pdf", {}).get("url"):
                status, error = "failed", "Tally PDF upload was not confirmed"
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id}, {"$set": {"status": status, "error": error, "updated_at": now}}
                )
                command.update({"status": status, "error": error})
        elif status == "completed" and command["job_type"] in {"master_create", "master_update"}:
            readback, _ = await queue_tally_sync(
                integration["org_id"], force=True, force_new=True, triggered_by_job_id=job_id,
            )
            command["readback_job_id"] = readback["job_id"]
            await db.tally_command_jobs.update_one(
                {"org_id": integration["org_id"], "job_id": job_id},
                {"$set": {"readback_job_id": readback["job_id"], "updated_at": now}},
            )
        if command["job_type"] == "sync_snapshot" and status != "completed":
            await db.tally_integrations.update_one(
                {"org_id": integration["org_id"]}, {"$set": {"last_sync_error": error}}
            )
        await db.tally_integrations.update_one(
            {"org_id": integration["org_id"]},
            {"$set": {"last_error": error, "last_result_at": now}},
        )
        return await get_tally_command_job(integration["org_id"], job_id)
    existing_command = await db.tally_command_jobs.find_one(
        {"org_id": integration["org_id"], "job_id": job_id}, {"_id": 0, "requests": 0, "responses": 0}
    )
    if existing_command:
        if existing_command.get("status") != status:
            raise HTTPException(status_code=409, detail="This Tally command was already completed with a different result")
        if status == "completed" and existing_command.get("job_type") in {"master_create", "master_update"} and not existing_command.get("readback_job_id"):
            readback, _ = await queue_tally_sync(
                integration["org_id"], force=True, force_new=True, triggered_by_job_id=job_id,
            )
            await db.tally_command_jobs.update_one(
                {"org_id": integration["org_id"], "job_id": job_id},
                {"$set": {"readback_job_id": readback["job_id"], "updated_at": now}},
            )
        return await get_tally_command_job(integration["org_id"], job_id)
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
