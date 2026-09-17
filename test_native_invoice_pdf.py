import importlib.util
from io import BytesIO
from pathlib import Path
import unittest

from PIL import Image, ImageDraw

from services.native_invoice_pdf import (
    FONT_DIR,
    InvoicePdfError,
    render_native_adjustment_pdf,
    render_native_invoice_pdf,
    render_native_receipt_pdf,
)


def invoice():
    item = {
        "description": "वर्कस्टेशन — final sentinel",
        "hsn_sac": "8471",
        "quantity": "2",
        "uqc": "NOS",
        "unit_price_paise": 10_000,
        "gst_rate_bps": 1800,
        "taxable_value_paise": 20_000,
        "cgst_paise": 1_800,
        "sgst_paise": 1_800,
        "igst_paise": 0,
        "total_paise": 23_600,
    }
    return {
        "invoice_id": "invoice-1",
        "invoice_number": "INV/26-27/000001",
        "lifecycle": "issued",
        "invoice_date": "2026-09-17",
        "due_date": "2026-09-30",
        "issued_at": "2026-09-17T10:00:00+00:00",
        "seller": {"name": "परीक्षण विक्रेता", "gstin": "27AAPFU0939F1ZV", "state_code": "27", "address": "मुंबई, महाराष्ट्र"},
        "customer": {"name": "Customer", "gstin": "", "state_code": "29", "address": "Bengaluru, Karnataka"},
        "place_of_supply_state_code": "29",
        "shipping_address": "Delivery address",
        "shipping_state_code": "29",
        "reverse_charge": False,
        "authorized_signatory": "Authorised person",
        "items": [dict(item, description=f"{index}: {item['description']}") for index in range(100)],
        "subtotal_paise": 2_000_000,
        "discount_paise": 0,
        "taxable_value_paise": 2_000_000,
        "cgst_paise": 180_000,
        "sgst_paise": 180_000,
        "igst_paise": 0,
        "total_paise": 2_360_000,
        "notes": "Every stored note is rendered. notes-final-sentinel",
        "terms": ["Every stored term is rendered.", "terms-final-sentinel"],
    }


def image_asset(filename="signature.png"):
    output = BytesIO()
    image = Image.new("RGBA", (320, 100), (255, 255, 255, 0))
    ImageDraw.Draw(image).line((10, 80, 310, 20), fill=(30, 30, 30, 255), width=4)
    image.save(output, format="PNG")
    data = output.getvalue()
    return {"filename": filename, "content_type": "image/png", "byte_size": len(data), "data": data}


