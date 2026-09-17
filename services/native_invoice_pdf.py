"""Server-owned, plain-text renderer for immutable native invoice PDFs."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from PIL import Image

from services.native_invoice_assets import (
    InvoiceAssetError,
    InvoiceImage,
    validate_attachment_images,
    validate_invoice_image,
    validate_signature_image,
)
from services.india_tax import gst_state_name


RENDERER_VERSION = 3
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "native_invoice"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
DEFAULT_COLUMNS = ("description", "hsn_sac", "quantity", "unit_rate", "gst_rate", "tax", "total")
DEFAULT_LABELS = {
    "title": "TAX INVOICE",
    "supplier": "SUPPLIER",
    "recipient": "RECIPIENT / BILL TO",
    "delivery_address": "DELIVERY ADDRESS",
    "items": "ITEMS",
    "items_continued": "ITEMS (CONTINUED)",
    "gst_summary": "GST SUMMARY",
    "totals": "TOTALS",
    "notes": "NOTES",
    "terms": "TERMS AND CONDITIONS",
    "authorization": "AUTHORIZATION",
    "verification": "VERIFICATION",
    "attachments": "ATTACHMENT",
    "page": "Page",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "due_date": "Due date",
    "place_of_supply": "Place of supply",
    "reverse_charge": "Tax payable on reverse charge",
    "hsn_sac": "HSN/SAC",
    "quantity": "Quantity",
    "unit_rate": "Unit rate",
    "taxable": "Taxable",
    "gst_rate": "GST rate",
    "tax": "Tax",
    "line_total": "Line total",
    "subtotal": "Subtotal",
    "discount": "Discount",
    "taxable_value": "Taxable value",
    "invoice_total": "INVOICE TOTAL",
    "authorised_signatory": "Authorised signatory",
}


@dataclass(frozen=True)
class InvoicePresentation:
    page_format: str
    template_id: str
    orientation: str
    logo_size: str
    accent: tuple[int, int, int]
    labels: dict[str, str]
    columns: tuple[str, ...]


class InvoicePdfError(ValueError):
    pass


def _document_title(document: dict) -> str:
    return {
        "credit_note": "CREDIT NOTE",
        "debit_note": "DEBIT NOTE",
    }.get(document.get("_pdf_document_kind"), "TAX INVOICE")


def _presentation(invoice: dict) -> InvoicePresentation:
    raw = invoice.get("presentation") or {}
    if not isinstance(raw, dict) or set(raw) - {"page_format", "template_id", "orientation", "logo_size", "accent_color", "labels", "columns"}:
        raise InvoicePdfError("Invoice presentation contains unsupported settings")
    page_format = str(raw.get("page_format") or "A4").upper()
    if page_format not in {"A4", "LETTER"}:
        raise InvoicePdfError("Invoice page format must be A4 or LETTER")
    template_id = str(raw.get("template_id") or "classic")
    orientation = str(raw.get("orientation") or "portrait")
    logo_size = str(raw.get("logo_size") or "medium")
    if template_id not in {"classic", "compact", "branded"}:
        raise InvoicePdfError("Invoice template must be classic, compact, or branded")
    if orientation not in {"portrait", "landscape"}:
        raise InvoicePdfError("Invoice orientation must be portrait or landscape")
    if logo_size not in {"small", "medium", "large"}:
        raise InvoicePdfError("Invoice logo size must be small, medium, or large")
    color = str(raw.get("accent_color") or "#355267")
    if not _COLOR.fullmatch(color):
        raise InvoicePdfError("Invoice accent color must be a six-digit hex color")
    labels = dict(DEFAULT_LABELS)
    custom_labels = raw.get("labels") or {}
    if not isinstance(custom_labels, dict) or set(custom_labels) - set(DEFAULT_LABELS):
        raise InvoicePdfError("Invoice presentation contains unsupported labels")
    for key, value in custom_labels.items():
        clean = _text(value).strip()
        if not clean or len(clean) > 40 or "\n" in clean:
            raise InvoicePdfError(f"Invoice label {key} is invalid")
        labels[key] = clean
    columns = tuple(raw.get("columns") or DEFAULT_COLUMNS)
    if len(columns) != len(DEFAULT_COLUMNS) or len(set(columns)) != len(columns) or set(columns) != set(DEFAULT_COLUMNS):
        raise InvoicePdfError("Invoice columns must contain each statutory column exactly once")
    return InvoicePresentation(
        page_format="A4" if page_format == "A4" else "Letter",
        template_id=template_id,
        orientation=orientation,
        logo_size=logo_size,
        accent=tuple(int(color[index:index + 2], 16) for index in (1, 3, 5)),
        labels=labels,
        columns=columns,
    )


def _text(value: Any) -> str:
    return _CONTROL_CHARACTERS.sub(" ", str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " "))


def _money(paise: Any) -> str:
    return f"INR {int(paise or 0) / 100:,.2f}"


def _date(value: Any) -> str:
    if not value:
        return "-"
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return _text(value)
    return parsed.strftime("%d %b %Y")


def _state(code: Any) -> str:
    value = str(code or "").zfill(2)
    try:
        return f"{gst_state_name(value)} ({value})"
    except ValueError:
        raise InvoicePdfError(f"Unsupported GST state code: {value}") from None


@lru_cache(maxsize=1)
def _font_files() -> tuple[Path, ...]:
    primary = [FONT_DIR / "NotoSans-Regular.ttf", FONT_DIR / "NotoSans-Bold.ttf"]
    if not all(path.is_file() for path in primary):
        raise InvoicePdfError(f"Native invoice fonts are missing from {FONT_DIR}")
    return tuple(primary + sorted(path for path in FONT_DIR.glob("NotoSans*.ttf") if path not in primary))


def _supported_codepoints(files: tuple[Path, ...]) -> set[int]:
    supported: set[int] = set()
    for path in files:
        with TTFont(path, lazy=True) as font:
            for table in font["cmap"].tables:
                if table.isUnicode():
                    supported.update(table.cmap)
    return supported


@lru_cache(maxsize=1)
def _bundled_supported_codepoints() -> frozenset[int]:
    return frozenset(_supported_codepoints(_font_files()))


def _rendered_strings(invoice: dict):
    for field in (
        "invoice_number", "invoice_date", "note_date", "due_date", "shipping_address", "authorized_signatory", "notes",
        "void_reason", "reason", "source_invoice_id", "source_invoice_number", "source_invoice_date",
        "einvoice_exemption_declaration",
    ):
        if invoice.get(field) is not None:
            yield _text(invoice[field])
    for party_name in ("seller", "customer"):
        for value in (invoice.get(party_name) or {}).values():
            if isinstance(value, str):
                yield _text(value)
    for item in invoice.get("items") or []:
        for field in ("description", "hsn_sac", "quantity", "uqc"):
            if item.get(field) is not None:
                yield _text(item[field])
    for term in invoice.get("terms") or []:
        yield _text(term)
    for reference in invoice.get("document_references") or []:
        for value in reference.values():
            if isinstance(value, (str, date)):
                yield _text(value)
    for value in (invoice.get("payment_profile") or {}).values():
        if isinstance(value, str):
            yield _text(value)
    for collection in ("payments", "refunds"):
        for entry in invoice.get(collection) or []:
            for value in entry.values():
                if isinstance(value, str):
                    yield _text(value)
    for value in ((invoice.get("presentation") or {}).get("labels") or {}).values():
        yield _text(value)
    for asset in [invoice.get("signature_image"), invoice.get("logo_image"), *(invoice.get("attachments") or [])]:
        if asset:
            yield _text(asset.get("filename"))


def assert_invoice_text_supported(invoice: dict, font_files: tuple[Path, ...] | None = None) -> None:
    """Fail closed instead of silently dropping a customer's characters."""
    supported = _supported_codepoints(font_files) if font_files is not None else _bundled_supported_codepoints()
    shaping_controls = {"\u200c", "\u200d"}
    missing = sorted({character for value in _rendered_strings(invoice) for character in value if character != "\n" and character not in shaping_controls and ord(character) not in supported})
    if missing:
        preview = " ".join(f"U+{ord(character):04X}" for character in missing[:8])
        raise InvoicePdfError(f"Invoice text contains unsupported characters: {preview}")


