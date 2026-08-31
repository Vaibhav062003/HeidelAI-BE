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
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.entitlement_service import feature_access, require_feature
from services.utils import normalize_phone, serialize_mongo
from services.websocket_service import broadcast_org_admin_ws
from services.work_team_service import require_conversation_access


db = get_async_mongo_db()
CLAIM_TIMEOUT = timedelta(minutes=15)
SNAPSHOT_STALE_AFTER = timedelta(minutes=10)
CONTACT_SYNC_STALE_AFTER = timedelta(minutes=15)
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
    "reminder_template_parameter_format": "named",
}
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
INVALID_XML10_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
INVALID_XML10_REFERENCES = re.compile(
    r"&#(?:0*(?:[0-8]|1[12]|1[4-9]|2[0-9]|3[01])|x0*(?:[0-8bcef]|1[0-9a-f]));",
    re.IGNORECASE,
)
SNAPSHOT_KEYS = {"ledgers", "stock_items", "units", "invoices", "credit_notes", "outstandings", "profit_loss"}
TALLY_OUTCOME_MAX_ATTEMPTS = 5
TALLY_OUTCOME_CLAIM_TIMEOUT = timedelta(minutes=5)


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
    if settings["reminder_template_parameter_format"] not in {"named", "positional"}:
        settings["reminder_template_parameter_format"] = "named"
    return settings


def _supports_invoice_readback(integration: dict) -> bool:
    version = str(integration.get("connector_version") or "")
    return version.isdigit() and int(version) >= 8 and "invoice_readback" in (integration.get("connector_capabilities") or [])


def valid_gstin_format(value: str) -> bool:
    return bool(GSTIN_PATTERN.fullmatch((value or "").strip().upper()))