@unittest.skipUnless((FONT_DIR / "NotoSans-Regular.ttf").exists(), "bundled Noto fonts not installed")
class NativeInvoicePdfTests(unittest.TestCase):
    def test_unicode_multipage_pdf_is_rendered_without_truncation(self):
        data = render_native_invoice_pdf(invoice())
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(data.count(b"/Type /Page"), 1)
        self.assertGreater(len(data), 10_000)
        self.assertEqual(data, render_native_invoice_pdf(invoice()))

    def test_unsupported_glyph_fails_closed(self):
        value = invoice()
        value["customer"]["name"] = "Unsupported emoji \U0001f680"
        with self.assertRaisesRegex(InvoicePdfError, "unsupported characters"):
            render_native_invoice_pdf(value)

    def test_void_pdf_is_a_distinct_artifact(self):
        value = invoice()
        issued = render_native_invoice_pdf(value)
        value.update({"lifecycle": "void", "voided_at": "2026-09-18T10:00:00+00:00", "void_reason": "Entered in error"})
        self.assertNotEqual(issued, render_native_invoice_pdf(value))

    def test_presentation_signature_attachment_and_draft_preview(self):
        value = invoice()
        value["items"] = value["items"][:1]
        value["items"][0]["line_kind"] = "shipping"
        value["presentation"] = {
            "page_format": "LETTER",
            "accent_color": "#6B1D1D",
            "labels": {"notes": "MEMO"},
            "columns": ["description", "quantity", "hsn_sac", "unit_rate", "gst_rate", "tax", "total"],
        }
        value["signature_image"] = image_asset()
        value["attachments"] = [image_asset("supporting-page.png")]
        data = render_native_invoice_pdf(value)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreaterEqual(data.count(b"/Type /Page"), 2)
        self.assertIn(b"612", data)  # Letter width in PDF points.

        value.update({"lifecycle": "draft", "invoice_number": None})
        with self.assertRaisesRegex(InvoicePdfError, "watermarked preview"):
            render_native_invoice_pdf(value)
        self.assertTrue(render_native_invoice_pdf(value, final=False).startswith(b"%PDF-"))

    @unittest.skipUnless(importlib.util.find_spec("qrcode"), "qrcode dependency not installed")
    def test_branded_landscape_logo_references_and_payment_qr(self):
        value = invoice()
        value["items"] = value["items"][:1]
        value["presentation"] = {
            "page_format": "A4",
            "template_id": "branded",
            "orientation": "landscape",
            "logo_size": "large",
            "accent_color": "#235B74",
            "labels": {},
            "columns": ["description", "hsn_sac", "quantity", "unit_rate", "gst_rate", "tax", "total"],
        }
        value["logo_image"] = image_asset("seller-logo.png")
        value["payment_profile"] = {
            "account_name": "Test Seller", "bank_name": "Test Bank", "account_number": "123456789",
            "ifsc": "ABCD0123456", "upi_id": "seller@example", "qr_enabled": True,
        }
        value["document_references"] = [{
            "kind": "purchase_order", "reference": "PO-2026-17", "date": "2026-09-16", "note": "Customer order",
        }]
        branded = render_native_invoice_pdf(value)
        self.assertTrue(branded.startswith(b"%PDF-"))
        self.assertIn(b"841", branded)  # A4 landscape width in PDF points.
        self.assertEqual(branded, render_native_invoice_pdf(value))

    def test_receipt_is_deterministic_and_rejects_drafts(self):
        value = invoice()
        value["items"] = value["items"][:1]
        value.update({
            "updated_at": "2026-09-18T11:00:00+00:00",
            "amount_paid_paise": 20_000,
            "amount_refunded_paise": 1_000,
            "credit_total_paise": 3_600,
            "debit_total_paise": 0,
            "payments": [{"paid_on": "2026-09-18", "amount_paise": 20_000, "method": "upi", "reference": "PAY-1"}],
            "refunds": [{"refunded_on": "2026-09-18", "amount_paise": 1_000, "method": "bank_transfer", "reference": "REF-1"}],
        })
        receipt = render_native_receipt_pdf(value)
        self.assertTrue(receipt.startswith(b"%PDF-"))
        self.assertEqual(receipt, render_native_receipt_pdf(value))
        value.update({"lifecycle": "draft", "invoice_number": None})
        with self.assertRaisesRegex(InvoicePdfError, "receipt requires"):
            render_native_receipt_pdf(value)

    def test_credit_note_uses_adjustment_document_contract(self):
        value = invoice()
        value.update({
            "document_kind": "credit_note",
            "note_number": "CN/26-27/000001",
            "note_date": "2026-09-18",
            "source_invoice_id": value["invoice_id"],
            "source_invoice_number": value["invoice_number"],
            "source_invoice_date": value["invoice_date"],
            "reason": "Returned goods",
        })
        self.assertTrue(render_native_adjustment_pdf(value).startswith(b"%PDF-"))
        value["document_kind"] = "unsupported"
        with self.assertRaisesRegex(InvoicePdfError, "document kind"):
            render_native_adjustment_pdf(value)

    def test_einvoice_exemption_declaration_is_renderable(self):
        value = invoice()
        value["einvoice_exemption_declaration"] = "Rule 48 exemption declaration"
        self.assertTrue(render_native_invoice_pdf(value).startswith(b"%PDF-"))

    @unittest.skipUnless(importlib.util.find_spec("qrcode"), "qrcode dependency not installed")
    def test_signed_qr_payload_is_rendered_and_bounded(self):
        value = invoice()
        value["signed_qr_payload"] = "signed-irp-payload"
        self.assertTrue(render_native_invoice_pdf(value).startswith(b"%PDF-"))
        value["signed_qr_payload"] = "x" * 4097
        with self.assertRaisesRegex(InvoicePdfError, "4096"):
            render_native_invoice_pdf(value)

    def test_font_asset_contract(self):
        readme = Path(FONT_DIR / "README.md").read_text()
        self.assertIn("NotoSans-Regular.ttf", readme)
        self.assertIn("NotoSansDevanagari-Regular.ttf", readme)


if __name__ == "__main__":
    unittest.main()