class _InvoicePdf(FPDF):
    def __init__(self, invoice: dict, presentation: InvoicePresentation, watermark: str | None):
        super().__init__(orientation="L" if presentation.orientation == "landscape" else "P", format=presentation.page_format, unit="mm")
        self.invoice = invoice
        self.presentation = presentation
        self.watermark = watermark
        margin = 10 if presentation.template_id == "compact" else 14
        self.set_margins(margin, 16 if presentation.template_id == "compact" else 20, margin)
        self.set_auto_page_break(auto=True, margin=14 if presentation.template_id == "compact" else 17)

    def header(self):
        if self.watermark:
            self.set_font("Noto", "B", 42)
            self.set_text_color(229, 229, 229) if self.watermark == "PAID" else self.set_text_color(241, 218, 218)
            with self.rotation(35, self.w / 2, self.h / 2):
                self.set_xy(0, self.h / 2 - 10)
                self.cell(self.w, 20, self.watermark, align="C")
            self.set_xy(self.l_margin, self.t_margin)
        self.set_text_color(*self.presentation.accent)
        self.set_font("Noto", "B", 9)
        self.cell(95, 5, _text((self.invoice.get("seller") or {}).get("name") or "Organization"))
        self.set_font("Noto", "", 8)
        name = _document_title(self.invoice).title()
        state = {"draft": "DRAFT preview", "void": f"VOID {name}"}.get(self.invoice.get("lifecycle"), name)
        self.cell(0, 5, f"{state} - {_text(self.invoice.get('invoice_number'))}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(210, 210, 205)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(4)

    def footer(self):
        self.set_y(-12)
        self.set_text_color(100, 100, 100)
        self.set_font("Noto", "", 7)
        state = "VOID | " if self.invoice.get("lifecycle") == "void" else ""
        document_name = _document_title(self.invoice).lower()
        self.cell(0, 4, f"{state}Electronically generated {document_name} | {self.presentation.labels['page']} {self.page_no()}", align="C")


def _configure_fonts(pdf: _InvoicePdf, files: tuple[Path, ...]) -> None:
    regular = FONT_DIR / "NotoSans-Regular.ttf"
    bold = FONT_DIR / "NotoSans-Bold.ttf"
    pdf.add_font("Noto", "", str(regular))
    pdf.add_font("Noto", "B", str(bold))
    fallback_families = []
    for path in files:
        match = re.fullmatch(r"(.+)-(Regular|Bold)\.ttf", path.name)
        if not match or match.group(1) == "NotoSans":
            continue
        family = match.group(1)
        style = "B" if match.group(2) == "Bold" else ""
        pdf.add_font(family, style, str(path))
        if family not in fallback_families:
            fallback_families.append(family)
    if fallback_families:
        pdf.set_fallback_fonts(fallback_families, exact_match=False)
    pdf.set_text_shaping(True)


def _section(pdf: _InvoicePdf, title: str) -> None:
    pdf.ln(2)
    accent = pdf.presentation.accent
    pdf.set_fill_color(*(round(channel + (255 - channel) * 0.88) for channel in accent))
    pdf.set_text_color(*accent)
    pdf.set_font("Noto", "B", 9)
    pdf.cell(0, 7, title, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(1)


def _paragraph(pdf: _InvoicePdf, value: Any, *, bold: bool = False, size: float = 8.5) -> None:
    pdf.set_text_color(35, 35, 35)
    pdf.set_font("Noto", "B" if bold else "", size)
    pdf.multi_cell(0, 4.5, _text(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT, wrapmode="CHAR")


def _party(pdf: _InvoicePdf, title: str, party: dict) -> None:
    _section(pdf, title)
    _paragraph(pdf, party.get("name") or "-", bold=True, size=9.5)
    legal = " | ".join(filter(None, [_text(party.get("legal_name")), _text(party.get("trade_name"))]))
    if legal and legal != _text(party.get("name")):
        _paragraph(pdf, legal)
    if party.get("gstin"):
        _paragraph(pdf, f"GSTIN: {party['gstin']}")
    _paragraph(pdf, f"State: {_state(party.get('state_code'))}")
    _paragraph(pdf, "Country: India")
    _paragraph(pdf, party.get("address") or "-")
    locality = " ".join(filter(None, [_text(party.get("location")), _text(party.get("pincode"))]))
    if locality:
        _paragraph(pdf, locality)
    contact = " | ".join(filter(None, [_text(party.get("phone")), _text(party.get("email"))]))
    if contact:
        _paragraph(pdf, contact)


def _issued_at(invoice: dict) -> datetime:
    value = invoice.get("issued_at")
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            pass
    invoice_date = date.fromisoformat(str(invoice["invoice_date"])[:10])
    return datetime.combine(invoice_date, time(), tzinfo=timezone.utc)


def _watermark(invoice: dict, *, final: bool) -> str | None:
    lifecycle = invoice.get("lifecycle")
    if lifecycle == "draft":
        return "DRAFT"
    if lifecycle == "void":
        return "VOID"
    total = int(invoice.get("total_paise") or 0)
    if not final and total > 0 and int(invoice.get("amount_paid_paise") or 0) >= total:
        return "PAID"
    return None


def _item_column(item: dict, column: str, labels: dict[str, str]) -> tuple[str, bool]:
    if column == "description":
        line_kind = str(item.get("line_kind") or "item")
        prefix = {"shipping": "Shipping: ", "surcharge": "Surcharge: "}.get(line_kind, "")
        return prefix + _text(item.get("description") or "-"), True
    if column == "hsn_sac":
        return f"{labels['hsn_sac']}: {item.get('hsn_sac') or '-'}", False
    if column == "quantity":
        return f"{labels['quantity']}: {item.get('quantity') or 0} {item.get('uqc') or ''}", False
    if column == "unit_rate":
        basis = "GST inclusive" if item.get("gst_inclusive") else "before GST"
        return f"{labels['unit_rate']}: {_money(item.get('unit_price_paise'))} ({basis})", False
    if column == "gst_rate":
        return f"{labels['gst_rate']}: {Decimal(str(item.get('gst_rate_bps') or 0)) / 100}%", False
    if column == "tax":
        taxes = [
            f"{label}: {_money(item[key])}"
            for label, key in (("CGST", "cgst_paise"), ("SGST", "sgst_paise"), ("IGST", "igst_paise"))
            if int(item.get(key) or 0)
        ]
        return f"{labels['taxable']}: {_money(item.get('taxable_value_paise'))} | {labels['tax']}: {' | '.join(taxes) or 'INR 0.00'}", False
    return f"{labels['line_total']}: {_money(item.get('total_paise'))}", False


def _draw_image(pdf: _InvoicePdf, image: InvoiceImage, *, x: float, y: float, max_width: float, max_height: float) -> float:
    scale = min(max_width / image.width, max_height / image.height)
    width = image.width * scale
    height = image.height * scale
    stream = BytesIO(image.data)
    stream.name = image.filename
    pdf.image(stream, x=x, y=y, w=width, h=height)
    return height


def _qr_png(payload: Any, *, filename: str = "signed-qr.png") -> InvoiceImage | None:
    if payload in (None, ""):
        return None
    value = str(payload)
    if len(value.encode("utf-8")) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
        raise InvoicePdfError("Signed QR payload is invalid or exceeds 4096 bytes")
    try:
        import qrcode
    except ImportError as exc:
        raise InvoicePdfError("QR rendering dependency is unavailable") from exc
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=5, border=4)
    qr.add_data(value)
    try:
        qr.make(fit=True)
    except (ValueError, OverflowError) as exc:
        raise InvoicePdfError("Signed QR payload cannot be encoded") from exc
    output = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(output, format="PNG")
    image = output.getvalue()
    with Image.open(BytesIO(image)) as decoded:
        width, height = decoded.size
    return InvoiceImage(filename, "image/png", image, width, height, hashlib.sha256(image).hexdigest())


def _payment_qr_png(invoice: dict) -> InvoiceImage | None:
    profile = invoice.get("payment_profile") or {}
    if not profile.get("qr_enabled"):
        return None
    upi_id = str(profile.get("upi_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}@[A-Za-z0-9.-]{2,32}", upi_id):
        raise InvoicePdfError("Payment profile UPI ID is invalid")
    payload = "upi://pay?" + urlencode({
        "pa": upi_id,
        "pn": str(profile.get("account_name") or (invoice.get("seller") or {}).get("name") or "Supplier")[:80],
        "am": f"{int(invoice.get('total_paise') or 0) / 100:.2f}",
        "cu": "INR",
        "tn": f"Invoice {invoice.get('invoice_number') or 'draft'}"[:80],
    })
    return _qr_png(payload, filename="payment-upi-qr.png")


def render_native_invoice_pdf(invoice: dict, *, final: bool = True) -> bytes:
    """Render a final invoice, or an explicitly non-final watermarked preview."""
    lifecycle = invoice.get("lifecycle")
    if lifecycle == "draft" and final:
        raise InvoicePdfError("A draft can be rendered only as a watermarked preview")
    if lifecycle not in {"draft", "issued", "void"} or (lifecycle != "draft" and not invoice.get("invoice_number")):
        raise InvoicePdfError("Only a draft preview or numbered issued invoice can be rendered")
    presentation = _presentation(invoice)
    files = _font_files()
    assert_invoice_text_supported(invoice, files)
    try:
        signature = validate_signature_image(invoice.get("signature_image"))
        logo = validate_invoice_image(invoice["logo_image"], max_bytes=64 * 1024) if invoice.get("logo_image") else None
        attachments = validate_attachment_images(invoice.get("attachments"))
    except InvoiceAssetError as exc:
        raise InvoicePdfError(str(exc)) from exc
    qr_image = _qr_png(invoice.get("signed_qr_payload"))
    payment_qr = _payment_qr_png(invoice)
    document_title = _document_title(invoice)
    is_adjustment = invoice.get("_pdf_document_kind") in {"credit_note", "debit_note"}

    pdf = _InvoicePdf(invoice, presentation, _watermark(invoice, final=final))
    _configure_fonts(pdf, files)
    document_number = _text(invoice.get("invoice_number") or "DRAFT")
    pdf.set_title(f"{'Draft ' if lifecycle == 'draft' else ''}{document_title.title()} {document_number}")
    pdf.set_author(_text((invoice.get("seller") or {}).get("name") or "HeidelAI"))
    pdf.set_subject("Draft document preview" if lifecycle == "draft" else f"GST {document_title.lower()}")
    pdf.set_creator(f"HeidelAI Native Invoice Renderer/{RENDERER_VERSION}")
    pdf.set_creation_date(_issued_at(invoice))
    pdf.add_page()

    labels = presentation.labels
    if presentation.template_id == "branded":
        pdf.set_fill_color(*presentation.accent)
        pdf.rect(pdf.l_margin, pdf.get_y(), pdf.w - pdf.l_margin - pdf.r_margin, 2.5, style="F")
        pdf.ln(5)
    if logo:
        max_width, max_height = {"small": (28, 14), "medium": (42, 20), "large": (56, 26)}[presentation.logo_size]
        image_y = pdf.get_y()
        height = _draw_image(pdf, logo, x=pdf.w - pdf.r_margin - max_width, y=image_y, max_width=max_width, max_height=max_height)
        pdf.set_y(image_y + height + 2)
    pdf.set_text_color(*presentation.accent)
    pdf.set_font("Noto", "B", 20)
    base_title = document_title if is_adjustment else labels["title"]
    title = f"VOID {base_title}" if lifecycle == "void" else f"DRAFT - {base_title}" if lifecycle == "draft" else base_title
    pdf.cell(0, 10, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Noto", "", 9)
    if lifecycle == "void":
        _paragraph(pdf, f"Voided: {_date(invoice.get('voided_at'))} | Reason: {invoice.get('void_reason') or '-'}", bold=True)
    if is_adjustment:
        _paragraph(pdf, f"Note number: {document_number} | Note date: {_date(invoice.get('invoice_date'))}")
        source_reference = invoice.get("source_invoice_number") or invoice.get("source_invoice_id") or "-"
        _paragraph(pdf, f"Original invoice: {source_reference} dated {_date(invoice.get('source_invoice_date'))} | Reason: {invoice.get('reason') or '-'}", bold=True)
    else:
        _paragraph(pdf, f"{labels['invoice_number']}: {document_number} | {labels['invoice_date']}: {_date(invoice.get('invoice_date'))} | {labels['due_date']}: {_date(invoice.get('due_date'))}")
    _paragraph(pdf, f"{labels['place_of_supply']}: {_state(invoice.get('place_of_supply_state_code'))} | {labels['reverse_charge']}: {'Yes' if invoice.get('reverse_charge') else 'No'}")

    _party(pdf, labels["supplier"], invoice.get("seller") or {})
    _party(pdf, labels["recipient"], invoice.get("customer") or {})
    if invoice.get("shipping_address"):
        _section(pdf, labels["delivery_address"])
        _paragraph(pdf, invoice["shipping_address"])
        _paragraph(pdf, f"State: {_state(invoice.get('shipping_state_code') or (invoice.get('customer') or {}).get('state_code'))}")

    if invoice.get("document_references"):
        _section(pdf, "DOCUMENT REFERENCES")
        for reference in invoice["document_references"]:
            label = str(reference.get("kind") or "other").replace("_", " ").title()
            dated = f" dated {_date(reference.get('date'))}" if reference.get("date") else ""
            note = f" | {reference.get('note')}" if reference.get("note") else ""
            _paragraph(pdf, f"{label}: {reference.get('reference')}{dated}{note}")

    _section(pdf, labels["items"])
    for index, item in enumerate(invoice.get("items") or [], start=1):
        if pdf.get_y() > pdf.h - 48:
            pdf.add_page()
            _section(pdf, labels["items_continued"])
        details = []
        for column in presentation.columns:
            value, bold = _item_column(item, column, labels)
            if bold:
                if details:
                    _paragraph(pdf, " | ".join(details))
                    details = []
                _paragraph(pdf, f"{index}. {value}", bold=True, size=9)
            else:
                details.append(value)
                if len(details) == 2:
                    _paragraph(pdf, " | ".join(details))
                    details = []
        if details:
            _paragraph(pdf, " | ".join(details))
        pdf.set_draw_color(225, 225, 220)
        pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.w - pdf.r_margin, pdf.get_y() + 1)
        pdf.ln(3)

    _section(pdf, labels["gst_summary"])
    rates: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in invoice.get("items") or []:
        bucket = rates[int(item.get("gst_rate_bps") or 0)]
        for key in ("taxable_value_paise", "cgst_paise", "sgst_paise", "igst_paise"):
            bucket[key] += int(item.get(key) or 0)
    for rate in sorted(rates):
        bucket = rates[rate]
        _paragraph(pdf, f"{Decimal(rate) / 100}% | {labels['taxable']}: {_money(bucket['taxable_value_paise'])} | CGST: {_money(bucket['cgst_paise'])} | SGST: {_money(bucket['sgst_paise'])} | IGST: {_money(bucket['igst_paise'])}")

    _section(pdf, labels["totals"])
    _paragraph(pdf, f"{labels['subtotal']}: {_money(invoice.get('subtotal_paise'))}")
    if int(invoice.get("discount_paise") or 0):
        _paragraph(pdf, f"{labels['discount']}: {_money(invoice.get('discount_paise'))}")
    _paragraph(pdf, f"{labels['taxable_value']}: {_money(invoice.get('taxable_value_paise'))}")
    _paragraph(pdf, f"CGST: {_money(invoice.get('cgst_paise'))} | SGST: {_money(invoice.get('sgst_paise'))} | IGST: {_money(invoice.get('igst_paise'))}")
    _paragraph(pdf, f"{labels['invoice_total']}: {_money(invoice.get('total_paise'))}", bold=True, size=11)

    payment_profile = invoice.get("payment_profile") or {}
    legacy_payment = (invoice.get("seller") or {}).get("payment_details")
    if payment_profile or legacy_payment:
        _section(pdf, "PAYMENT DETAILS")
        if payment_profile:
            if payment_profile.get("account_name"):
                _paragraph(pdf, f"Account name: {payment_profile['account_name']}")
            if payment_profile.get("bank_name"):
                _paragraph(pdf, f"Bank: {payment_profile['bank_name']}")
            if payment_profile.get("account_number"):
                _paragraph(pdf, f"Account: {payment_profile['account_number']} | IFSC: {payment_profile.get('ifsc') or '-'}")
            if payment_profile.get("upi_id"):
                _paragraph(pdf, f"UPI ID: {payment_profile['upi_id']}")
        else:
            _paragraph(pdf, legacy_payment)
        if payment_qr:
            image_y = pdf.get_y() + 1
            height = _draw_image(pdf, payment_qr, x=pdf.l_margin, y=image_y, max_width=42, max_height=42)
            pdf.set_xy(pdf.l_margin + 47, image_y)
            _paragraph(pdf, "UPI payment QR. This is not the signed IRP verification QR.", bold=True)
            pdf.set_y(max(pdf.get_y(), image_y + height + 2))

    if invoice.get("einvoice_exemption_declaration"):
        _section(pdf, "E-INVOICE EXEMPTION DECLARATION")
        _paragraph(pdf, invoice["einvoice_exemption_declaration"])

    if invoice.get("notes"):
        _section(pdf, labels["notes"])
        _paragraph(pdf, invoice["notes"])
    if invoice.get("terms"):
        _section(pdf, labels["terms"])
        for index, term in enumerate(invoice["terms"], start=1):
            _paragraph(pdf, f"{index}. {term}")
    if pdf.get_y() > pdf.h - (55 if signature else 32):
        pdf.add_page()
    _section(pdf, labels["authorization"])
    _paragraph(pdf, f"For {(invoice.get('seller') or {}).get('name') or 'Organization'}", bold=True)
    if signature:
        image_y = pdf.get_y()
        height = _draw_image(pdf, signature, x=pdf.l_margin, y=image_y, max_width=48, max_height=24)
        pdf.set_y(image_y + height + 2)
    _paragraph(pdf, f"{labels['authorised_signatory']}: {invoice.get('authorized_signatory') or 'Authorised representative'}")
    _paragraph(pdf, f"Electronically generated {document_title.lower()}.", size=7.5)

    if qr_image:
        if pdf.get_y() > pdf.h - 70:
            pdf.add_page()
        _section(pdf, labels["verification"])
        image_y = pdf.get_y()
        height = _draw_image(pdf, qr_image, x=pdf.l_margin, y=image_y, max_width=55, max_height=55)
        pdf.set_xy(pdf.l_margin + 60, image_y)
        _paragraph(pdf, "Scan this signed QR payload to verify the registered invoice data.")
        if invoice.get("irn"):
            _paragraph(pdf, f"IRN: {invoice['irn']}")
        pdf.set_y(max(pdf.get_y(), image_y + height + 2))

    for index, attachment in enumerate(attachments, start=1):
        pdf.add_page()
        _section(pdf, f"{labels['attachments']} {index}/{len(attachments)}")
        _paragraph(pdf, attachment.filename, bold=True)
        image_y = pdf.get_y() + 1
        _draw_image(
            pdf,
            attachment,
            x=pdf.l_margin,
            y=image_y,
            max_width=pdf.w - pdf.l_margin - pdf.r_margin,
            max_height=pdf.h - image_y - 20,
        )

    return bytes(pdf.output())


def render_native_adjustment_pdf(adjustment: dict, *, final: bool = True) -> bytes:
    """Render an immutable credit/debit note using the native document renderer."""
    kind = adjustment.get("document_kind")
    if kind not in {"credit_note", "debit_note"}:
        raise InvoicePdfError("Adjustment document kind must be credit_note or debit_note")
    normalized = deepcopy(adjustment)
    normalized.update({
        "_pdf_document_kind": kind,
        "invoice_number": adjustment.get("note_number"),
        "invoice_date": adjustment.get("note_date"),
        "due_date": None,
    })
    return render_native_invoice_pdf(normalized, final=final)


def render_native_receipt_pdf(invoice: dict) -> bytes:
    """Render payment status without changing or impersonating the tax invoice."""
    if invoice.get("lifecycle") not in {"issued", "void"} or not invoice.get("invoice_number"):
        raise InvoicePdfError("A receipt requires a numbered issued or void invoice")
    presentation = _presentation(invoice)
    files = _font_files()
    assert_invoice_text_supported(invoice, files)
    pdf = FPDF(
        orientation="L" if presentation.orientation == "landscape" else "P",
        format=presentation.page_format,
        unit="mm",
    )
    pdf.set_margins(14, 16, 14)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.presentation = presentation
    _configure_fonts(pdf, files)
    receipt_timestamp = _issued_at({**invoice, "issued_at": invoice.get("updated_at") or invoice.get("issued_at")})
    pdf.set_title(f"Payment status {invoice['invoice_number']}")
    pdf.set_author(_text((invoice.get("seller") or {}).get("name") or "HeidelAI"))
    pdf.set_subject("Payment receipt and status; not a tax invoice")
    pdf.set_creator(f"HeidelAI Native Invoice Renderer/{RENDERER_VERSION}")
    pdf.set_creation_date(receipt_timestamp)
    pdf.add_page()
    pdf.set_text_color(*presentation.accent)
    pdf.set_font("Noto", "B", 18)
    pdf.cell(0, 10, "PAYMENT RECEIPT / STATUS", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _paragraph(pdf, "This records payment status only. It is not a tax invoice.", bold=True)
    _paragraph(pdf, f"Invoice: {invoice['invoice_number']} | Invoice date: {_date(invoice.get('invoice_date'))}")
    _paragraph(pdf, f"Supplier: {(invoice.get('seller') or {}).get('name') or '-'} | Customer: {(invoice.get('customer') or {}).get('name') or '-'}")
    original = int(invoice.get("total_paise") or 0)
    adjusted = max(0, original + int(invoice.get("debit_total_paise") or 0) - int(invoice.get("credit_total_paise") or 0))
    net_paid = max(0, int(invoice.get("amount_paid_paise") or 0) - int(invoice.get("amount_refunded_paise") or 0))
    _section(pdf, "STATUS")
    _paragraph(pdf, f"Original total: {_money(original)} | Adjusted total: {_money(adjusted)}")
    _paragraph(pdf, f"Net received: {_money(net_paid)} | Balance: {_money(max(0, adjusted - net_paid))} | Refund due: {_money(max(0, net_paid - adjusted))}", bold=True)
    _section(pdf, "PAYMENTS")
    payments = [entry for entry in invoice.get("payments") or [] if not entry.get("reversed_at")]
    if not payments:
        _paragraph(pdf, "No active payments recorded.")
    for entry in payments:
        _paragraph(pdf, f"{_date(entry.get('paid_on'))} | {_money(entry.get('amount_paise'))} | {str(entry.get('method') or 'unspecified').replace('_', ' ')} | Ref: {entry.get('reference') or '-'}")
    refunds = [entry for entry in invoice.get("refunds") or [] if not entry.get("reversed_at")]
    if refunds:
        _section(pdf, "REFUNDS")
        for entry in refunds:
            _paragraph(pdf, f"{_date(entry.get('refunded_on'))} | {_money(entry.get('amount_paise'))} | {str(entry.get('method') or 'unspecified').replace('_', ' ')} | Ref: {entry.get('reference') or '-'}")
    _paragraph(pdf, f"Generated at {receipt_timestamp.isoformat()}", size=7.5)
    return bytes(pdf.output())