def _normalized_phone(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _strict_party_matches(parties: list[dict], gstin: str = "", phone: str = "") -> list[dict]:
    identifiers = []
    gstin = str(gstin or "").strip().upper()
    phone = _normalized_phone(phone)
    if gstin:
        identifiers.append({
            str(party.get("master_id") or "") for party in parties
            if str(party.get("gstin") or "").strip().upper() == gstin
        })
    if phone:
        identifiers.append({
            str(party.get("master_id") or "") for party in parties
            if phone in {_normalized_phone(value) for value in party.get("phones") or [party.get("phone")]}
        })
    identifiers = [matches - {""} for matches in identifiers if matches]
    if not identifiers:
        return []
    matched_ids = set.intersection(*identifiers)
    return [party for party in parties if str(party.get("master_id") or "") in matched_ids]


def _contact_match_id(
    party: dict,
    master_index: dict[str, set],
    gstin_index: dict[str, set],
    phone_index: dict[str, set],
) -> tuple[object | None, str]:
    """Choose one safe existing contact without guessing across conflicting identities."""
    master_id = str(party.get("master_id") or "")
    master_matches = master_index.get(master_id, set())
    if len(master_matches) == 1:
        return next(iter(master_matches)), "master_id"
    if len(master_matches) > 1:
        return None, "ambiguous"

    gstin = str(party.get("gstin") or "").strip().upper()
    phone = _normalized_phone(party.get("phone") or "")
    gstin_matches = gstin_index.get(gstin, set()) if gstin else set()
    phone_matches = phone_index.get(phone, set()) if phone else set()
    if len(gstin_matches) > 1 or (not gstin_matches and len(phone_matches) > 1):
        return None, "ambiguous"
    gstin_id = next(iter(gstin_matches)) if gstin_matches else None
    phone_id = next(iter(phone_matches)) if len(phone_matches) == 1 else None
    if gstin_id is not None and phone_id is not None and gstin_id != phone_id:
        return None, "ambiguous"
    if gstin_id is not None:
        return gstin_id, "gstin"
    if phone_id is not None:
        return phone_id, "phone"
    return None, "new"


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
    tokens = body.split()
    entries = [entry.strip() for entry in re.split(r"[;\r\n]+", body) if entry.strip()]
    compact_items, start = [], 0
    if len(entries) == 1:
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

    if not entries or len(entries) > 50:
        raise HTTPException(status_code=422, detail="Add between 1 and 50 products after /invoice")
    compact_entries = []
    for entry in entries:
        parts = [part for part in entry.split() if part != "|"]
        price_match = re.fullmatch(r"(?:₹)?((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(\+)?", parts[-1]) if len(parts) >= 3 else None
        try:
            quantity = Decimal(parts[-2].replace(",", "")) if price_match else None
        except Exception:
            quantity = None
        if not price_match or quantity is None or quantity <= 0 or quantity > 10000:
            compact_entries = []
            continue
        rate = Decimal(price_match.group(1).replace(",", ""))
        model = " ".join(parts[:-2]).strip()
        if not model or rate <= 0 or rate > 1_000_000_000:
            raise HTTPException(status_code=422, detail=f"Use <product> <quantity> <price> for: {entry}")
        compact_entries.append({
            "model": model, "quantity": str(quantity.normalize()), "rate": float(_money(rate)),
            "gst_inclusive": price_match.group(2) is None,
        })
    if len(compact_entries) == len(entries):
        return {"items": compact_entries}
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


def _export_collection_xml(
    settings: dict, collection: str, fetch: list[str], *, filters: str = "", formula: str = "",
) -> str:
    settings = normalized_settings(settings)
    collection_id = f"HeidelAI{collection}"
    native_methods = "".join(f"<NATIVEMETHOD>{_xml(field)}</NATIVEMETHOD>" for field in fetch)
    filter_xml = f"<FILTER>{_xml(filters)}</FILTER>" if filters else ""
    formula_text = formula or f'$VoucherTypeName = "{settings["voucher_type"]}"'
    formula_xml = (
        f"<SYSTEM TYPE=\"Formulae\" NAME=\"{_xml(filters)}\">{_xml(formula_text)}</SYSTEM>"
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


def _invoice_export_xml(settings: dict, master_id: str, invoice_date: str = "") -> str:
    settings = normalized_settings(settings)
    fetch = [
        "Date", "EffectiveDate", "VoucherNumber", "Reference", "VoucherTypeName", "PartyLedgerName",
        "PartyGSTIN", "StateName", "PlaceOfSupply", "BasicBuyerName", "BuyerName", "BasicBuyerAddress",
        "Address", "BuyerPinNumber", "BillToPlace", "ConsigneeMailingName", "ConsigneeGSTIN",
        "ConsigneeStateName", "ConsigneePinNumber", "ShipToPlace", "DispatchFromName",
        "DispatchFromAddress", "DispatchFromStateName", "DispatchFromPinCode", "DispatchFromPlace",
        "Narration", "GUID", "MasterID", "AlterID", "Amount",
        "AllLedgerEntries", "AllInventoryEntries",
    ]
    native_methods = "".join(f"<NATIVEMETHOD>{field}</NATIVEMETHOD>" for field in fetch)
    fetch_list = "".join(f"<FETCH>{field}</FETCH>" for field in fetch)
    tally_date = (invoice_date or "").replace("-", "")
    period = (
        f"<SVFROMDATE>{_xml(tally_date)}</SVFROMDATE><SVTODATE>{_xml(tally_date)}</SVTODATE>"
        if re.fullmatch(r"\d{8}", tally_date) else ""
    )
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        "<TYPE>COLLECTION</TYPE><ID>HeidelAIInvoice</ID></HEADER><BODY><DESC><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{_xml(settings['company_name'])}</SVCURRENTCOMPANY>{period}"
        "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES>"
        f"<FETCHLIST>{fetch_list}</FETCHLIST><TDL><TDLMESSAGE>"
        f"<COLLECTION NAME=\"HeidelAIInvoice\" ISINITIALIZE=\"Yes\"><TYPE>Voucher</TYPE>"
        f"<FILTER>HeidelAIInvoiceMaster</FILTER>{native_methods}</COLLECTION>"
        f"<SYSTEM TYPE=\"Formulae\" NAME=\"HeidelAIInvoiceMaster\">$MasterID = {master_id}</SYSTEM>"
        "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
    )


def build_invoice_export_xml(settings: dict, master_id: str, invoice_date: str = "") -> str:
    if not re.fullmatch(r"\d+", str(master_id or "").strip()):
        raise HTTPException(status_code=422, detail="Tally invoice is missing a numeric Master ID")
    return _invoice_export_xml(settings, str(int(master_id)), invoice_date)


def build_invoice_readback_xml(settings: dict, invoice_date: str = "") -> str:
    return _invoice_export_xml(settings, "__HEIDELAI_MASTER_ID__", invoice_date)


def build_snapshot_requests(settings: dict) -> list[dict]:
    fy_from, fy_to = _financial_year_period()
    return [
        {"key": "ledgers", "xml": _export_collection_xml(settings, "Ledger", [
            "Name", "Parent", "GUID", "MasterID", "AlterID", "ClosingBalance", "OpeningBalance",
            "LedgerMobile", "LedgerPhone", "Email", "PartyGSTIN", "GSTIN", "LedGSTRegDetails",
            "LedgerStateName", "GSTRegistrationType", "IsBillWiseOn", "MailingName", "Address", "Pincode",
        ])},
        {"key": "stock_items", "xml": _export_collection_xml(settings, "StockItem", [
            "Name", "Alias", "Parent", "GUID", "MasterID", "AlterID", "BaseUnits", "ClosingBalance",
            "ClosingValue", "StandardPrice", "Description", "BatchAllocations", "GSTApplicable", "GSTTaxability", "GSTHSNName", "GSTIGSTRate",
        ])},
        {"key": "units", "xml": _export_collection_xml(settings, "Unit", [
            "Name", "GUID", "MasterID", "AlterID", "OriginalName", "GSTRepUOM",
        ])},
        {"key": "invoices", "xml": _export_collection_xml(settings, "Voucher", [
            "Date", "EffectiveDate", "VoucherNumber", "Reference", "VoucherTypeName", "PartyLedgerName", "GUID", "MasterID",
            "AlterID", "Amount", "IsCancelled", "IsOptional", "IsPostDated", "AllLedgerEntries", "AllInventoryEntries",
        ], filters="HeidelAISalesVoucher", formula="$$IsSales:$VoucherTypeName")},
        {"key": "credit_notes", "xml": _export_collection_xml(settings, "Voucher", [
            "Date", "EffectiveDate", "VoucherNumber", "Reference", "VoucherTypeName", "PartyLedgerName", "GUID", "MasterID",
            "AlterID", "Amount", "IsCancelled", "IsOptional", "IsPostDated", "AllLedgerEntries", "AllInventoryEntries",
        ], filters="HeidelAICreditNote", formula="$$IsCreditNote:$VoucherTypeName")},
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
    parsed = {"ledgers": [], "stock_items": [], "units": [], "invoices": [], "credit_notes": [], "outstandings": [], "profit_loss": {}}
    tags = {"ledgers": "LEDGER", "stock_items": "STOCKITEM", "units": "UNIT", "invoices": "VOUCHER", "credit_notes": "VOUCHER"}
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
        line_error = _node_text(root, "LINEERROR")
        if line_error or _node_text(root, "STATUS") == "0" or (_node_text(root, "ERRORS").isdigit() and int(_node_text(root, "ERRORS")) > 0):
            raise HTTPException(status_code=502, detail=line_error or f"Tally rejected the {key} export")
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
                phones = list(dict.fromkeys(filter(None, [
                    _node_text(node, "LEDGERMOBILE"), _node_text(node, "LEDGERPHONE"),
                ])))
                addresses = list(dict.fromkeys(
                    child.text.strip() for child in node.iter()
                    if _tag(child) == "ADDRESS" and child.text and child.text.strip()
                ))
                parsed[key].append({**common,
                    "parent": _node_text(node, "PARENT"),
                    "closing_balance": float(_decimal_text(_node_text(node, "CLOSINGBALANCE"))),
                    "opening_balance": float(_decimal_text(_node_text(node, "OPENINGBALANCE"))),
                    "phone": phones[0] if phones else "",
                    "phones": phones,
                    "email": _node_text(node, "EMAIL"),
                    "gstin": _node_text(node, "PARTYGSTIN", "GSTIN"),
                    "state": _node_text(node, "LEDGERSTATENAME", "LEDSTATENAME"),
                    "mailing_name": _node_text(node, "MAILINGNAME") or name,
                    "address": ", ".join(addresses),
                    "pincode": _node_text(node, "PINCODE"),
                    "registration_type": _node_text(node, "GSTREGISTRATIONTYPE"),
                    "billwise": _node_text(node, "ISBILLWISEON").casefold() == "yes",
                })
            elif key == "stock_items":
                aliases = [value.strip() for value in _node_text(node, "ALIAS").split(",") if value.strip()]
                allocations = []
                for allocation in node.iter():
                    if _tag(allocation) != "BATCHALLOCATIONS.LIST":
                        continue
                    quantity = float(_decimal_text(_node_text(allocation, "CLOSINGBALANCE", "ACTUALQTY", "BILLEDQTY")))
                    allocations.append({
                        "godown": _node_text(allocation, "GODOWNNAME") or "Main Location",
                        "batch": _node_text(allocation, "BATCHNAME") or "Primary Batch",
                        "quantity": abs(quantity),
                    })
                parsed[key].append({**common,
                    "aliases": aliases,
                    "model_number": aliases[0] if aliases else name,
                    "parent": _node_text(node, "PARENT"),
                    "unit": _node_text(node, "BASEUNITS"),
                    "closing_quantity": float(_decimal_text(_node_text(node, "CLOSINGBALANCE"))),
                    "closing_value": float(_decimal_text(_node_text(node, "CLOSINGVALUE"))),
                    "standard_rate": float(_decimal_text(_node_text(node, "STANDARDPRICE"))),
                    "description": _node_text(node, "DESCRIPTION"),
                    "allocations": allocations,
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
                is_cancelled = _node_text(node, "ISCANCELLED", "CANCELLED").casefold() in {"yes", "true", "1"}
                is_optional = _node_text(node, "ISOPTIONAL").casefold() in {"yes", "true", "1"}
                is_post_dated = _node_text(node, "ISPOSTDATED").casefold() in {"yes", "true", "1"}
                parsed[key].append({**common,
                    "voucher_number": _node_text(node, "VOUCHERNUMBER"),
                    "date": invoice_date,
                    "reference": _node_text(node, "REFERENCE"),
                    "bill_reference": bill_reference or _node_text(node, "REFERENCE") or _node_text(node, "VOUCHERNUMBER"),
                    "due_date": due_date or invoice_date,
                    "voucher_type": _node_text(node, "VOUCHERTYPENAME"),
                    "transaction_type": "credit_note" if key == "credit_notes" else "invoice",
                    "lifecycle_status": "cancelled" if is_cancelled else "optional" if is_optional else "post_dated" if is_post_dated else "active",
                    "is_cancelled": is_cancelled,
                    "is_optional": is_optional,
                    "is_post_dated": is_post_dated,
                    "party_ledger": _node_text(node, "PARTYLEDGERNAME"),
                    "amount": float(abs(amount)),
                    "items": items,
                })
    return parsed


def _merge_deleted_transactions(current: list[dict], previous: list[dict], deleted_at) -> list[dict]:
    live_ids = {str(row.get("master_id") or row.get("guid") or row.get("invoice_id") or "") for row in current}
    merged = list(current)
    for row in previous:
        identity = str(row.get("master_id") or row.get("guid") or row.get("invoice_id") or "")
        if not identity or identity in live_ids:
            continue
        deleted = {key: value for key, value in row.items() if key not in {"_id", "sync_id", "synced_at"}}
        deleted.update({"lifecycle_status": "deleted", "payment_status": "deleted", "outstanding_amount": 0})
        deleted.setdefault("deleted_at", deleted_at)
        merged.append(deleted)
    return merged


def validate_snapshot_responses(responses: list[dict], parsed: dict) -> None:
    missing = SNAPSHOT_KEYS - {response.get("key") for response in responses or []}
    if missing:
        raise HTTPException(status_code=502, detail=f"Tally snapshot is incomplete: missing {', '.join(sorted(missing))}")
    # Every Tally company has built-in ledgers. An empty ledger export means the
    # collection request was not fulfilled and must not erase the last good cache.
    if not parsed.get("ledgers"):
        raise HTTPException(status_code=502, detail="Tally returned an empty ledger export; existing HeidelAI data was kept")


def parse_tally_import_response(xml: str) -> dict:
    try:
        root = _parse_tally_xml(xml)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail="Tally returned an invalid invoice import response") from exc
    line_error = _node_text(root, "LINEERROR")
    raw_counts = [_node_text(root, name) or "0" for name in ("ERRORS", "CREATED", "ALTERED")]
    if any(not value.isdigit() for value in raw_counts):
        raise HTTPException(status_code=502, detail="Tally returned invalid invoice import counters")
    errors, created, altered = map(int, raw_counts)
    master_id = _node_text(root, "LASTVCHID")
    voucher_number = _node_text(root, "VCHNUMBER")
    if line_error or _node_text(root, "STATUS") == "0" or errors or created + altered < 1:
        raise HTTPException(status_code=502, detail=line_error or "Tally did not confirm that it saved the invoice")
    if not master_id.isdigit() or int(master_id) < 1 or not voucher_number:
        raise HTTPException(status_code=502, detail="Tally saved the invoice but did not return its invoice number and Master ID")
    return {
        "master_id": master_id, "voucher_number": voucher_number,
        "created": created, "altered": altered,
    }


def parse_invoice_export_response(xml: str, master_id: str) -> dict:
    invoices = parse_snapshot_responses([{"key": "invoices", "xml": xml}])["invoices"]
    invoice = next((row for row in invoices if str(row.get("master_id")) == str(master_id)), None)
    if not invoice:
        raise HTTPException(status_code=502, detail="Tally did not return the requested invoice voucher")
    root = _parse_tally_xml(xml)
    voucher = next((node for node in root.iter() if _tag(node) == "VOUCHER" and _node_text(node, "MASTERID") == str(master_id)), None)
    if voucher is None:
        raise HTTPException(status_code=502, detail="Tally did not return the requested invoice voucher")
    bill_addresses = []
    ship_addresses = []
    dispatch_addresses = []
    ledger_entries = []
    seen_ledgers = set()
    for node in voucher.iter():
        tag = _tag(node)
        if tag == "BASICBUYERADDRESS" and node.text and node.text.strip() not in bill_addresses:
            bill_addresses.append(node.text.strip())
        elif tag == "ADDRESS" and node.text and node.text.strip() not in ship_addresses:
            ship_addresses.append(node.text.strip())
        elif tag == "DISPATCHFROMADDRESS" and node.text and node.text.strip() not in dispatch_addresses:
            dispatch_addresses.append(node.text.strip())
        if tag in {"ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"}:
            name = _node_text(node, "LEDGERNAME")
            amount = float(abs(_decimal_text(_direct_text(node, "AMOUNT"))))
            identity = (name.casefold(), amount)
            if name and identity not in seen_ledgers:
                seen_ledgers.add(identity)
                ledger_entries.append({"name": name, "amount": amount})
    bill_to = {
        "name": _node_text(voucher, "BASICBUYERNAME", "BUYERNAME", "PARTYNAME") or invoice.get("party_ledger"),
        "address": ", ".join(bill_addresses),
        "gstin": _node_text(voucher, "PARTYGSTIN"),
        "state": _node_text(voucher, "STATENAME"),
        "pincode": _node_text(voucher, "BUYERPINNUMBER"),
        "place": _node_text(voucher, "BILLTOPLACE"),
    }
    ship_to = {
        "name": _node_text(voucher, "CONSIGNEEMAILINGNAME"),
        "address": ", ".join(ship_addresses),
        "gstin": _node_text(voucher, "CONSIGNEEGSTIN"),
        "state": _node_text(voucher, "CONSIGNEESTATENAME") or _node_text(voucher, "PLACEOFSUPPLY"),
        "pincode": _node_text(voucher, "CONSIGNEEPINNUMBER"),
        "place": _node_text(voucher, "SHIPTOPLACE"),
    }
    dispatch_from = {
        "name": _node_text(voucher, "DISPATCHFROMNAME"),
        "address": ", ".join(dispatch_addresses),
        "state": _node_text(voucher, "DISPATCHFROMSTATENAME"),
        "pincode": _node_text(voucher, "DISPATCHFROMPINCODE"),
        "place": _node_text(voucher, "DISPATCHFROMPLACE"),
    }
    invoice.update({
        "party_name": bill_to["name"],
        "party_gstin": bill_to["gstin"],
        "party_state": bill_to["state"] or ship_to["state"],
        "party_address": bill_to["address"],
        "bill_to": {key: value for key, value in bill_to.items() if value},
        "ship_to": {key: value for key, value in ship_to.items() if value},
        "dispatch_from": {key: value for key, value in dispatch_from.items() if value},
        "narration": _node_text(voucher, "NARRATION"),
        "ledger_entries": ledger_entries,
    })
    return invoice


def validate_invoice_creation_readback(job: dict, result: dict) -> dict:
    imported = parse_tally_import_response(result.get("tally_response") or "")
    if imported["master_id"] != str(result.get("master_id") or "") or imported["voucher_number"] != str(result.get("voucher_number") or ""):
        raise HTTPException(status_code=502, detail="Tally's invoice identifiers changed between import and confirmation")
    response = next((item for item in result.get("responses") or [] if item.get("key") == "invoice"), None)
    invoice = parse_invoice_export_response((response or {}).get("xml") or "", imported["master_id"])
    checks = (
        (invoice.get("voucher_number") == imported["voucher_number"], "invoice number"),
        (invoice.get("reference") == job.get("quote_number"), "reference"),
        (str(invoice.get("party_ledger") or "").casefold() == str(job.get("party_ledger") or "").casefold(), "customer ledger"),
        (_money(invoice.get("amount")) == _money(job.get("amount")), "total"),
    )
    mismatch = next((label for matched, label in checks if not matched), "")
    if mismatch:
        raise HTTPException(status_code=502, detail=f"Tally invoice read-back did not match the requested {mismatch}")
    return invoice


def _pdf_document(lines: list[str]) -> bytes:
    pages = [lines[index:index + 50] for index in range(0, len(lines), 50)] or [[""]]
    font_id = 3 + len(pages) * 2
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(f'{3 + index * 2} 0 R' for index in range(len(pages)))}] >>".encode(),
        font_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    }
    for index, page_lines in enumerate(pages):
        page_id, content_id = 3 + index * 2, 4 + index * 2
        commands = ["q 1 1 1 rg 0 0 595 842 re f Q"]
        if index == 0:
            commands.append("q 0.94 0.97 1 rg 32 750 531 60 re f Q")
        commands.append("BT /F1 9 Tf 42 800 Td 14 TL")
        for line_index, line in enumerate(page_lines):
            if index == 0 and line_index == 0:
                commands.append("/F1 12 Tf")
            elif index == 0 and line_index == 1:
                commands.append("/F1 15 Tf")
            elif index == 0 and line_index == 2:
                commands.append("/F1 9 Tf")
            # ponytail: built-in PDF font is Latin-1; embed a Unicode font if non-Latin Tally names are required.
            safe = str(line).encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({safe}) Tj T*")
        commands.extend(["ET", f"BT /F1 8 Tf 500 24 Td (Page {index + 1} of {len(pages)}) Tj ET"])
        stream = "\n".join(commands).encode("latin-1")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode()
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, font_id + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode() + objects[object_id] + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {font_id + 1}\n0000000000 65535 f \n".encode())
    output.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets[1:]).encode())
    output.extend(f"trailer\n<< /Size {font_id + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


def render_invoice_pdf(invoice: dict, company_name: str, sales_ledger: str = "Sales") -> bytes:
    def amount(value) -> str:
        return f"INR {float(value or 0):,.2f}"

    def wrap(value, width=88) -> list[str]:
        words, rows, current = str(value or "").split(), [], ""
        for word in words:
            if current and len(current) + len(word) + 1 > width:
                rows.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        return rows + ([current] if current else [])

    def address_lines(title: str, value: dict) -> list[str]:
        rows = wrap(f"{title}: {value.get('name') or '-'}")
        if value.get("address"):
            rows.extend(wrap(f"Address: {value['address']}"))
        if value.get("gstin"):
            rows.append(f"GSTIN: {value['gstin']}")
        location = ", ".join(filter(None, [value.get("place"), value.get("state"), value.get("pincode")]))
        if location:
            rows.extend(wrap(f"Place / state / PIN: {location}"))
        return rows

    bill_to = invoice.get("bill_to") or {
        "name": invoice.get("party_name") or invoice.get("party_ledger"),
        "address": invoice.get("party_address"), "gstin": invoice.get("party_gstin"),
        "state": invoice.get("party_state"),
    }
    ship_to = invoice.get("ship_to") or {}
    dispatch_from = invoice.get("dispatch_from") or {}

    lines = [
        str(company_name or "Tally company").upper()[:80], "TAX INVOICE", "=" * 88,
        f"Invoice: {invoice.get('voucher_number') or '-'}    Date: {invoice.get('date') or '-'}",
        *wrap(f"Reference: {invoice.get('reference') or '-'}"), "",
        *address_lines("Bill to", bill_to),
    ]
    if ship_to and ship_to != bill_to:
        lines.extend(["", *address_lines("Ship to", ship_to)])
    if dispatch_from:
        lines.extend(["", *address_lines("Dispatch from", dispatch_from)])
    lines.extend(["", f"{'Product':<48}{'Qty':>8}{'Rate':>15}{'Amount':>17}", "-" * 88])
    subtotal = 0.0
    for item in invoice.get("items") or []:
        item_amount = float(item.get("amount") or 0)
        subtotal += item_amount
        descriptions = wrap(item.get("name") or "Item", 46) or ["Item"]
        quantity = f"{float(item.get('quantity') or 0):g}"
        lines.append(f"{descriptions[0]:<48}{quantity:>8}{amount(item.get('rate')):>15}{amount(item_amount):>17}")
        lines.extend(f"  {description}" for description in descriptions[1:])
    lines.extend(["-" * 88, f"{'Items subtotal':<71}{amount(subtotal):>17}"])
    excluded = {str(invoice.get("party_ledger") or "").casefold(), str(sales_ledger or "").casefold()}
    for entry in invoice.get("ledger_entries") or []:
        if entry.get("name", "").casefold() not in excluded and float(entry.get("amount") or 0):
            labels = wrap(entry["name"], 69) or ["Charge / tax"]
            lines.append(f"{labels[0]:<71}{amount(entry['amount']):>17}")
            lines.extend(f"  {label}" for label in labels[1:])
    lines.extend([f"{'GRAND TOTAL':<71}{amount(invoice.get('amount')):>17}", "=" * 88])
    if invoice.get("narration"):
        lines.extend(["", *wrap(f"Narration: {invoice['narration']}")])
    lines.extend(["", "Generated from live TallyPrime voucher data via HeidelAI."])
    return _pdf_document(lines)


def _customer_snapshot(quote: dict, conversation: dict | None = None) -> dict:
    customer = quote.get("customer") or {}
    conversation = conversation or {}
    billing_address = (
        customer.get("billing_address") or customer.get("address")
        or conversation.get("billing_address") or conversation.get("billingAddress")
    )
    shipping_address = (
        customer.get("shipping_address") or conversation.get("shipping_address")
        or conversation.get("shippingAddress") or billing_address
    )
    values = {
        "name": customer.get("name") or conversation.get("customer_name"),
        "phone": customer.get("phone") or conversation.get("customer_id"),
        "email": customer.get("email"),
        "gstin": customer.get("gstin"),
        "address": billing_address,
        "billing_address": billing_address,
        "shipping_address": shipping_address,
        "state": customer.get("state") or conversation.get("state"),
        "pincode": customer.get("pincode") or conversation.get("pincode"),
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


async def _record_tally_outcomes(
    org_id: str, conversation_id: str, quote_id: str, quote_revision: int,
    event_key: str, status: str,
) -> None:
    now = datetime.now(timezone.utc)
    outbox_id = f"{org_id}:{event_key}"
    await db.tally_outcome_outbox.update_one(
        {"_id": outbox_id},
        {"$setOnInsert": {
            "org_id": org_id,
            "conversation_id": conversation_id,
            "quote_id": quote_id,
            "quote_revision": int(quote_revision),
            "event_key": event_key,
            "outcome_status": status,
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": now,
            "created_at": now,
            "expires_at": now + timedelta(days=30),
        }},
        upsert=True,
    )
    await _process_tally_outcome(outbox_id)


async def _process_tally_outcome(outbox_id: str) -> bool:
    now = datetime.now(timezone.utc)
    claim_id = uuid.uuid4().hex
    job = await db.tally_outcome_outbox.find_one_and_update(
        {
            "_id": outbox_id,
            "attempts": {"$lt": TALLY_OUTCOME_MAX_ATTEMPTS},
            "$or": [
                {"status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": now}},
                {"status": "processing", "claimed_at": {"$lt": now - TALLY_OUTCOME_CLAIM_TIMEOUT}},
            ],
        },
        {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return False

    errors = []
    try:
        from services.sales_offer_service import record_verified_offer_outcome

        await record_verified_offer_outcome(
            job["org_id"], job["conversation_id"], job["quote_id"],
            int(job["quote_revision"]), job["outcome_status"], job["event_key"],
        )
    except Exception as exc:
        errors.append(f"offer:{type(exc).__name__}")
    try:
        from services.sales_learning_service import record_verified_tally_outcome

        await record_verified_tally_outcome(
            job["org_id"], job["conversation_id"], job["event_key"], job["outcome_status"],
        )
    except Exception as exc:
        errors.append(f"learning:{type(exc).__name__}")

    completed_at = datetime.now(timezone.utc)
    if not errors:
        await db.tally_outcome_outbox.update_one(
            {"_id": outbox_id, "claim_id": claim_id},
            {"$set": {"status": "completed", "completed_at": completed_at}, "$unset": {"claim_id": "", "claimed_at": "", "last_error": ""}},
        )
        return True

    attempts = int(job.get("attempts") or 0) + 1
    await db.tally_outcome_outbox.update_one(
        {"_id": outbox_id, "claim_id": claim_id},
        {
            "$set": {
                "status": "dead" if attempts >= TALLY_OUTCOME_MAX_ATTEMPTS else "failed",
                "attempts": attempts,
                "last_error": ",".join(errors),
                "next_attempt_at": completed_at + timedelta(seconds=min(900, 30 * (2 ** (attempts - 1)))),
            },
            "$unset": {"claim_id": "", "claimed_at": ""},
        },
    )
    logger.warning(f"Deferred verified Tally outcome {outbox_id}: {','.join(errors)}")
    return False


async def process_pending_tally_outcomes() -> int:
    now = datetime.now(timezone.utc)
    jobs = await db.tally_outcome_outbox.find({
        "attempts": {"$lt": TALLY_OUTCOME_MAX_ATTEMPTS},
        "$or": [
            {"status": {"$in": ["pending", "failed"]}, "next_attempt_at": {"$lte": now}},
            {"status": "processing", "claimed_at": {"$lt": now - TALLY_OUTCOME_CLAIM_TIMEOUT}},
        ],
    }, {"_id": 1}).limit(50).to_list(length=50)
    completed = 0
    for job in jobs:
        completed += int(await _process_tally_outcome(str(job["_id"])))
    return completed


def _customer_mapping(party: dict, link: dict) -> dict:
    return {
        "party_master_id": party["master_id"],
        "party_ledger": party["name"],
        "company_name": party["name"],
        "gstin": party.get("gstin") or "",
        "tally_phone": party.get("phone") or "",
        "phone": link["phone"],
        "conversation_id": link["conversation_id"],
        "match_source": link.get("match_source") or "manual",
        "updated_at": link["updated_at"],
    }


async def _store_customer_mapping(org_id: str, party: dict, link: dict) -> None:
    mapping = _customer_mapping(party, link)
    await db[f"conversations_{org_id}"].update_one(
        {"conversation_id": link["conversation_id"]},
        {"$set": {"tally_customer_mapping": mapping}},
    )
    now = datetime.now(timezone.utc)
    await db[f"contacts_{org_id}"].update_one(
        {"conversation_id": link["conversation_id"]},
        {"$set": {
            "conversation_id": link["conversation_id"], "phone_number": link["phone"], "platform": "whatsapp",
            "tally_customer_mapping": mapping, "company": party["name"], "gstin": party.get("gstin") or "", "updated_at": now,
        }, "$setOnInsert": {"full_name": party["name"], "created_at": now}},
        upsert=True,
    )


async def _promote_primary_link(org_id: str, party_master_id: str) -> None:
    if not party_master_id or await db.tally_customer_links.find_one({
        "org_id": org_id, "party_master_id": party_master_id, "is_primary": True,
    }):
        return
    row = await db.tally_customer_links.find_one_and_update(
        {"org_id": org_id, "party_master_id": party_master_id},
        {"$set": {"is_primary": True, "updated_at": datetime.now(timezone.utc)}},
        sort=[("updated_at", 1)], return_document=ReturnDocument.AFTER,
    )
    if row:
        party = await db.tally_ledgers.find_one({"org_id": org_id, "master_id": party_master_id})
        if party:
            await _store_customer_mapping(org_id, party, row)


async def _upsert_customer_link(
    org_id: str, party: dict, conversation_id: str, phone: str, user_id: str,
    *, match_source: str, make_primary: bool = False,
) -> dict | None:
    existing = await db.tally_customer_links.find_one({"org_id": org_id, "conversation_id": conversation_id})
    if existing and existing.get("party_master_id") != party["master_id"] and match_source != "manual":
        return None
    old_party = (existing or {}).get("party_master_id")
    old_primary = bool((existing or {}).get("is_primary"))
    other = await db.tally_customer_links.find_one({
        "org_id": org_id, "party_master_id": party["master_id"], "conversation_id": {"$ne": conversation_id},
    })
    primary = make_primary or (old_party == party["master_id"] and old_primary) or not other
    if primary:
        await db.tally_customer_links.update_many(
            {"org_id": org_id, "party_master_id": party["master_id"], "conversation_id": {"$ne": conversation_id}},
            {"$set": {"is_primary": False}},
        )
    now = datetime.now(timezone.utc)
    link = await db.tally_customer_links.find_one_and_update(
        {"org_id": org_id, "conversation_id": conversation_id},
        {"$set": {
            "party_master_id": party["master_id"], "party_ledger": party["name"],
            "company_name": party["name"], "gstin": party.get("gstin") or "",
            "tally_phone": party.get("phone") or "", "phone": phone,
            "match_source": match_source if match_source == "manual" else (existing or {}).get("match_source") or match_source,
            "is_primary": primary, "updated_at": now, "updated_by": user_id,
        }, "$setOnInsert": {"org_id": org_id, "conversation_id": conversation_id, "created_at": now}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    await _store_customer_mapping(org_id, party, link)
    if old_party and old_party != party["master_id"] and old_primary:
        await _promote_primary_link(org_id, old_party)
    return link


async def _reconcile_customer_links(org_id: str, ledgers: list[dict]) -> None:
    parties = [party for party in ledgers if party.get("parent") == "Sundry Debtors" and party.get("master_id")]
    parties_by_id = {party["master_id"]: party for party in parties}
    linked_conversations = set()
    async for link in db.tally_customer_links.find({"org_id": org_id}):
        linked_conversations.add(link.get("conversation_id"))
        party = parties_by_id.get(link.get("party_master_id"))
        if party:
            await _upsert_customer_link(
                org_id, party, link["conversation_id"], link.get("phone") or "", link.get("updated_by") or "sync",
                match_source=link.get("match_source") or "manual", make_primary=bool(link.get("is_primary")),
            )
    async for contact in db[f"contacts_{org_id}"].find(
        {"platform": "whatsapp"},
        {"conversation_id": 1, "phone_number": 1, "gstin": 1},
    ):
        phone = str(contact.get("phone_number") or "")
        conversation_id = contact.get("conversation_id") or (f"whatsapp_{phone}" if phone else "")
        if not conversation_id or conversation_id in linked_conversations:
            continue
        matches = _strict_party_matches(parties, contact.get("gstin") or "", phone)
        if len(matches) == 1:
            source = "gstin" if contact.get("gstin") else "phone"
            link = await _upsert_customer_link(
                org_id, matches[0], conversation_id, phone, "automatic-sync", match_source=source,
            )
            if link:
                linked_conversations.add(conversation_id)


async def _sync_tally_contacts(org_id: str, ledgers: list[dict]) -> dict:
    parties = [
        party for party in ledgers
        if party.get("parent") == "Sundry Debtors" and party.get("master_id")
    ]
    collection = db[f"contacts_{org_id}"]
    # ponytail: one org-local scan per Tally snapshot; batch/index this beyond 100k contacts per org.
    contacts = await collection.find({}, {
        "_id": 1, "conversation_id": 1, "platform": 1, "phone_number": 1, "gstin": 1,
        "tally.master_id": 1, "tally_customer_mapping.party_master_id": 1,
    }).to_list(length=None)
    master_index: dict[str, set] = {}
    gstin_index: dict[str, set] = {}
    phone_index: dict[str, set] = {}
    by_id = {contact["_id"]: contact for contact in contacts}

    def index(target: dict[str, set], key: str, contact_id) -> None:
        if key:
            target.setdefault(key, set()).add(contact_id)

    for contact in contacts:
        contact_id = contact["_id"]
        tally = contact.get("tally") or {}
        mapping = contact.get("tally_customer_mapping") or {}
        index(master_index, str(tally.get("master_id") or mapping.get("party_master_id") or ""), contact_id)
        index(gstin_index, str(tally.get("gstin") or contact.get("gstin") or "").strip().upper(), contact_id)
        index(phone_index, _normalized_phone(contact.get("phone_number") or ""), contact_id)

    counts = {"imported": 0, "updated": 0, "inactive": 0, "ambiguous": 0, "skipped": 0}
    active_master_ids = []
    now = datetime.now(timezone.utc)
    party_gstins: dict[str, int] = {}
    party_phones: dict[str, int] = {}
    for party in parties:
        gstin = str(party.get("gstin") or "").strip().upper()
        phone = _normalized_phone(party.get("phone") or "")
        if gstin:
            party_gstins[gstin] = party_gstins.get(gstin, 0) + 1
        if phone:
            party_phones[phone] = party_phones.get(phone, 0) + 1
    for party in parties:
        master_id = str(party["master_id"])
        active_master_ids.append(master_id)
        gstin = str(party.get("gstin") or "").strip().upper()
        phone = _normalized_phone(party.get("phone") or "")
        contact_id, source = _contact_match_id(party, master_index, gstin_index, phone_index)
        if source != "master_id" and ((gstin and party_gstins[gstin] > 1) or (not gstin and phone and party_phones[phone] > 1)):
            counts["ambiguous"] += 1
            continue
        if source == "ambiguous":
            counts["ambiguous"] += 1
            continue
        phones = sorted({normalize_phone(value) for value in party.get("phones") or [party.get("phone")] if normalize_phone(value)})
        tally_contact = {
            "master_id": master_id,
            "legal_name": str(party.get("name") or "").strip(),
            "gstin": str(party.get("gstin") or "").strip().upper(),
            "phones": phones,
            "email": str(party.get("email") or "").strip(),
            "state": str(party.get("state") or "").strip(),
            "active": True,
            "match_source": source,
            "synced_at": now,
        }
        if contact_id is None:
            conversation_id = f"tally_{hashlib.sha256(f'{org_id}:{master_id}'.encode()).hexdigest()[:24]}"
            result = await collection.update_one(
                {"conversation_id": conversation_id},
                {"$set": {"tally": tally_contact, "company": tally_contact["legal_name"], "gstin": tally_contact["gstin"], "updated_at": now},
                 "$setOnInsert": {
                     "conversation_id": conversation_id, "platform": "tally",
                     "full_name": tally_contact["legal_name"], "phone_number": phones[0] if phones else "",
                     "email": tally_contact["email"] or None, "categories": [], "labels": [], "created_at": now,
                 }},
                upsert=True,
            )
            counts["imported"] += int(result.upserted_id is not None)
            counts["updated"] += int(result.upserted_id is None)
            continue

        contact = by_id[contact_id]
        primary_fields = {"full_name": tally_contact["legal_name"], "phone_number": phones[0] if phones else "", "email": tally_contact["email"] or None} if contact.get("platform") == "tally" else {}
        await collection.update_one(
            {"_id": contact_id},
            {"$set": {"tally": tally_contact, "company": tally_contact["legal_name"], "gstin": tally_contact["gstin"], "updated_at": now, **primary_fields}},
        )
        counts["updated"] += 1
        conversation_id = str(contact.get("conversation_id") or "")
        phone = str(contact.get("phone_number") or "")
        if conversation_id.startswith("whatsapp_") and phone:
            await _upsert_customer_link(org_id, party, conversation_id, phone, "tally-contact-sync", match_source=source)

    inactive = await collection.update_many(
        {"tally.master_id": {"$exists": True, "$nin": active_master_ids}, "tally.active": {"$ne": False}},
        {"$set": {"tally.active": False, "tally.synced_at": now, "updated_at": now}},
    )
    counts["inactive"] = inactive.modified_count
    counts["skipped"] = len([
        party for party in ledgers
        if party.get("parent") == "Sundry Debtors" and not party.get("master_id")
    ])
    return counts


async def _set_contact_sync_health(org_id: str, status: str, *, counts: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    values = {"contact_sync_status": status, "contact_sync_last_attempt_at": now}
    if status in {"healthy", "partial"}:
        values.update({
            "contact_sync_last_success_at": now,
            "contact_sync_error": None if status == "healthy" else "Some Tally customers need manual review",
            "contact_sync_counts": counts or {},
        })
    elif status == "failed":
        values["contact_sync_error"] = "Tally contacts could not be synchronized"
    previous = await db.tally_integrations.find_one_and_update(
        {"org_id": org_id},
        {"$set": values, "$setOnInsert": {"org_id": org_id}},
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    if (previous or {}).get("contact_sync_status") != status:
        await broadcast_org_admin_ws(org_id, "tally_contact_sync_health", {
            "org_id": org_id, "status": status, "error": values.get("contact_sync_error"),
            "counts": counts or {}, "updated_at": now.isoformat(),
        })


async def check_tally_contact_sync_health() -> int:
    """Keep a persistent setup/staleness warning for entitled organizations."""
    checked = 0
    now = datetime.now(timezone.utc)
    async for row in db.feature_entitlements.find({"product": "TALLY"}, {"account_id": 1}):
        org_id = str(row.get("account_id") or "")
        if not org_id or feature_access(org_id, "TALLY", now)["state"] != "active":
            continue
        integration = await get_integration(org_id)
        settings = normalized_settings(integration)
        if not settings["company_name"].strip() or not integration.get("connector_key_hash"):
            status = "setup_required"
        else:
            last_success = integration.get("contact_sync_last_success_at")
            if last_success and last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=timezone.utc)
            status = "stale" if not last_success or last_success < now - CONTACT_SYNC_STALE_AFTER else "healthy"
        if status != "healthy" and integration.get("contact_sync_status") != status:
            await _set_contact_sync_health(org_id, status)
        checked += 1
    return checked


async def _remember_customer_mapping(org_id: str, job: dict) -> None:
    ledger = await db.tally_ledgers.find_one({
        "org_id": org_id, "name": {"$regex": f"^{re.escape(job['party_ledger'])}$", "$options": "i"},
    })
    if ledger and ledger.get("master_id") and job.get("conversation_id", "").startswith("whatsapp_"):
        await _upsert_customer_link(
            org_id, ledger, job["conversation_id"], job["conversation_id"].removeprefix("whatsapp_"),
            job.get("created_by") or "invoice", match_source="invoice",
        )


async def get_customer_mapping(org_id: str, conversation_id: str) -> dict:
    link = await db.tally_customer_links.find_one(
        {"org_id": org_id, "conversation_id": conversation_id}, {"_id": 0}
    )
    if link:
        return serialize_mongo(link)
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


def _clean_invoice_address(value: dict | None, fallback: dict | None = None) -> dict:
    value, fallback = value or {}, fallback or {}
    result = {
        key: str(value.get(key) or fallback.get(key) or "").strip()
        for key in ("name", "address", "state", "pincode", "gstin", "place")
        if value.get(key) or fallback.get(key)
    }
    if result.get("gstin"):
        result["gstin"] = result["gstin"].upper()
    return result


def _string_list_xml(field: str, value: str) -> str:
    lines = [line.strip() for line in re.split(r"[\r\n]+", str(value or "")) if line.strip()]
    return (
        f"<{field}.LIST TYPE=\"String\">"
        + "".join(f"<{field}>{_xml(line)}</{field}>" for line in lines)
        + f"</{field}.LIST>"
    ) if lines else ""


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
    bill_to = _clean_invoice_address(quote.get("bill_to"), quote.get("customer"))
    ship_to = _clean_invoice_address(quote.get("ship_to"), bill_to)
    dispatch_from = _clean_invoice_address(quote.get("dispatch_from"))
    bill_xml = (
        f"<BASICBUYERNAME>{_xml(bill_to.get('name'))}</BASICBUYERNAME>"
        f"{_string_list_xml('BASICBUYERADDRESS', bill_to.get('address', ''))}"
        f"<PARTYGSTIN>{_xml(bill_to.get('gstin'))}</PARTYGSTIN>"
        f"<STATENAME>{_xml(bill_to.get('state'))}</STATENAME>"
        f"<BUYERPINNUMBER>{_xml(bill_to.get('pincode'))}</BUYERPINNUMBER>"
        f"<BILLTOPLACE>{_xml(bill_to.get('place') or bill_to.get('state'))}</BILLTOPLACE>"
    ) if bill_to else ""
    ship_xml = (
        f"<CONSIGNEEMAILINGNAME>{_xml(ship_to.get('name'))}</CONSIGNEEMAILINGNAME>"
        f"{_string_list_xml('ADDRESS', ship_to.get('address', ''))}"
        f"<CONSIGNEEGSTIN>{_xml(ship_to.get('gstin'))}</CONSIGNEEGSTIN>"
        f"<CONSIGNEESTATENAME>{_xml(ship_to.get('state'))}</CONSIGNEESTATENAME>"
        f"<CONSIGNEEPINNUMBER>{_xml(ship_to.get('pincode'))}</CONSIGNEEPINNUMBER>"
        f"<SHIPTOPLACE>{_xml(ship_to.get('place') or ship_to.get('state'))}</SHIPTOPLACE>"
        f"<PLACEOFSUPPLY>{_xml(ship_to.get('state') or ship_to.get('place'))}</PLACEOFSUPPLY>"
    ) if ship_to and ship_to != bill_to else ""
    dispatch_xml = (
        f"<DISPATCHFROMNAME>{_xml(dispatch_from.get('name'))}</DISPATCHFROMNAME>"
        f"{_string_list_xml('DISPATCHFROMADDRESS', dispatch_from.get('address', ''))}"
        f"<DISPATCHFROMSTATENAME>{_xml(dispatch_from.get('state'))}</DISPATCHFROMSTATENAME>"
        f"<DISPATCHFROMPINCODE>{_xml(dispatch_from.get('pincode'))}</DISPATCHFROMPINCODE>"
        f"<DISPATCHFROMPLACE>{_xml(dispatch_from.get('place'))}</DISPATCHFROMPLACE>"
    ) if dispatch_from else ""
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{_xml(settings['company_name'])}</SVCURRENTCOMPANY>"
        "<SVVCHIMPORTFORMAT>$$SysName:XML</SVVCHIMPORTFORMAT>"
        "</STATICVARIABLES></REQUESTDESC><REQUESTDATA>"
        "<TALLYMESSAGE xmlns:UDF=\"TallyUDF\">"
        f"<VOUCHER REMOTEID=\"{remote_id}\" VCHTYPE=\"{_xml(settings['voucher_type'])}\" ACTION=\"Create\" OBJVIEW=\"Invoice Voucher View\">"
        f"<DATE>{issued}</DATE><VOUCHERTYPENAME>{_xml(settings['voucher_type'])}</VOUCHERTYPENAME>"
        f"<PARTYLEDGERNAME>{_xml(party_ledger)}</PARTYLEDGERNAME>"
        f"{bill_xml}{ship_xml}{dispatch_xml}"
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
            f"<DESCRIPTION>{_xml(value.get('description'))}</DESCRIPTION>"
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


def build_physical_stock_xml(settings: dict, item: dict, quantity: float, allocations: list[dict]) -> str:
    """Build a location/batch-aware Physical Stock voucher for actual quantity."""
    unit = item.get("unit") or normalized_settings(settings)["stock_unit"]
    allocations = allocations or [{"godown": "Main Location", "batch": "Primary Batch", "quantity": quantity}]
    total = sum(float(row.get("quantity") or 0) for row in allocations)
    if abs(total - float(quantity)) > 0.001:
        raise HTTPException(status_code=422, detail="Location and batch quantities must equal the total stock quantity")
    batch_xml = "".join(
        "<BATCHALLOCATIONS.LIST>"
        f"<GODOWNNAME>{_xml(row.get('godown') or 'Main Location')}</GODOWNNAME>"
        f"<BATCHNAME>{_xml(row.get('batch') or 'Primary Batch')}</BATCHNAME>"
        f"<ACTUALQTY>{_number(row.get('quantity'))} {_xml(unit)}</ACTUALQTY>"
        f"<BILLEDQTY>{_number(row.get('quantity'))} {_xml(unit)}</BILLEDQTY>"
        "</BATCHALLOCATIONS.LIST>"
        for row in allocations
    )
    company = _xml(normalized_settings(settings)["company_name"])
    body = (
        '<VOUCHER VCHTYPE="Physical Stock" ACTION="Create" OBJVIEW="Inventory Voucher View">'
        f"<DATE>{date.today().strftime('%Y%m%d')}</DATE><EFFECTIVEDATE>{date.today().strftime('%Y%m%d')}</EFFECTIVEDATE>"
        "<VOUCHERTYPENAME>Physical Stock</VOUCHERTYPENAME><PERSISTEDVIEW>Inventory Voucher View</PERSISTEDVIEW>"
        "<ALLINVENTORYENTRIES.LIST>"
        f"<STOCKITEMNAME>{_xml(item.get('name'))}</STOCKITEMNAME>"
        f"<ACTUALQTY>{_number(quantity)} {_xml(unit)}</ACTUALQTY>"
        f"<BILLEDQTY>{_number(quantity)} {_xml(unit)}</BILLEDQTY>{batch_xml}"
        "</ALLINVENTORYENTRIES.LIST></VOUCHER>"
    )
    return (
        "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
        "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
        f"<SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC><REQUESTDATA>"
        f'<TALLYMESSAGE xmlns:UDF="TallyUDF">{body}</TALLYMESSAGE>'
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
    previous_rows = []
    previous_invoices = {}
    async for row in db.tally_invoices.find({"org_id": org_id}):
        previous_rows.append(row)
        if row.get("master_id"):
            previous_invoices[row["master_id"]] = row
    invoice_origins = {}
    async for job in db.tally_bill_jobs.find(
        {"org_id": org_id, "status": "completed", "master_id": {"$type": "string", "$ne": ""}},
        {"master_id": 1, "conversation_id": 1, "customer.phone": 1, "quote_id": 1, "quote_revision": 1},
    ).sort("created_at", -1):
        invoice_origins.setdefault(job["master_id"], {
            "conversation_id": job.get("conversation_id") or "",
            "phone": (job.get("customer") or {}).get("phone") or str(job.get("conversation_id") or "").removeprefix("whatsapp_"),
            "quote_id": job.get("quote_id") or "",
            "quote_revision": int(job.get("quote_revision") or 1),
        })
    outstanding = {
        (row["party_ledger"].casefold(), row["bill_reference"].casefold()): row
        for row in parsed["outstandings"]
    }
    ledger_ids = {row["name"].casefold(): row.get("master_id", "") for row in parsed["ledgers"]}
    billwise_ledgers = {row["name"].casefold(): row.get("billwise") is True for row in parsed["ledgers"]}
    transactions = parsed["invoices"] + parsed["credit_notes"]
    for invoice in transactions:
        invoice["invoice_id"] = invoice.get("master_id") or invoice.get("guid") or f"{invoice['voucher_number']}:{invoice['date']}"
        invoice["party_master_id"] = ledger_ids.get(invoice.get("party_ledger", "").casefold(), "")
        due = outstanding.get((invoice.get("party_ledger", "").casefold(), invoice.get("bill_reference", "").casefold())) if (
            invoice["transaction_type"] == "invoice" and invoice["lifecycle_status"] == "active"
        ) else None
        invoice["outstanding_amount"] = float((due or {}).get("outstanding_amount") or 0)
        invoice["due_date"] = (due or {}).get("due_date") or invoice.get("due_date") or invoice.get("date")
        invoice["payment_status"] = (
            invoice["lifecycle_status"] if invoice["lifecycle_status"] != "active"
            else "credit" if invoice["transaction_type"] == "credit_note"
            else "overdue" if due and invoice["due_date"] and invoice["due_date"] < date.today().isoformat()
            else "outstanding" if due
            else "paid" if billwise_ledgers.get(invoice.get("party_ledger", "").casefold())
            else "unknown"
        )
        previous = previous_invoices.get(invoice.get("master_id")) or {}
        pdf = previous.get("pdf") or {}
        if (pdf.get("public_id") or pdf.get("url")) and str(pdf.get("alter_id")) == str(invoice.get("alter_id")):
            invoice["pdf"] = pdf
        previous_contact = previous.get("billing_contact") or {}
        origin = invoice_origins.get(invoice.get("master_id")) or {}
        billing_contact = {**origin, **previous_contact}
        if billing_contact:
            invoice["billing_contact"] = billing_contact
    parsed["invoices"] = _merge_deleted_transactions(transactions, previous_rows, now)
    for key, collection in (("ledgers", db.tally_ledgers), ("stock_items", db.tally_stock_items), ("units", db.tally_units), ("invoices", db.tally_invoices), ("outstandings", db.tally_outstandings)):
        rows = [{**row, "org_id": org_id, "sync_id": sync_id, "synced_at": now} for row in parsed[key]]
        if rows:
            await collection.insert_many(rows)
        await collection.delete_many({"org_id": org_id, "sync_id": {"$ne": sync_id}})
    await _reconcile_customer_links(org_id, parsed["ledgers"])
    contact_counts = {}
    try:
        contact_counts = await _sync_tally_contacts(org_id, parsed["ledgers"])
        contact_status = "partial" if contact_counts["ambiguous"] or contact_counts["skipped"] else "healthy"
        await _set_contact_sync_health(org_id, contact_status, counts=contact_counts)
    except Exception as error:
        try:
            await _set_contact_sync_health(org_id, "failed")
        except Exception as health_error:
            logger.error(f"Tally contact health update failed: {type(health_error).__name__}")
        # Contact sync is secondary: never discard a valid financial snapshot.
        logger.error(f"Tally contact reconciliation failed: {type(error).__name__}")
    invalid_invoice_ids = [
        row["invoice_id"] for row in parsed["invoices"]
        if row.get("invoice_id") and (row.get("transaction_type") != "invoice" or row.get("lifecycle_status") != "active")
    ]
    if invalid_invoice_ids:
        await db.tally_payment_reminders.update_many(
            {"org_id": org_id, "invoice_id": {"$in": invalid_invoice_ids}, "status": {"$in": ["pending", "needs_review", "failed"]}},
            {"$set": {"status": "cancelled", "cancel_reason": "Voucher is no longer an active Tally invoice", "cancelled_at": now, "updated_at": now}},
        )
    fy_from, fy_to = _financial_year_period()
    if parsed["profit_loss"].get("rows"):
        await db.tally_profit_loss_reports.update_one(
            {"org_id": org_id, "from_date": fy_from, "to_date": fy_to},
            {"$set": {**parsed["profit_loss"], "synced_at": now}},
            upsert=True,
        )
    counts = {key: len(value) for key, value in parsed.items() if isinstance(value, list) and key != "invoices"}
    counts.update({
        "invoices": sum(row.get("transaction_type") == "invoice" and row.get("lifecycle_status") != "deleted" for row in parsed["invoices"]),
        "credit_notes": sum(row.get("transaction_type") == "credit_note" and row.get("lifecycle_status") != "deleted" for row in parsed["invoices"]),
        "cancelled": sum(row.get("lifecycle_status") == "cancelled" for row in parsed["invoices"]),
        "deleted": sum(row.get("lifecycle_status") == "deleted" for row in parsed["invoices"]),
    })
    await db.tally_integrations.update_one(
        {"org_id": org_id}, {"$set": {
            "last_sync_at": now, "last_sync_error": None, "last_sync_counts": counts,
        }}
    )
    await reconcile_default_payment_reminders(org_id)
    for invoice in parsed["invoices"]:
        contact = invoice.get("billing_contact") or {}
        if invoice.get("payment_status") == "paid" and contact.get("conversation_id"):
            await _record_tally_outcomes(
                org_id,
                str(contact["conversation_id"]),
                str(contact.get("quote_id") or ""),
                int(contact.get("quote_revision") or 1),
                f"tally-paid:{invoice.get('invoice_id')}",
                "paid",
            )
    return {"counts": counts, "contact_counts": contact_counts, "last_sync_at": now}


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
    if kind in {"", "party"} and rows:
        links_by_party: dict[str, list[dict]] = {}
        async for link in db.tally_customer_links.find(
            {"org_id": org_id, "party_master_id": {"$in": [
                row.get("master_id") for row in rows if row.get("parent") == "Sundry Debtors"
            ]}}, {"_id": 0}
        ).sort([("is_primary", -1), ("created_at", 1)]):
            links_by_party.setdefault(link["party_master_id"], []).append(link)
        for row in rows:
            row["customer_links"] = links_by_party.get(row.get("master_id"), [])
    return serialize_mongo(rows)


def _tally_stock_match(org_id: str, query: str = "") -> dict:
    match: dict = {"org_id": org_id}
    if query.strip():
        pattern = {"$regex": re.escape(query.strip()), "$options": "i"}
        match["$or"] = [{"name": pattern}, {"model_number": pattern}, {"aliases": pattern}]
    return match


async def list_tally_stock_items(
    org_id: str, query: str = "", page: int = 1, page_size: int = 500
) -> list[dict]:
    rows = await db.tally_stock_items.find(
        _tally_stock_match(org_id, query), {"_id": 0, "sync_id": 0}
    ).sort("name", 1).skip((page - 1) * page_size).limit(page_size).to_list(length=page_size)
    return serialize_mongo(rows)


async def count_tally_stock_items(org_id: str, query: str = "") -> int:
    return await db.tally_stock_items.count_documents(_tally_stock_match(org_id, query))


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
    if status in {"paid", "outstanding", "overdue", "unknown", "credit"}:
        match["payment_status"] = status
    elif status in {"active", "cancelled", "deleted", "optional", "post_dated"}:
        match["lifecycle_status"] = status
    elif status == "credit_note":
        match["transaction_type"] = "credit_note"
    if from_date or to_date:
        match["date"] = {}
        if from_date:
            match["date"]["$gte"] = from_date
        if to_date:
            match["date"]["$lte"] = to_date
    total = await db.tally_invoices.count_documents(match)
    rows = await db.tally_invoices.find(match, {"_id": 0, "sync_id": 0}).sort([("date", -1), ("voucher_number", -1)]).skip((page - 1) * page_size).limit(page_size).to_list(length=page_size)
    invoice_ids = [row.get("invoice_id") for row in rows if row.get("invoice_id")]
    links_by_party: dict[str, list[dict]] = {}
    async for link in db.tally_customer_links.find(
        {"org_id": org_id, "party_master_id": {"$in": [row.get("party_master_id") for row in rows]}}, {"_id": 0}
    ).sort([("is_primary", -1), ("created_at", 1)]):
        links_by_party.setdefault(link["party_master_id"], []).append(link)
    reminders = {}
    async for reminder in db.tally_payment_reminders.find(
        {"org_id": org_id, "invoice_id": {"$in": invoice_ids}, "status": {"$in": ["pending", "needs_review"]}}, {"_id": 0}
    ).sort("scheduled_at", 1):
        reminders.setdefault(reminder["invoice_id"], reminder)
    for row in rows:
        links = links_by_party.get(row.get("party_master_id"), [])
        origin_id = (row.get("billing_contact") or {}).get("conversation_id")
        row["customer_links"] = links
        row["customer_link"] = next(
            (link for link in links if link.get("conversation_id") == origin_id),
            next((link for link in links if link.get("is_primary")), links[0] if links else None),
        )
        row["next_reminder"] = reminders.get(row.get("invoice_id"))
        row["pdf"] = _pdf_client_view(row.get("pdf") or {}, row.get("alter_id", ""))
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
        {"$match": {"org_id": org_id, "transaction_type": {"$ne": "credit_note"}, "lifecycle_status": {"$nin": ["cancelled", "deleted", "optional", "post_dated"]}, "payment_status": {"$in": ["outstanding", "overdue"]}}},
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
        "invoices": await db.tally_invoices.count_documents({"org_id": org_id, "transaction_type": {"$ne": "credit_note"}, "lifecycle_status": {"$nin": ["cancelled", "deleted"]}}),
        "credit_notes": await db.tally_invoices.count_documents({"org_id": org_id, "transaction_type": "credit_note", "lifecycle_status": {"$nin": ["cancelled", "deleted"]}}),
        "cancelled_transactions": await db.tally_invoices.count_documents({"org_id": org_id, "lifecycle_status": "cancelled"}),
        "deleted_transactions": await db.tally_invoices.count_documents({"org_id": org_id, "lifecycle_status": "deleted"}),
        "connector_version": integration.get("connector_version") or "",
        "connector_capabilities": integration.get("connector_capabilities") or [],
    }
    version = str(overview["connector_version"])
    capabilities = overview["connector_capabilities"]
    overview["connector_update_required"] = connected and (
        not version.isdigit() or int(version) < 8 or "reports" not in capabilities or "invoice_pdf" not in capabilities
        or "invoice_readback" not in capabilities
    )
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
    if job.get("pdf"):
        result["pdf"] = serialize_mongo(_pdf_client_view(job["pdf"], job.get("invoice_alter_id", "")))
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
        {"org_id": org_id}, {"_id": 0, "request_xml": 0, "readback_request_xml": 0, "tally_response": 0, "customer": 0}
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


def _pdf_expires_at(pdf: dict) -> datetime | None:
    value = pdf.get("expires_at")
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            value = None
    if not isinstance(value, datetime):
        value = pdf.get("generated_at")
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                value = None
        value = value + timedelta(days=30) if isinstance(value, datetime) else None
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _pdf_is_current(pdf: dict, alter_id: str, now: datetime | None = None) -> bool:
    expiry = _pdf_expires_at(pdf or {})
    return bool(
        (pdf or {}).get("public_id") or (pdf or {}).get("url")
    ) and str((pdf or {}).get("alter_id")) == str(alter_id or "") and bool(expiry and expiry > (now or datetime.now(timezone.utc)))


def _pdf_client_view(pdf: dict, alter_id: str, now: datetime | None = None) -> dict:
    if not pdf:
        return {"ready": False}
    expiry = _pdf_expires_at(pdf)
    return {
        "ready": _pdf_is_current(pdf, alter_id, now), "alter_id": pdf.get("alter_id", ""),
        "generated_at": pdf.get("generated_at"), "expires_at": expiry,
    }


async def queue_invoice_pdf(org_id: str, user_id: str, invoice_id: str) -> tuple[dict, bool]:
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    pdf = invoice.get("pdf") or {}
    if _pdf_is_current(pdf, invoice.get("alter_id", "")):
        return {"status": "completed", "pdf": _pdf_client_view(pdf, invoice.get("alter_id", ""))}, False
    if invoice.get("lifecycle_status", "active") != "active":
        lifecycle = str(invoice.get("lifecycle_status") or "inactive").replace("_", " ")
        raise HTTPException(status_code=409, detail=f"This Tally voucher is {lifecycle} and cannot be pulled as a current PDF")
    integration = await get_integration(org_id)
    if "invoice_pdf" not in (integration.get("connector_capabilities") or []):
        raise HTTPException(status_code=409, detail="Download and restart the latest Tally connector to pull invoice PDFs")
    active = await db.tally_command_jobs.find_one({
        "org_id": org_id, "job_type": "invoice_pdf", "invoice_id": invoice_id,
        "invoice_alter_id": invoice.get("alter_id", ""), "status": {"$in": ["pending", "processing"]},
    }, {"_id": 0, "requests": 0})
    if active:
        return serialize_mongo(active), False
    now = datetime.now(timezone.utc)
    job = {
        "job_id": str(uuid.uuid4()), "job_type": "invoice_pdf", "org_id": org_id,
        "invoice_id": invoice_id, "invoice_master_id": invoice.get("master_id"),
        "invoice_alter_id": invoice.get("alter_id", ""), "status": "pending", "attempts": 0,
        "requests": [{"key": "invoice", "xml": build_invoice_export_xml(
            integration, str(invoice.get("master_id") or ""), invoice.get("date") or "",
        )}],
        "created_at": now, "updated_at": now, "created_by": user_id,
    }
    await db.tally_command_jobs.insert_one(job)
    return serialize_mongo({key: value for key, value in job.items() if key != "requests"}), True


async def get_invoice_pdf_download(org_id: str, invoice_id: str) -> dict:
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    pdf = invoice.get("pdf") or {}
    if not _pdf_is_current(pdf, invoice.get("alter_id", "")):
        raise HTTPException(status_code=409, detail="This PDF is not ready or has expired. Prepare it again from TallyPrime")
    try:
        from services.cloudinary_service import tally_invoice_pdf_download_url
        url = tally_invoice_pdf_download_url(pdf)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="HeidelAI could not prepare the secure PDF download") from exc
    filename = re.sub(r"[^A-Za-z0-9._-]", "-", invoice.get("voucher_number") or invoice_id)[:120]
    return {"download_url": url, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5), "filename": f"{filename}.pdf"}


def _validate_tally_pdf(data: bytes) -> None:
    if len(data) > 10_000_000 or not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Choose a valid PDF under 10 MB")


async def store_uploaded_tally_pdf(org_id: str, invoice_id: str, data: bytes) -> dict:
    _validate_tally_pdf(data)
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    from services.cloudinary_service import upload_tally_invoice_pdf
    pdf = await upload_tally_invoice_pdf(data, org_id, invoice_id, str(invoice.get("alter_id") or "current"))
    pdf["alter_id"] = invoice.get("alter_id", "")
    await db.tally_invoices.update_one({"_id": invoice["_id"]}, {"$set": {"pdf": pdf}})
    return serialize_mongo(_pdf_client_view(pdf, invoice.get("alter_id", "")))


async def store_tally_pdf_artifact(org_id: str, job_id: str, claim_id: str, data: bytes) -> dict:
    _validate_tally_pdf(data)
    job = await db.tally_command_jobs.find_one({
        "org_id": org_id, "job_id": job_id, "job_type": "invoice_pdf", "status": "processing", "claim_id": claim_id,
    })
    if not job:
        raise HTTPException(status_code=409, detail="This Tally PDF claim is no longer active")
    from services.cloudinary_service import upload_tally_invoice_pdf
    pdf = await upload_tally_invoice_pdf(data, org_id, job["invoice_id"], str(job.get("invoice_alter_id") or "current"))
    pdf["alter_id"] = job.get("invoice_alter_id", "")
    await db.tally_invoices.update_one({"org_id": org_id, "invoice_id": job["invoice_id"]}, {"$set": {"pdf": pdf}})
    await db.tally_command_jobs.update_one({"_id": job["_id"], "claim_id": claim_id}, {"$set": {"pdf": pdf}})
    return serialize_mongo(_pdf_client_view(pdf, job.get("invoice_alter_id", "")))


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


async def _party_candidates(org_id: str, customer: dict, mapped_master_id: str = "") -> list[dict]:
    parties = [row async for row in db.tally_ledgers.find(
        {"org_id": org_id, "parent": "Sundry Debtors"}, {"_id": 0, "sync_id": 0},
    )]
    rows = _strict_party_matches(parties, customer.get("gstin") or "", customer.get("phone") or "")
    mapped = next((party for party in parties if party.get("master_id") == mapped_master_id), None)
    if mapped and mapped not in rows:
        rows.insert(0, mapped)
    return serialize_mongo(rows)


async def _draft_items_from_quote(org_id: str, quote: dict) -> list[dict]:
    items = []
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
            "candidates": candidates, "selected_master_id": selected_master_id,
            "rate": float(_money(item.get("private_unit_price"))),
            "gst_rate": float(_money(item.get("gst_rate"))),
            "gst_inclusive": item.get("gst_inclusive") is True,
        })
    return items


async def _message_draft_source(org_id: str, conversation_id: str, requested_ids: list[str]) -> tuple[list[dict], dict | None, list[dict]]:
    offers = db[f"sales_offers_{org_id}"]
    query = {
        "org_id": org_id,
        "conversation_id": conversation_id,
        "delivery_status": {"$ne": "failed"},
        "status": {"$in": ["open", "accepted", "billed", "paid"]},
    }
    if requested_ids:
        rows = await offers.find({**query, "source_message_id": {"$in": requested_ids}}).sort("created_at", -1).to_list(length=20)
        matched = {str(row.get("source_message_id") or "") for row in rows}
        if matched != set(requested_ids):
            raise HTTPException(status_code=422, detail="Choose delivered quote or offer messages from this conversation")
        rows.sort(key=lambda row: requested_ids.index(str(row["source_message_id"])))
    else:
        rows = await offers.find({**query, "$or": [
            {"source_type": "formal_quote"}, {"line_items.0": {"$exists": True}},
        ]}).sort("created_at", -1).limit(1).to_list(length=1)
    if not rows:
        raise HTTPException(status_code=422, detail="No delivered structured quote or offer is available for billing")

    formal = [row for row in rows if row.get("source_type") == "formal_quote"]
    if len(formal) > 1 or (formal and len(rows) > 1):
        raise HTTPException(status_code=422, detail="Choose one current formal quotation for billing")
    quote = None
    items = []
    if formal:
        offer = formal[0]
        quote = await db[f"quotes_{org_id}"].find_one({
            "org_id": org_id, "conversation_id": conversation_id,
            "quote_id": offer.get("quote_id"),
        })
        if not quote:
            raise HTTPException(status_code=404, detail="The source quotation is no longer available")
        items = await _draft_items_from_quote(org_id, quote)
    else:
        for offer in rows:
            line_items = offer.get("line_items") or []
            if not line_items:
                raise HTTPException(status_code=422, detail="Choose a structured product offer for billing")
            for item in line_items:
                term = str(item.get("description") or "").strip()
                candidates = await _product_candidates(org_id, term)
                items.append({
                    "requested": term,
                    "quantity": str(Decimal(str(item.get("quantity") or 1)).normalize()),
                    "rate": float(_money(item.get("unit_price"))),
                    "gst_rate": float(_money(item.get("gst_rate"))),
                    "gst_inclusive": item.get("gst_inclusive") is True,
                    "candidates": candidates,
                    "selected_master_id": _exact_product_master_id(candidates, term),
                })
    previews = [{
        "message_id": str(row.get("source_message_id") or ""),
        "source_type": str(row.get("source_type") or "chat_price"),
        "preview": str(row.get("message_preview") or row.get("quote_number") or "Offer")[:240],
        "created_at": row.get("created_at"),
    } for row in rows]
    return items, quote, previews


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
        items = await _draft_items_from_quote(org_id, quote)
        reference = quote.get("quote_number") or quote["quote_id"]
        source_messages = []
    elif source == "messages":
        source_ids = list(dict.fromkeys(str(value).strip() for value in payload.get("source_message_ids") or [] if str(value).strip()))[:20]
        items, quote, source_messages = await _message_draft_source(org_id, conversation_id, source_ids)
        reference = (quote or {}).get("quote_number") or f"HDL-{uuid.uuid4().hex[:8].upper()}"
    else:
        raise HTTPException(status_code=422, detail="Invoice draft source must be command, quote, or messages")
    if source != "messages":
        source_messages = []
    customer = _customer_snapshot(quote or {}, conversation)
    mapping = await get_customer_mapping(org_id, conversation_id)
    party_candidates = await _party_candidates(org_id, customer, mapping.get("party_master_id") or "")
    mapped_master_id = mapping.get("party_master_id") or ""
    party_master_id = (
        mapped_master_id if any(row.get("master_id") == mapped_master_id for row in party_candidates)
        else party_candidates[0].get("master_id", "") if len(party_candidates) == 1
        else ""
    )
    selected_party = next((row for row in party_candidates if row.get("master_id") == party_master_id), {})
    party_address = {
        "name": selected_party.get("mailing_name") or selected_party.get("name") or customer.get("name"),
        "address": selected_party.get("address") or customer.get("billing_address") or customer.get("address"),
        "state": selected_party.get("state") or customer.get("state"),
        "pincode": selected_party.get("pincode") or customer.get("pincode"),
        "gstin": selected_party.get("gstin") or customer.get("gstin"),
    }
    bill_to = _clean_invoice_address(party_address)
    ship_to = _clean_invoice_address({
        **bill_to, "address": customer.get("shipping_address") or bill_to.get("address"),
    })
    now = datetime.now(timezone.utc)
    draft = {
        "draft_id": str(uuid.uuid4()), "org_id": org_id, "conversation_id": conversation_id,
        "source": source, "reference": reference, "quote_id": (quote or {}).get("quote_id"),
        "source_message_ids": [item["message_id"] for item in source_messages],
        "source_messages": source_messages,
        "quote_revision": int((quote or {}).get("revision_number") or 1), "customer": customer,
        "party_candidates": party_candidates, "party_master_id": party_master_id,
        "bill_to": bill_to, "ship_to": ship_to, "dispatch_from": {},
        "items": items, "status": "needs_review",
        "shipping": float(_money((quote or {}).get("shipping"))),
        "discount": float(_money((quote or {}).get("discount"))),
        "created_by": user_id, "created_by_type": "ai" if user_id == "ai-sales" else "user",
        "created_at": now, "updated_at": now,
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
    party_address = {
        "name": party.get("mailing_name") or party.get("name"), "address": party.get("address"),
        "state": party.get("state"), "pincode": party.get("pincode"), "gstin": party.get("gstin"),
    }
    bill_to = _clean_invoice_address(payload.get("bill_to"), _clean_invoice_address(draft.get("bill_to"), party_address))
    ship_to = _clean_invoice_address(payload.get("ship_to"), bill_to)
    dispatch_from = _clean_invoice_address(payload.get("dispatch_from"))
    for label, address in (("Bill To", bill_to), ("Ship To", ship_to)):
        if address.get("gstin") and not valid_gstin_format(address["gstin"]):
            raise HTTPException(status_code=422, detail=f"Enter a valid GSTIN for {label}")
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
            "bill_to": bill_to, "ship_to": ship_to, "dispatch_from": dispatch_from,
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
    if not _supports_invoice_readback(integration):
        raise HTTPException(status_code=409, detail="Download and restart Tally connector version 8 before creating invoices")
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
        "bill_to": draft.get("bill_to") or {}, "ship_to": draft.get("ship_to") or {},
        "dispatch_from": draft.get("dispatch_from") or {},
    }
    settings = {**normalized_settings(integration), "sales_ledger": draft["sales_ledger"]}
    request_xml = build_sales_voucher_xml(quote, settings, draft["party_ledger"], draft["tax_mode"])
    job = {
        "job_id": str(uuid.uuid4()), "org_id": org_id, "conversation_id": draft["conversation_id"],
        "quote_id": quote_id, "quote_revision": int(draft.get("quote_revision") or 1),
        "quote_number": draft["reference"], "draft_id": draft_id, "amount": float(_money(draft["total"])),
        "customer": draft.get("customer") or _customer_snapshot({}, conversation), "party_ledger": draft["party_ledger"],
        "tax_mode": draft["tax_mode"], "status": "pending", "request_xml": request_xml,
        "readback_request_xml": build_invoice_readback_xml(settings, quote["issued_on"]),
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
        if not stored:
            raise HTTPException(status_code=409, detail="The existing Tally invoice job could not be loaded")
        if stored.get("status") == "delivery_unknown":
            raise HTTPException(
                status_code=409,
                detail="Verify Tally Day Book before retrying this unconfirmed invoice",
            )
        if stored.get("status") == "failed":
            stored = await db.tally_bill_jobs.find_one_and_update(
                {"_id": stored["_id"], "status": "failed"},
                {"$set": {
                    "status": "pending", "request_xml": request_xml,
                    "readback_request_xml": build_invoice_readback_xml(settings, quote["issued_on"]), "draft_id": draft_id,
                    "amount": job["amount"], "outstanding_amount": job["outstanding_amount"],
                    "customer": job["customer"], "party_ledger": job["party_ledger"],
                    "tax_mode": job["tax_mode"], "created_by": user_id,
                    "created_by_name": user_name, "updated_at": now,
                }, "$unset": {
                    "error": "", "claim_id": "", "claimed_at": "", "tally_response": "",
                    "voucher_number": "", "master_id": "", "result_at": "",
                }},
                return_document=ReturnDocument.AFTER,
            ) or await db.tally_bill_jobs.find_one({"_id": stored["_id"]})
        if stored.get("status") == "delivery_unknown":
            raise HTTPException(
                status_code=409,
                detail="Verify Tally Day Book before retrying this unconfirmed invoice",
            )
        created = False
    draft_status = stored.get("status") if stored.get("status") in {"completed", "failed", "delivery_unknown"} else "queued"
    await db.tally_invoice_drafts.update_one(
        {"_id": draft["_id"]}, {"$set": {
            "status": draft_status, "job_id": stored["job_id"], "voucher_number": stored.get("voucher_number"),
            "error": stored.get("error"), "updated_at": now,
        }}
    )
    return serialize_mongo({
        key: value for key, value in stored.items()
        if key not in {"_id", "request_xml", "readback_request_xml", "tally_response"}
    }), created


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


async def link_tally_customer(
    org_id: str, user_id: str, party_master_id: str, conversation_id: str = "", *, phone: str = "", make_primary: bool = False,
) -> dict:
    party = await db.tally_ledgers.find_one({"org_id": org_id, "master_id": party_master_id, "parent": "Sundry Debtors"})
    if not party:
        raise HTTPException(status_code=404, detail="Tally customer ledger not found")
    conversation_id = str(conversation_id or "").strip()
    conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}) if conversation_id else None
    contact = await db[f"contacts_{org_id}"].find_one({"conversation_id": conversation_id, "platform": "whatsapp"}) if conversation_id else None
    phone = normalize_phone(phone or (conversation or {}).get("customer_id") or (contact or {}).get("phone_number") or conversation_id.removeprefix("whatsapp_"))
    if not re.fullmatch(r"\d{10,15}", phone):
        raise HTTPException(status_code=422, detail="Enter a valid WhatsApp number with country code")
    conversation_id = conversation_id or f"whatsapp_{phone}"
    if not conversation_id.startswith("whatsapp_"):
        raise HTTPException(status_code=422, detail="Choose a WhatsApp customer")
    link = await _upsert_customer_link(
        org_id, party, conversation_id, phone, user_id, match_source="manual", make_primary=make_primary,
    )
    return serialize_mongo(link)


async def unlink_tally_customer(org_id: str, party_master_id: str, conversation_id: str) -> None:
    link = await db.tally_customer_links.find_one_and_delete({
        "org_id": org_id, "party_master_id": party_master_id, "conversation_id": conversation_id,
    })
    if not link:
        raise HTTPException(status_code=404, detail="This WhatsApp number is not linked to the Tally customer")
    await db[f"conversations_{org_id}"].update_one(
        {"conversation_id": conversation_id, "tally_customer_mapping.party_master_id": party_master_id},
        {"$unset": {"tally_customer_mapping": ""}},
    )
    await db[f"contacts_{org_id}"].update_one(
        {"conversation_id": conversation_id, "tally_customer_mapping.party_master_id": party_master_id},
        {"$unset": {"tally_customer_mapping": ""}},
    )
    if link.get("is_primary"):
        await _promote_primary_link(org_id, party_master_id)


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
        "transaction_type": {"$ne": "credit_note"}, "lifecycle_status": {"$in": ["active", None]},
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


async def _invoice_customer_link(org_id: str, invoice: dict, conversation_id: str = "") -> dict | None:
    links = await db.tally_customer_links.find({
        "org_id": org_id, "party_master_id": invoice.get("party_master_id"),
    }).sort([("is_primary", -1), ("created_at", 1)]).to_list(length=100)
    preferred = conversation_id or (invoice.get("billing_contact") or {}).get("conversation_id")
    return next((link for link in links if link.get("conversation_id") == preferred), links[0] if links else None)


async def create_payment_reminder(org_id: str, user_id: str, user_name: str, invoice_id: str, scheduled_at: datetime) -> dict:
    invoice = await db.tally_invoices.find_one({"org_id": org_id, "invoice_id": invoice_id})
    if not invoice:
        raise HTTPException(status_code=404, detail="Tally invoice not found")
    if invoice.get("transaction_type", "invoice") != "invoice" or invoice.get("lifecycle_status", "active") != "active":
        raise HTTPException(status_code=409, detail="Payment reminders can only be sent for an active Tally invoice")
    if invoice.get("payment_status") == "unknown":
        raise HTTPException(status_code=409, detail="Enable bill-by-bill tracking for this Tally customer and sync again")
    if invoice.get("payment_status") == "paid" or float(invoice.get("outstanding_amount") or 0) <= 0:
        raise HTTPException(status_code=409, detail="This Tally invoice is already paid")
    link = await _invoice_customer_link(org_id, invoice)
    if not link:
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
        "recipient_conversation_id": link["conversation_id"], "recipient_phone": link["phone"],
        "created_at": now, "created_by": user_id, "created_by_name": user_name, "updated_at": now,
    }
    await queue_tally_sync(org_id, user_id, force=True)
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
        if feature_access(item["org_id"], "TALLY", now)["state"] != "active":
            continue
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
        elif invoice.get("transaction_type", "invoice") != "invoice" or invoice.get("lifecycle_status", "active") != "active":
            await db.tally_payment_reminders.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {"$set": {"status": "cancelled", "cancelled_at": now, "cancel_reason": "Voucher is no longer an active Tally invoice", "updated_at": now}, "$unset": {"claim_id": "", "claimed_at": ""}},
            )
            processed += 1
            continue
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
            try:
                await queue_tally_sync(claimed["org_id"], "payment-reminder", force=True)
                await db.tally_payment_reminders.update_one(
                    {"_id": claimed["_id"], "claim_id": claim_id},
                    {"$set": {"status": "pending", "scheduled_at": now + timedelta(minutes=1), "updated_at": now},
                     "$unset": {"claim_id": "", "claimed_at": ""}},
                )
                continue
            except HTTPException as exc:
                error = str(exc.detail)
        link = await _invoice_customer_link(
            claimed["org_id"], invoice or {}, claimed.get("recipient_conversation_id") or "",
        ) if invoice else None
        if not error and not link:
            error = "The Tally party is not linked to a WhatsApp customer"
        settings = normalized_settings(integration)
        if not error and not settings["reminder_template_name"]:
            error = "Payment reminders are not configured"
        elif not error and claimed.get("source") == "default" and not settings["reminders_enabled"]:
            error = "Automatic payment reminders are disabled"
        pdf = (invoice or {}).get("pdf") or {}
        if not error and not _pdf_is_current(pdf, invoice.get("alter_id", ""), now):
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
        pdf_url = ""
        if not error:
            try:
                from services.cloudinary_service import tally_invoice_pdf_download_url
                pdf_url = tally_invoice_pdf_download_url(pdf, expires_in_seconds=86400)
            except Exception:
                error = "The secure Tally invoice PDF link could not be created"
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
                {"type": "header", "parameters": [{"type": "document", "document": {"link": pdf_url, "filename": f"Invoice-{invoice['voucher_number']}.pdf"}}]},
                {"type": "body", "parameters": [
                    {"type": "text", "parameter_name": "customer_name", "text": invoice.get("party_ledger") or "Customer"},
                    {"type": "text", "parameter_name": "invoice_number", "text": invoice.get("voucher_number") or invoice["invoice_id"]},
                    {"type": "text", "parameter_name": "outstanding_amount", "text": f"INR {float(invoice.get('outstanding_amount') or 0):,.2f}"},
                    {"type": "text", "parameter_name": "due_date", "text": invoice.get("due_date") or invoice.get("date") or ""},
                ]},
            ],
        }
        if settings["reminder_template_parameter_format"] == "positional":
            for parameter in template["components"][1]["parameters"]:
                parameter.pop("parameter_name", None)
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


