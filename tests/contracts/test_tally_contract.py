import ast
import hashlib
import pathlib
import re
import shlex
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from html import escape


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


source = pathlib.Path("services/tally_service.py").read_text()
tree = ast.parse(source)
wanted = {
    "connector_key_hash", "stable_remote_id", "normalized_settings", "_money", "_number", "_xml", "_ledger",
    "_customer_snapshot", "_tally_escalation_payload", "build_sales_voucher_xml", "parse_invoice_command",
    "_node_text", "_decimal_text", "_tag", "_direct_text", "_parse_tally_xml", "_tally_date", "parse_snapshot_responses", "validate_snapshot_responses", "build_master_xml",
    "valid_gstin_format", "parse_outstanding_response", "parse_profit_loss_response", "build_report_xml",
    "_export_collection_xml",
    "_exact_product_master_id", "_job_phase",
    "_validate_tally_pdf",
}
nodes = [
    node for node in tree.body
    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted)
    or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in {"DEFAULT_SETTINGS", "GSTIN_PATTERN", "INVALID_XML10_CHARACTERS", "INVALID_XML10_REFERENCES", "SNAPSHOT_KEYS"} for target in node.targets))
]
namespace = {
    "hashlib": hashlib, "date": date, "datetime": datetime, "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP,
    "escape": escape, "HTTPException": HTTPException, "re": re, "shlex": shlex, "ET": ET,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "services/tally_service.py", "exec"), namespace)
build_sales_voucher_xml = namespace["build_sales_voucher_xml"]
connector_key_hash = namespace["connector_key_hash"]
normalized_settings = namespace["normalized_settings"]
customer_snapshot = namespace["_customer_snapshot"]
tally_escalation_payload = namespace["_tally_escalation_payload"]
parse_invoice_command = namespace["parse_invoice_command"]
parse_snapshot_responses = namespace["parse_snapshot_responses"]
validate_snapshot_responses = namespace["validate_snapshot_responses"]
build_master_xml = namespace["build_master_xml"]
valid_gstin_format = namespace["valid_gstin_format"]
parse_outstanding_response = namespace["parse_outstanding_response"]
parse_profit_loss_response = namespace["parse_profit_loss_response"]
build_report_xml = namespace["build_report_xml"]
exact_product_master_id = namespace["_exact_product_master_id"]
job_phase = namespace["_job_phase"]
export_collection_xml = namespace["_export_collection_xml"]
validate_tally_pdf = namespace["_validate_tally_pdf"]


