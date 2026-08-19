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
wanted = {"connector_key_hash", "stable_remote_id", "normalized_settings", "_money", "_number", "_xml", "_ledger", "build_sales_voucher_xml"}
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


if __name__ == "__main__":
    unittest.main()