async def cleanup_expired_tally_pdfs() -> int:
    from services.cloudinary_service import cleanup_expired_tally_invoice_pdfs
    deleted = await cleanup_expired_tally_invoice_pdfs()
    if deleted is None:
        return 0
    now = datetime.now(timezone.utc)
    expired = {"$or": [
        {"pdf.expires_at": {"$lte": now}},
        {"pdf.expires_at": {"$exists": False}, "pdf.generated_at": {"$lte": now - timedelta(days=30)}},
    ]}
    invoices = await db.tally_invoices.update_many(expired, {"$unset": {"pdf": ""}})
    await db.tally_command_jobs.update_many({"job_type": "invoice_pdf", **expired}, {"$unset": {"pdf": ""}})
    return invoices.modified_count


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
    for name, index in (await db.tally_customer_links.index_information()).items():
        if index.get("key") == [("org_id", 1), ("party_master_id", 1)] and index.get("unique"):
            await db.tally_customer_links.drop_index(name)
    seen_links = set()
    duplicate_ids = []
    async for link in db.tally_customer_links.find({}).sort("updated_at", -1):
        key = (link.get("org_id"), link.get("conversation_id"))
        if key in seen_links:
            duplicate_ids.append(link["_id"])
        else:
            seen_links.add(key)
    if duplicate_ids:
        await db.tally_customer_links.delete_many({"_id": {"$in": duplicate_ids}})
    await db.tally_customer_links.create_index([("org_id", 1), ("party_master_id", 1)])
    await db.tally_customer_links.create_index([("org_id", 1), ("conversation_id", 1)], unique=True)
    await db.tally_payment_reminders.create_index("reminder_id", unique=True)
    await db.tally_payment_reminders.create_index("default_key", unique=True, sparse=True)
    await db.tally_payment_reminders.create_index([("status", 1), ("scheduled_at", 1)])
    await db.tally_profit_loss_reports.create_index([("org_id", 1), ("from_date", 1), ("to_date", 1)], unique=True)
    await db.tally_invoice_drafts.create_index("draft_id", unique=True)
    await db.tally_invoice_drafts.create_index([("org_id", 1), ("conversation_id", 1), ("created_at", -1)])
    await db.tally_outcome_outbox.create_index([("status", 1), ("next_attempt_at", 1)])
    await db.tally_outcome_outbox.create_index("expires_at", expireAfterSeconds=0)


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
    require_feature(integration["org_id"], "TALLY")
    now = datetime.now(timezone.utc)
    await db.tally_integrations.update_one(
        {"_id": integration["_id"], "connector_key_hash": integration["connector_key_hash"]},
        {"$set": {
            "last_seen_at": now, "connector_version": version[:40],
            "connector_capabilities": sorted({item.strip() for item in capabilities.split(",") if item.strip()}),
        }},
    )
    integration.update({
        "last_seen_at": now,
        "connector_version": version[:40],
        "connector_capabilities": sorted({item.strip() for item in capabilities.split(",") if item.strip()}),
    })
    if _snapshot_is_stale(integration, now):
        await queue_tally_sync(integration["org_id"], force=True)
    return integration


