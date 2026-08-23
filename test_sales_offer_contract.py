import ast
import unittest
from pathlib import Path

from services.sales_offer_policy import (
    explicit_total,
    extract_quoted_prices,
    is_outbound_offer,
    next_delivery_status,
    offer_prices,
    quote_revision_snapshot,
    report_query,
    structured_offer_snapshot,
    tax_basis,
)


class SalesOfferContractTest(unittest.TestCase):
    def test_structured_chat_offer_totals_and_validation(self):
        snapshot = structured_offer_snapshot({"offer": {"items": [
            {"description": "Ryzen 7 9700X", "quantity": 1, "unit_price": 32_000, "gst_rate": 18, "gst_inclusive": True},
            {"description": "RTX 5060", "quantity": 1, "unit_price": 45_000, "gst_rate": 18, "gst_inclusive": False},
        ]}})
        self.assertEqual(snapshot["amounts"], [32_000, 53_100])
        self.assertEqual(snapshot["total_amount"], 85_100)
        self.assertEqual(snapshot["tax_basis"], "mixed")
        with self.assertRaises(ValueError):
            structured_offer_snapshot({"offer": {"items": []}})
        with self.assertRaises(ValueError):
            structured_offer_snapshot({"offer": {"items": [{"description": "RTX 5060", "unit_price": "NaN"}]}})

    def test_scope_money_and_delivery_rules(self):
        member = report_query(
            org_id="org-1", user_id="agent-1", role="org:member", start=None, end=None,
            source="chat_price", status="open", agent_id="agent-2",
        )
        self.assertEqual(member["agent_id"], "agent-1")
        admin = report_query(
            org_id="org-1", user_id="admin-1", role="org:admin", start=None, end=None,
            source=None, status=None, agent_id="agent-2",
        )
        self.assertEqual(admin["agent_id"], "agent-2")

        self.assertEqual(explicit_total("GPU ₹45,000 and CPU ₹20,000. Total ₹65,000", [45_000, 20_000, 65_000]), 65_000)
        self.assertIsNone(explicit_total("GPU ₹45,000 and CPU ₹20,000", [45_000, 20_000]))
        self.assertEqual(tax_basis("Final price ₹45,000 + 18% GST"), "extra")
        self.assertEqual(tax_basis("₹45,000 GST included"), "included")
        self.assertEqual(tax_basis("₹45,000 GST extra"), "extra")
        self.assertEqual(tax_basis("₹45,000 not GST included"), "extra")
        self.assertEqual(tax_basis("₹45,000 not inclusive of GST"), "extra")
        self.assertEqual(tax_basis("₹45,000 GST not inclusive"), "extra")
        self.assertEqual(tax_basis("₹45,000 GST is not inclusive"), "extra")

        self.assertFalse(is_outbound_offer("Your budget was ₹50,000, correct?", [50_000]))
        self.assertFalse(is_outbound_offer("You paid ₹50,000", [50_000]))
        self.assertFalse(is_outbound_offer("Can you do 45000?", [45_000]))
        self.assertTrue(is_outbound_offer("Our final price is ₹45,000", [45_000]))
        self.assertTrue(is_outbound_offer("45000", [45_000]))
        self.assertEqual(extract_quoted_prices("Our price is ₹45,000"), [45_000])
        self.assertEqual(extract_quoted_prices("Our price is 45,000"), [45_000])
        self.assertEqual(offer_prices("RTX 5060 45000", []), [45_000])
        self.assertEqual(offer_prices("MSI B850 25000", []), [25_000])
        self.assertEqual(offer_prices("Ryzen 5500", []), [])
        self.assertEqual(offer_prices("Ship to Pune 411001", []), [])
        self.assertEqual(offer_prices("Invoice 123456", []), [])
        for text in ("We are located at 411001", "Our PIN is 411001", "Call us at 9876543"):
            prices = offer_prices(text, extract_quoted_prices(text))
            self.assertFalse(is_outbound_offer(text, prices), text)
        for text in ("Discount ₹500", "₹500 discount", "Advance ₹5,000", "Shipping ₹1,500", "The total savings is ₹5,000"):
            prices = offer_prices(text, extract_quoted_prices(text))
            self.assertFalse(is_outbound_offer(text, prices), text)
        mixed = "Discount ₹500; final price ₹45,000"
        mixed_prices = offer_prices(mixed, extract_quoted_prices(mixed))
        self.assertEqual(mixed_prices, [45_000])
        self.assertEqual(explicit_total(mixed, mixed_prices), 45_000)
        for text in (
            "Discount ₹500 and final price ₹45,000",
            "Advance ₹5,000 and our final price is ₹45,000",
            "Shipping ₹1,500 plus final price ₹45,000",
            "Discount ₹500 and grand total ₹45,000",
        ):
            prices = extract_quoted_prices(text)
            self.assertEqual(prices, [45_000], text)
            self.assertEqual(explicit_total(text, prices), 45_000, text)
        self.assertEqual(offer_prices("Call 919876543210", []), [])

        self.assertEqual(next_delivery_status("read", "sent"), "read")
        self.assertEqual(next_delivery_status("sent", "failed"), "failed")
        quote = {
            "revision_number": 2,
            "public_grand_total": 62_000,
            "revisions": [{"revision_number": 1, "public_grand_total": 60_000}],
        }
        self.assertEqual(quote_revision_snapshot(quote, 1)["public_grand_total"], 60_000)
        self.assertEqual(quote_revision_snapshot(quote, 2)["public_grand_total"], 62_000)
        self.assertIsNone(quote_revision_snapshot(quote, 3))

    def test_whatsapp_status_batch_is_fully_processed(self):
        tree = ast.parse(Path("api/webhook_routes.py").read_text())
        status_loop = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id == "statuses"
        )
        self.assertFalse(any(isinstance(node, ast.Return) for node in ast.walk(status_loop)))

    def test_offer_insert_reconciles_a_racing_delivery_receipt(self):
        tree = ast.parse(Path("services/sales_offer_service.py").read_text())
        record = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "record_outbound_offer"
        )
        insert_line = next(
            node.lineno for node in ast.walk(record)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "insert_one"
        )
        reconcile_line = next(
            node.lineno for node in ast.walk(record)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update_offer_delivery"
        )
        self.assertLess(insert_line, reconcile_line)
        existing_branch = next(
            node for node in record.body
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "existing"
        )
        self.assertTrue(any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update_offer_delivery"
            for node in ast.walk(existing_branch)
        ))
        duplicate_handler = next(
            node for node in ast.walk(record)
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "DuplicateKeyError"
        )
        self.assertTrue(any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "update_offer_delivery"
            for node in ast.walk(duplicate_handler)
        ))

    def test_offer_enqueue_rejects_internal_messages(self):
        tree = ast.parse(Path("services/sales_offer_service.py").read_text())
        enqueue = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "enqueue_outbound_offer"
        )
        allowed = next(
            {item.value for item in node.elts if isinstance(item, ast.Constant)}
            for node in ast.walk(enqueue)
            if isinstance(node, ast.Set)
        )
        self.assertEqual(allowed, {"whatsapp", "instagram"})


if __name__ == "__main__":
    unittest.main()