class TallyContractTests(unittest.TestCase):
    def setUp(self):
        self.quote = {
            "org_id": "org_test",
            "quote_id": "quote_test",
            "revision_number": 2,
            "quote_number": "Q-100",
            "issued_on": "2026-08-18",
            "private_subtotal": 100,
            "private_gst_total": 18,
            "shipping": 10,
            "discount": 5,
            "public_grand_total": 123,
            "customer": {
                "name": "Buyer & Sons", "phone": "+919999999999", "email": "buyer@example.com",
                "gstin": "27ABCDE1234F1Z5", "address": "Pune",
            },
            "items": [{
                "description": "RTX <5060> & build",
                "quantity": 1,
                "private_line_subtotal": 100,
                "public_line_total": 118,
            }],
        }
        self.settings = {
            "company_name": "Heidel & Co",
            "voucher_type": "Sales",
            "sales_ledger": "Sales",
            "stock_unit": "Nos",
            "cgst_ledger": "CGST",
            "sgst_ledger": "SGST",
            "shipping_ledger": "Shipping",
            "discount_ledger": "Discount",
        }

    def test_xml_is_escaped_balanced_and_stable(self):
        xml = build_sales_voucher_xml(self.quote, self.settings, "Buyer & Sons", "cgst_sgst")
        self.assertIn("Heidel &amp; Co", xml)
        self.assertIn("RTX &lt;5060&gt; &amp; build", xml)
        self.assertIn("<AMOUNT>-123.00</AMOUNT>", xml)
        self.assertIn("<AMOUNT>9.00</AMOUNT>", xml)
        self.assertIn("<BILLALLOCATIONS.LIST><NAME>Q-100</NAME><BILLTYPE>New Ref</BILLTYPE>", xml)
        self.assertEqual(xml.count("<BILLALLOCATIONS.LIST>"), 1)
        self.assertEqual(xml.count("REMOTEID="), 1)
        self.assertNotIn("<VOUCHERNUMBER>", xml)
        self.assertIn("<REFERENCE>Q-100</REFERENCE>", xml)
        self.assertNotIn("Buyer & Sons", xml)

    def test_mismatched_money_fails_before_connector(self):
        self.quote["public_grand_total"] = 124
        with self.assertRaises(HTTPException):
            build_sales_voucher_xml(self.quote, self.settings, "Buyer", "cgst_sgst")

    def test_connector_secret_is_stored_as_hash(self):
        raw = "connector-secret"
        self.assertNotEqual(connector_key_hash(raw), raw)
        self.assertEqual(connector_key_hash(raw), connector_key_hash(raw))
        self.assertNotIn("connector_key_hash", normalized_settings({"connector_key_hash": "never-return-this"}))

    def test_routes_keep_connector_separate_from_user_auth(self):
        source = pathlib.Path("api/tally_routes.py").read_text()
        service = pathlib.Path("services/tally_service.py").read_text()
        self.assertIn('alias="X-Tally-Connector-Key"', source)
        self.assertIn('alias="X-Tally-Claim-Id"', source)
        self.assertIn('await admin_required(user)', source)
        self.assertIn('/api/tally/operations', source)
        self.assertIn('connector_version_number < 6', source)
        self.assertNotIn('"connector_key_hash": integration', source)
        self.assertIn('"response_meta": response_meta', service)
        self.assertNotIn('"responses": result.get("responses")', service)

    def test_failure_escalation_connects_customer_and_tally_context(self):
        customer = customer_snapshot(self.quote)
        payload = tally_escalation_payload({
            "job_id": "job-1", "conversation_id": "whatsapp_919999999999",
            "quote_id": "quote_test", "quote_number": "Q-100", "party_ledger": "Buyer Ledger",
            "amount": 123, "customer": customer,
        }, "failed", "Ledger does not exist")
        self.assertEqual(payload["id"], "tally:job-1")
        self.assertEqual(payload["metadata"]["conversation_id"], "whatsapp_919999999999")
        self.assertEqual(payload["metadata"]["customer"]["gstin"], "27ABCDE1234F1Z5")
        self.assertEqual(payload["metadata"]["party_ledger"], "Buyer Ledger")
        self.assertIn("Quotation Q-100", payload["customer_text"])
        self.assertIn("Ledger does not exist", payload["customer_text"])

    def test_invoice_command_and_snapshot_are_normalized_for_review(self):
        self.assertEqual(parse_invoice_command('/invoice "RTX 5060" qty=2'), {
            "items": [{"model": "RTX 5060", "quantity": "2"}],
        })
        self.assertEqual(parse_invoice_command(
            "/invoice AMD RYZEN 9 9950X3D | qty=2 | price=45,000; RTX 5090 | quantity=1 | rate=180000"
        ), {
            "items": [
                {"model": "AMD RYZEN 9 9950X3D", "quantity": "2", "rate": 45000.0},
                {"model": "RTX 5090", "quantity": "1", "rate": 180000.0},
            ],
        })
        self.assertEqual(parse_invoice_command(
            "/invoice 995x3d 2 45000+ rtx 5090 msi 1 256500+"
        ), {
            "items": [
                {"model": "995x3d", "quantity": "2", "rate": 45000.0, "gst_inclusive": False},
                {"model": "rtx 5090 msi", "quantity": "1", "rate": 256500.0, "gst_inclusive": False},
            ],
        })
        laptop = (
            'ASUS Vivobook Go 15 E1504FA-BQ2111WS\tAMD Ryzen 3 30/ 8GB DDR5/ 512GB SSD/ '
            '15.6" FHD ANTI-GLARE/ Cool Silver/ Win 11 Home/ OFFICE HOME 24(IN)+M365 BASIC/ Backpack'
        )
        self.assertEqual(parse_invoice_command(f'/invoice {laptop} 1 55000'), {
            "items": [{"model": laptop.replace("\t", " "), "quantity": "1", "rate": 55000.0, "gst_inclusive": True}],
        })
        with self.assertRaises(HTTPException):
            parse_invoice_command("/invoice RTX 5090 | qty=0 | price=180000")
        parsed = parse_snapshot_responses([
            {"key": "stock_items", "xml": (
                "<ENVELOPE><STOCKITEM><NAME>Gaming PC</NAME><ALIAS>RTX-5060</ALIAS>"
                "<MASTERID>42</MASTERID><BASEUNITS>Nos</BASEUNITS><CLOSINGBALANCE>5 Nos</CLOSINGBALANCE>"
                "<STANDARDPRICE>75,000.00/Nos</STANDARDPRICE><GSTIGSTRATE>18</GSTIGSTRATE>"
                "</STOCKITEM></ENVELOPE>"
            )},
            {"key": "ledgers", "xml": '<ENVELOPE><LEDGER NAME="Lee Computers"><PARENT>Sundry Debtors</PARENT><MASTERID>7</MASTERID><ISBILLWISEON>Yes</ISBILLWISEON></LEDGER></ENVELOPE>'},
        ])
        self.assertEqual(parsed["stock_items"][0]["model_number"], "RTX-5060")
        self.assertEqual(parsed["stock_items"][0]["closing_quantity"], 5.0)
        self.assertEqual(parsed["stock_items"][0]["standard_rate"], 75000.0)
        self.assertTrue(parsed["ledgers"][0]["billwise"])
        self.assertEqual(parsed["ledgers"][0]["name"], "Lee Computers")

    def test_xml10_control_markers_do_not_break_ledger_sync(self):
        parsed = parse_snapshot_responses([{
            "key": "ledgers",
            "xml": (
                "<ENVELOPE><LEDGER><NAME>Cash</NAME><PARENT>&#4; Primary</PARENT></LEDGER>"
                "<LEDGER><NAME>Profit &amp; Loss A/c</NAME><PARENT>\x04Primary</PARENT></LEDGER></ENVELOPE>"
            ),
        }])
        self.assertEqual(parsed["ledgers"][0]["parent"], "Primary")
        self.assertEqual(parsed["ledgers"][1]["parent"], "Primary")

    def test_incomplete_snapshot_cannot_replace_the_last_good_cache(self):
        parsed = parse_snapshot_responses([
            {"key": "ledgers", "xml": "<ENVELOPE><LEDGER><NAME>Cash</NAME></LEDGER></ENVELOPE>"},
        ])
        with self.assertRaises(HTTPException):
            validate_snapshot_responses([{"key": "ledgers"}], parsed)
        empty = {"ledgers": []}
        responses = [{"key": key} for key in namespace["SNAPSHOT_KEYS"]]
        with self.assertRaises(HTTPException):
            validate_snapshot_responses(responses, empty)

    def test_collection_export_defines_the_collection_tally_is_asked_to_export(self):
        xml = export_collection_xml(self.settings, "Ledger", ["Name", "Parent"])
        self.assertIn("<ID>HeidelAILedger</ID>", xml)
        self.assertIn('<COLLECTION NAME="HeidelAILedger" ISINITIALIZE="Yes">', xml)
        self.assertIn("<TYPE>Ledger</TYPE>", xml)
        self.assertIn("<NATIVEMETHOD>Name</NATIVEMETHOD>", xml)
        self.assertIn("<NATIVEMETHOD>Parent</NATIVEMETHOD>", xml)

    def test_master_operation_waits_for_linked_readback(self):
        master = {"status": "completed", "job_type": "master_create", "readback_job_id": "sync-1", "master": {"name": "Lee Computers"}}
        self.assertEqual(job_phase(master, {"status": "pending"})[0], "reading_from_tally")
        self.assertEqual(job_phase(master, {"status": "completed"}, master_found=True)[0], "completed")
        phase, error = job_phase(master, {"status": "completed"}, master_found=False)
        self.assertEqual(phase, "failed")
        self.assertIn("read-back did not contain it", error)

    def test_master_xml_only_creates_or_alters_supported_masters(self):
        xml = build_master_xml("sales", {"name": "Computer Sales"})
        self.assertIn('ACTION="Create"', xml)
        self.assertIn("<PARENT>Sales Accounts</PARENT>", xml)
        altered = build_master_xml("stock_item", {
            "name": "Gaming PC", "model_number": "RTX-5060", "unit": "Nos", "standard_rate": 75000,
        }, action="Alter", current_name="Gaming PC")
        self.assertIn('ACTION="Alter"', altered)
        self.assertNotIn('ACTION="Delete"', altered)

    def test_gstin_reports_and_outstandings_are_explicit(self):
        self.assertTrue(valid_gstin_format("27abcde1234f1z5"))
        self.assertFalse(valid_gstin_format("27ABCDE1234F1Z"))
        outstanding = parse_outstanding_response(
            "<ENVELOPE><BILLFIXED><BILLDATE>20260801</BILLDATE><BILLREF>INV-7</BILLREF>"
            "<BILLPARTY>Lee Computers</BILLPARTY><BILLDUE>20260815</BILLDUE></BILLFIXED>"
            "<BILLCL>1,250.00</BILLCL></ENVELOPE>"
        )
        self.assertEqual(outstanding[0]["outstanding_amount"], 1250.0)
        self.assertEqual(outstanding[0]["due_date"], "2026-08-15")
        report = parse_profit_loss_response(
            "<ENVELOPE><DSPACCNAME><DSPDISPNAME>Sales Accounts</DSPDISPNAME></DSPACCNAME>"
            "<DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT>"
            "<DSPCLCRAMT><DSPCLCRAMTA>100000</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>"
            "<DSPACCNAME><DSPDISPNAME>Gross Profit</DSPDISPNAME></DSPACCNAME>"
            "<DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT>"
            "<DSPCLCRAMT><DSPCLCRAMTA>25000</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO>"
            "<DSPACCNAME><DSPDISPNAME>Net Profit</DSPDISPNAME></DSPACCNAME>"
            "<DSPACCINFO><DSPCLDRAMT><DSPCLDRAMTA></DSPCLDRAMTA></DSPCLDRAMT>"
            "<DSPCLCRAMT><DSPCLCRAMTA>18000</DSPCLCRAMTA></DSPCLCRAMT></DSPACCINFO></ENVELOPE>"
        )
        self.assertEqual(report["sales"], 100000.0)
        self.assertEqual(report["gross_profit"], 25000.0)
        self.assertEqual(report["net_profit"], 18000.0)
        report_xml = build_report_xml(self.settings, "Profit and Loss", "2026-04-01", "2026-08-20")
        self.assertIn("<SVFROMDATE>20260401</SVFROMDATE>", report_xml)

    def test_only_one_exact_tally_product_is_preselected(self):
        rows = [
            {"master_id": "1", "name": "Laptop RTX 5060", "model_number": "RTX5060", "aliases": []},
            {"master_id": "2", "name": "RTX 5060 Case", "model_number": "CASE5060", "aliases": []},
        ]
        self.assertEqual(exact_product_master_id(rows, "RTX5060"), "1")
        rows.append({"master_id": "3", "name": "RTX5060", "model_number": "OTHER", "aliases": []})
        self.assertEqual(exact_product_master_id(rows, "RTX5060"), "")

    def test_new_routes_cover_reports_pdf_links_and_reminders(self):
        routes = pathlib.Path("api/tally_routes.py").read_text()
        self.assertIn('"/api/tally/profit-loss"', routes)
        self.assertIn('"/api/tally/invoices/{invoice_id}/pdf"', routes)
        self.assertIn("store_uploaded_tally_pdf", routes)
        self.assertIn('"/api/tally/customers/{party_master_id}/link"', routes)
        self.assertIn('"/api/tally/invoices/{invoice_id}/reminders"', routes)
        self.assertIn('"/api/tally-connector/jobs/{job_id}/artifact"', routes)

    def test_connector_result_ignores_v5_null_response_placeholder(self):
        routes = pathlib.Path("api/tally_routes.py").read_text()
        self.assertIn('field_validator("responses", mode="before")', routes)
        self.assertIn("if item", routes)

    def test_uploaded_tally_pdf_must_be_a_pdf_under_ten_mb(self):
        validate_tally_pdf(b"%PDF-1.7\n")
        with self.assertRaises(HTTPException):
            validate_tally_pdf(b"<ENVELOPE></ENVELOPE>")


if __name__ == "__main__":
    unittest.main()