async def list_bills(org_id: str, conversation_id: str, user_id: str, role: str) -> list[dict]:
    await require_conversation_access(org_id, conversation_id, user_id, role)
    rows = await db.tally_bill_jobs.find(
        {"org_id": org_id, "conversation_id": conversation_id},
        {"_id": 0, "request_xml": 0, "readback_request_xml": 0, "connector_key_hash": 0, "tally_response": 0},
    ).sort("created_at", -1).to_list(length=100)
    return serialize_mongo(rows)


async def retry_bill(org_id: str, job_id: str, user_id: str, role: str) -> dict:
    job = await db.tally_bill_jobs.find_one({"org_id": org_id, "job_id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Tally bill not found")
    await require_conversation_access(org_id, job["conversation_id"], user_id, role)
    if job.get("status") not in {"failed", "delivery_unknown"}:
        raise HTTPException(status_code=409, detail="Only a failed or unconfirmed Tally bill can be retried")
    integration = await get_integration(org_id)
    if not _supports_invoice_readback(integration):
        raise HTTPException(status_code=409, detail="Download and restart Tally connector version 8 before retrying invoices")
    created_at = job.get("created_at")
    invoice_date = created_at.date().isoformat() if isinstance(created_at, datetime) else date.today().isoformat()
    updated = await db.tally_bill_jobs.find_one_and_update(
        {"_id": job["_id"], "status": job["status"]},
        {"$set": {
            "status": "pending", "error": None, "updated_at": datetime.now(timezone.utc),
            "readback_request_xml": build_invoice_readback_xml(integration, invoice_date),
        },
         "$unset": {"claim_id": "", "claimed_at": ""}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0, "request_xml": 0, "readback_request_xml": 0, "tally_response": 0},
    )
    if not updated:
        raise HTTPException(status_code=409, detail="This Tally bill changed. Refresh before retrying")
    return serialize_mongo(updated)


async def claim_next_job(integration: dict) -> dict | None:
    claim_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    job = await db.tally_command_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "status": "pending", "job_type": {"$ne": "sync_snapshot"}},
        {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now},
         "$inc": {"attempts": 1}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if job:
        return serialize_mongo(job)
    if _supports_invoice_readback(integration):
        job = await db.tally_bill_jobs.find_one_and_update(
            {"org_id": integration["org_id"], "status": "pending"},
            {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": now, "updated_at": now},
             "$inc": {"attempts": 1}},
            sort=[("created_at", 1)],
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if job:
            if not job.get("readback_request_xml"):
                created_at = job.get("created_at")
                invoice_date = created_at.date().isoformat() if isinstance(created_at, datetime) else date.today().isoformat()
                job["readback_request_xml"] = build_invoice_readback_xml(integration, invoice_date)
                await db.tally_bill_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job["job_id"], "claim_id": claim_id},
                    {"$set": {"readback_request_xml": job["readback_request_xml"]}},
                )
            job["job_type"] = "invoice_create"
            job["requests"] = [{"key": "invoice", "xml": job.pop("request_xml")}]
            return serialize_mongo(job)
    job = await db.tally_command_jobs.find_one_and_update(
        {"org_id": integration["org_id"], "status": "pending", "job_type": "sync_snapshot"},
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
                if command.get("triggered_by_job_id"):
                    parent = await db.tally_command_jobs.find_one({
                        "org_id": integration["org_id"], "job_id": command["triggered_by_job_id"], "job_type": "stock_change",
                    })
                    if parent:
                        from services.stock_service import fail_tally_stock_readback, finish_tally_stock_change
                        try:
                            await finish_tally_stock_change(
                                integration["org_id"], parent["job_id"], True, after_readback=True,
                            )
                        except Exception as stock_exc:
                            logger.exception("Tally stock read-back processing failed for %s", parent["job_id"])
                            await fail_tally_stock_readback(
                                integration["org_id"], parent["job_id"], type(stock_exc).__name__,
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
        elif command["job_type"] == "stock_change":
            from services.stock_service import finish_tally_stock_change
            await finish_tally_stock_change(
                integration["org_id"], command["job_id"], status == "completed", error,
                delivery_unknown=status == "delivery_unknown",
            )
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
            response = next((item for item in result.get("responses") or [] if item.get("key") == "invoice"), None)
            try:
                invoice = parse_invoice_export_response((response or {}).get("xml") or "", command["invoice_master_id"])
                settings = normalized_settings(integration)
                data = render_invoice_pdf(invoice, settings["company_name"], settings["sales_ledger"])
                _validate_tally_pdf(data)
                from services.cloudinary_service import upload_tally_invoice_pdf
                pdf = await upload_tally_invoice_pdf(
                    data, integration["org_id"], command["invoice_id"], str(command.get("invoice_alter_id") or "current"),
                )
                pdf["alter_id"] = command.get("invoice_alter_id", "")
                await db.tally_invoices.update_one(
                    {"org_id": integration["org_id"], "invoice_id": command["invoice_id"]}, {"$set": {"pdf": pdf}},
                )
                await db.tally_command_jobs.update_one(
                    {"org_id": integration["org_id"], "job_id": job_id}, {"$set": {"pdf": pdf, "updated_at": now}},
                )
                command["pdf"] = serialize_mongo(_pdf_client_view(pdf, command.get("invoice_alter_id", "")))
            except Exception as exc:
                status = "failed"
                error = str(exc.detail) if isinstance(exc, HTTPException) else f"HeidelAI could not generate the Tally invoice PDF: {type(exc).__name__}"
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
        elif existing_command.get("job_type") == "stock_change":
            from services.stock_service import finish_tally_stock_change
            await finish_tally_stock_change(
                integration["org_id"], existing_command["job_id"], status == "completed", existing_command.get("error"),
                delivery_unknown=status == "delivery_unknown",
            )
        return await get_tally_command_job(integration["org_id"], job_id)
    invoice_job = await db.tally_bill_jobs.find_one({"org_id": integration["org_id"], "job_id": job_id})
    if invoice_job and status == "completed":
        try:
            confirmed_invoice = validate_invoice_creation_readback(invoice_job, result)
            result = {
                **result,
                "master_id": confirmed_invoice["master_id"],
                "voucher_number": confirmed_invoice["voucher_number"],
            }
        except HTTPException as exc:
            status = "delivery_unknown"
            error = f"Tally accepted the invoice, but HeidelAI could not verify it: {exc.detail}"[:500]
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
        projection={"_id": 0, "request_xml": 0, "readback_request_xml": 0, "tally_response": 0},
    )
    if not updated:
        existing = await db.tally_bill_jobs.find_one(
            {"org_id": integration["org_id"], "job_id": job_id},
            {"_id": 0, "request_xml": 0, "readback_request_xml": 0, "tally_response": 0},
        )
        if existing and existing.get("status") == status:
            if status == "completed":
                await _remember_customer_mapping(integration["org_id"], existing)
                await queue_tally_sync(integration["org_id"], force=True)
                await _record_tally_outcomes(
                    integration["org_id"],
                    str(existing.get("conversation_id") or ""),
                    str(existing.get("quote_id") or ""),
                    int(existing.get("quote_revision") or 1),
                    f"tally-billed:{existing['job_id']}",
                    "billed",
                )
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
        await _record_tally_outcomes(
            integration["org_id"],
            str(updated.get("conversation_id") or ""),
            str(updated.get("quote_id") or ""),
            int(updated.get("quote_revision") or 1),
            f"tally-billed:{updated['job_id']}",
            "billed",
        )
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
    outcomes = await process_pending_tally_outcomes()
    return result.modified_count + commands.modified_count + outcomes
