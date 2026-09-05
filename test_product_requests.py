"""Offline regressions: python3 -m unittest test_product_requests -v."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

from services.product_request_policy import (
    request_key, grounded_request, available_stock, eligibility_reason,
    approved_restock_template, RESTOCK_TEMPLATE, RESTOCK_BODY, RESTOCK_FOOTER,
)


class ProductRequestPolicyTest(unittest.TestCase):
    def test_identity_isolates_org_line_and_variant(self):
        first = request_key("org-a", "wa:customer:line1", "RTX 4060 Ti")
        self.assertEqual(first, request_key("org-a", "wa:customer:line1", " rtx  4060 ti "))
        for org, line, product in [("org-b", "wa:customer:line1", "RTX 4060 Ti"),
                                   ("org-a", "wa:customer:line2", "RTX 4060 Ti"),
                                   ("org-a", "wa:customer:line1", "RTX 4060")]:
            self.assertNotEqual(first, request_key(org, line, product))

    def test_grounding_rejects_invented_agent_private_and_missing_evidence(self):
        message = {"message_id": "m1", "role": "customer", "content": "Do you have the RTX 4060 Ti?"}
        raw = {"source_message_id": "m1", "product_name": "RTX 4060 Ti", "evidence": message["content"], "confidence": "high"}
        self.assertIsNotNone(grounded_request(raw, [message]))
        self.assertIsNone(grounded_request({**raw, "evidence": "Please notify me"}, [message]))
        self.assertIsNone(grounded_request(raw, [{**message, "role": "agent"}]))
        self.assertIsNone(grounded_request(raw, [{**message, "mode": "private"}]))
        self.assertIsNone(grounded_request({**raw, "source_message_id": "invented"}, [message]))

    def test_stock_zero_unknown_nan_deleted_are_not_available(self):
        for item in [None, {}, {"stock": 0, "quantity": 10}, {"stock": -1}, {"stock": "nan"},
                     {"stock": "unknown"}, {"stock": 1, "is_deleted": True}]:
            self.assertFalse(available_stock(item))
        self.assertTrue(available_stock({"stock": "2"}))

    def test_active_recent_matched_consented_valid_line_required(self):
        stamp = datetime.now(timezone.utc)
        row = {"status": "requested", "product_id": "p1", "reviewed_by": "agent", "consent_note": "Customer asked today",
               "consent_by": "agent", "last_asked_at": stamp, "customer_phone": "919123456789", "business_phone_id": "line"}
        product = {"stock": 2, "updated_at": stamp}
        self.assertEqual(eligibility_reason(row, product), "")
        for change in [{"status": "fulfilled"}, {"status": "dismissed"}, {"product_id": None},
                       {"consent_note": ""}, {"business_phone_id": None}, {"customer_phone": "bad"},
                       {"notification_status": "unknown"}, {"last_asked_at": stamp - timedelta(days=91)}]:
            self.assertTrue(eligibility_reason({**row, **change}, product), change)

    def test_only_exact_approved_marketing_template_matches(self):
        template = {"name": RESTOCK_TEMPLATE, "language": "en_US", "status": "APPROVED", "category": "MARKETING",
                    "components": [{"type": "BODY", "text": RESTOCK_BODY}, {"type": "FOOTER", "text": RESTOCK_FOOTER}]}
        self.assertTrue(approved_restock_template([template]))
        for change in [{"status": "PENDING"}, {"category": "UTILITY"}, {"language": "en_GB"}, {"components": []}]:
            self.assertFalse(approved_restock_template([{**template, **change}]))
        self.assertFalse(RESTOCK_BODY.startswith("{{"))
        self.assertFalse(RESTOCK_BODY.endswith("}}"))

    def test_worker_is_org_scoped_and_accepts_single_message(self):
        source = Path("services/intent_queue_worker.py").read_text()
        module = ast.parse(source)
        enqueue = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "enqueue_intent_check")
        self.assertIn('"org_id": org_id', ast.get_source_segment(source, enqueue))
        self.assertIn("if not docs:", source)
        self.assertNotIn("if len(docs) < 3:", source)
        self.assertIn('raise RuntimeError("Intent extraction returned no result', source)


class TemplateLookupTest(unittest.IsolatedAsyncioTestCase):
    async def test_named_lookup_does_not_depend_on_first_unfiltered_page(self):
        tree = ast.parse(Path("services/wa_service.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_templates")
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"data": [{"name": RESTOCK_TEMPLATE}]})
        scope = {"Optional": Optional, "httpx": SimpleNamespace(AsyncClient=lambda **kw: client, Timeout=lambda n: n),
                 "logger": SimpleNamespace(info=lambda *a: None)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "template_lookup", "exec"), scope)
        result = await scope["get_templates"]("org", "waba", "dummy", name=RESTOCK_TEMPLATE)
        self.assertEqual(result["templates"][0]["name"], RESTOCK_TEMPLATE)
        self.assertEqual(client.get.call_args.kwargs["params"]["name"], RESTOCK_TEMPLATE)
        self.assertNotIn("dummy", client.get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
