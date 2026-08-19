import ast
import hashlib
import pathlib
import unittest
from datetime import date
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
    "_customer_snapshot", "_tally_escalation_payload", "build_sales_voucher_xml",
}
nodes = [
    node for node in tree.body
    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted)
    or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "DEFAULT_SETTINGS" for target in node.targets))
]
namespace = {
    "hashlib": hashlib, "date": date, "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP,
    "escape": escape, "HTTPException": HTTPException,
}
exec(compile(ast.Module(body=nodes, type_ignores=[]), "services/tally_service.py", "exec"), namespace)
build_sales_voucher_xml = namespace["build_sales_voucher_xml"]
connector_key_hash = namespace["connector_key_hash"]
normalized_settings = namespace["normalized_settings"]
customer_snapshot = namespace["_customer_snapshot"]
tally_escalation_payload = namespace["_tally_escalation_payload"]


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
        self.assertIn('alias="X-Tally-Connector-Key"', source)
        self.assertIn('alias="X-Tally-Claim-Id"', source)
        self.assertIn('await admin_required(user)', source)
        self.assertNotIn('"connector_key_hash": integration', source)

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


if __name__ == "__main__":
    unittest.main()
