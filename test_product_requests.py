"""Offline regressions: python3 -m unittest test_product_requests -v."""
import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from test_restock_dispatch import service

from services.product_request_policy import (
    request_key, grounded_request, available_stock, eligibility_reason, needs_verification,
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
        raw = {"source_message_id": "m1", "product_name": "RTX 4060 Ti", "evidence": message["content"], "confidence": "high",
               "specificity": "specific_product", "product_reference_message_id": "m1", "product_mention": "RTX 4060 Ti"}
        self.assertIsNotNone(grounded_request(raw, [message]))
        self.assertIsNone(grounded_request({**raw, "evidence": "Please notify me"}, [message]))
        self.assertIsNone(grounded_request(raw, [{**message, "role": "agent"}]))
        self.assertIsNone(grounded_request(raw, [{**message, "mode": "private"}]))
        self.assertIsNone(grounded_request({**raw, "source_message_id": "invented"}, [message]))

    def test_product_phrases_categories_and_variants_remain_supported(self):
        for product, content in [
            ("RTX 4060 Ti", "Do you have the RTX 4060 Ti?"),
            ("laptop", "I need a laptop"),
            ("Acme Unknown Model", "Acme Unknown Model ka price kya hai?"),
            ("लैपटॉप", "मुझे लैपटॉप चाहिए"),
            ("मरम्मत सेवा", "क्या मरम्मत सेवा उपलब्ध है?"),
            ("RTX-4060 Ti 16GB White", "RTX-4060 Ti 16GB White stock aaye to WhatsApp karna"),
            ("Nothing Phone (2)", "Nothing Phone (2) available hai?"),
            ("M.2 SSD", "Price for M.2 SSD?"),
            ("Hi-Fi speaker", "Do you have a Hi-Fi speaker?"),
            ("Lenovo ThinkPad X1 Carbon Gen 12 Intel Core Ultra 7 32GB RAM 1TB SSD Black",
             "Do you have Lenovo ThinkPad X1 Carbon Gen 12 Intel Core Ultra 7 32GB RAM 1TB SSD Black?"),
        ]:
            with self.subTest(product=product):
                message = {"message_id": "m1", "role": "customer", "content": content}
                raw = {"product_name": product, "source_message_id": "m1", "evidence": content, "confidence": "low"}
                self.assertEqual(grounded_request(raw, [message])["product_name"], product)

    def test_sentences_and_generic_replies_require_semantic_verification(self):
        for content in [
            "Do you have the RTX 4060 Ti?", "Do you have the RTX 4060 Ti", "Hello, how are you?",
            "Hello there", "What is the price", "I need a laptop", "Send me details",
            "I would like a laptop", "I'd like a laptop", "Need a laptop", "RTX 4060 Ti hai kya",
            "RTX 4060 Ti available hai", "RTX 4060 Ti available.", "RTX 4060 Ti stock aaye to WhatsApp karna",
            "RTX 4060 Ti chahiye", "mujhe laptop chahiye", "क्या लैपटॉप है", "मुझे लैपटॉप चाहिए",
            "it", "that one", "yes", "yes.", "ok", "ok.", "details", "something", "product",
        ]:
            for confidence in ("low", "medium", "high"):
                with self.subTest(content=content, confidence=confidence):
                    message = {"message_id": "m1", "role": "customer", "content": content}
                    raw = {"product_name": content, "source_message_id": "m1", "evidence": content, "confidence": confidence}
                    self.assertTrue(needs_verification(grounded_request(raw, [message])))

    def test_product_name_must_be_in_evidence_without_invented_variants_or_partial_words(self):
        for product, content, evidence in [
            ("MacBook Pro M3", "Do you have RTX 4060 Ti?", "Do you have RTX 4060 Ti?"),
            ("RTX 4060 Ti 16GB White", "Do you have RTX 4060 Ti?", "Do you have RTX 4060 Ti?"),
            ("RTX 4060 Ti", "RTX 4060 Titanium price?", "RTX 4060 Titanium price?"),
            ("iPhone 16", "iPhone 160 price?", "iPhone 160 price?"),
            ("RTX 4060 Ti", "RTX 4060 Ti. Hello!", "Hello!"),
        ]:
            with self.subTest(product=product, content=content):
                message = {"message_id": "m1", "role": "customer", "content": content}
                raw = {"product_name": product, "source_message_id": "m1", "evidence": evidence, "confidence": "high"}
                self.assertIsNone(grounded_request(raw, [message]))

    def test_name_grounding_normalizes_case_and_whitespace_only(self):
        message = {"message_id": "m1", "role": "customer", "content": "Do you have RTX-4060   Ti?"}
        raw = {"product_name": " rtx-4060  ti ", "source_message_id": "m1", "evidence": message["content"], "confidence": "high"}
        self.assertEqual(grounded_request(raw, [message])["product_name"], "rtx-4060 ti")
        self.assertIsNone(grounded_request({**raw, "product_name": "RTX4060 Ti"}, [message]))

    def test_reference_requires_verified_earlier_customer_product(self):
        stamp = datetime.now(timezone.utc)
        earlier = {"message_id": "m1", "role": "customer", "content": "I need RTX 4060 Ti", "timestamp": stamp}
        recent = {"message_id": "m2", "role": "customer", "content": "What is the price of that one?", "timestamp": stamp + timedelta(seconds=1)}
        raw = {"product_name": "RTX 4060 Ti", "source_message_id": "m2", "product_source_message_id": "m1",
               "evidence": recent["content"], "confidence": "high"}
        self.assertEqual(grounded_request(raw, [recent], [earlier])["product_source_message_id"], "m1")
        self.assertIsNotNone(grounded_request(raw, [earlier, recent]))
        for content in ("Uska price kya hai?", "उसका कितना दाम है?"):
            self.assertIsNotNone(grounded_request({**raw, "evidence": content}, [{**recent, "content": content}], [earlier]))
        for invalid_earlier in [{**earlier, "role": "agent"}, {**earlier, "mode": "private"},
                                {**earlier, "content": "I need RTX 4060"},
                                {**earlier, "timestamp": stamp + timedelta(seconds=2)}]:
            with self.subTest(earlier=invalid_earlier):
                self.assertIsNone(grounded_request(raw, [recent], [invalid_earlier]))
        self.assertIsNone(grounded_request(raw, [recent]))
        self.assertTrue(needs_verification(grounded_request(raw, [recent, earlier])))
        self.assertTrue(needs_verification(grounded_request({**raw, "confidence": "low"}, [recent], [earlier])))
        self.assertIsNone(grounded_request({**raw, "product_source_message_id": "invented"}, [recent], [earlier]))
        self.assertIsNone(grounded_request({**raw, "product_source_message_id": None}, [recent], [earlier]))
        self.assertIsNone(grounded_request({**raw, "source_message_id": "m1", "evidence": earlier["content"]}, [recent], [earlier]))
        for content in ("Hello", "What is the price?", "yes"):
            self.assertTrue(needs_verification(grounded_request({**raw, "evidence": content}, [{**recent, "content": content}], [earlier])))
        self.assertIsNotNone(grounded_request({**raw, "product_name": "Head and Shoulders shampoo"}, [recent],
                                             [{**earlier, "content": "I need Head and Shoulders shampoo"}]))

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


class ProductRequestCaptureTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_product_request_v2 import MemoryCollection, MemoryDB
        self.scope = service()
        self.requests = MemoryCollection()
        self.database = MemoryDB(product_requests=self.requests)
        self.database.collections["conversations_org"] = MemoryCollection([{
            "_id": "chat", "conversation_id": "chat", "platform": "whatsapp", "customer_id": "919123456789", "business_phone_id": "line"}])
        self.scope["get_async_mongo_db"] = lambda: self.database
        self.scope["catalogue_org"] = AsyncMock(return_value="org")
        self.scope["exact_catalogue_product"] = AsyncMock(return_value=None)
        async def verify(candidates, messages):
            return [{**c, "verification_status": "confirmed", "reason": "Fixture verified"} for c in candidates]
        module = SimpleNamespace(verify_product_observations=AsyncMock(side_effect=verify))
        self.module_patch = patch.dict("sys.modules", {"agents.intent_agent.agent": module})
        self.module_patch.start(); self.addCleanup(self.module_patch.stop)
        self.job = {"org_id": "org", "conversation_id": "chat"}
        self.stamp = datetime.now(timezone.utc)

    async def test_rejected_grounding_never_creates_request(self):
        content = "Do you have RTX 4060 Ti?"
        message = {"message_id": "m1", "role": "customer", "content": content, "timestamp": self.stamp}
        for product in ("MacBook Pro M3", "RTX 4060 Ti 16GB White"):
            raw = {"product_name": product, "source_message_id": "m1", "evidence": content, "confidence": "high"}
            await self.scope["capture_product_requests"](self.job, [raw], [message])
        self.assertEqual(self.requests.rows, {})

    async def test_repeat_enquiries_and_withdrawal_target_same_validated_product(self):
        for i, (product, content, withdrawn) in enumerate([
            ("RTX 4060 Ti", "Do you have RTX 4060 Ti?", False),
            ("rtx 4060 ti", "What is the price of RTX 4060 Ti?", False),
            ("RTX 4060 Ti", "I no longer want RTX 4060 Ti", True),
        ]):
            message = {"message_id": f"m{i}", "role": "customer", "content": content, "timestamp": self.stamp + timedelta(seconds=i)}
            raw = {"product_name": product, "source_message_id": message["message_id"], "evidence": content,
                   "confidence": "high", "withdrawn": withdrawn}
            await self.scope["capture_product_requests"](self.job, [raw], [message])
        self.assertEqual(len(self.requests.rows), 1)
        row = list(self.requests.rows.values())[0]
        self.assertEqual(row["status"], "dismissed")
        self.assertEqual(row["evidence"], "I no longer want RTX 4060 Ti")
        self.assertEqual(row["last_asked_at"], self.stamp + timedelta(seconds=1))

    async def test_capture_retains_reference_evidence_and_recent_source(self):
        earlier = {"message_id": "m1", "role": "customer", "content": "RTX 4060 Ti please", "timestamp": self.stamp}
        recent = {"message_id": "m2", "role": "customer", "content": "Notify me when that one is available", "timestamp": self.stamp + timedelta(seconds=1)}
        raw = {"product_name": "RTX 4060 Ti", "source_message_id": "m2", "product_source_message_id": "m1",
               "evidence": recent["content"], "confidence": "high", "notification_requested": True}
        await self.scope["capture_product_requests"](self.job, [raw], [recent], [earlier])
        saved = list(self.requests.rows.values())[0]
        self.assertEqual(saved["evidence"], recent["content"])
        self.assertEqual(saved["product_source_message_id"], "m1")
        self.assertEqual(saved["last_asked_at"], recent["timestamp"])
        self.assertTrue(saved["notification_requested"])

    async def test_worker_passes_older_product_context_without_allowing_old_request_evidence(self):
        earlier = {"message_id": "m1", "role": "customer", "content": "RTX 4060 Ti please", "timestamp": self.stamp}
        recent = {"message_id": "m2", "role": "customer", "content": "What is the price of that one?", "timestamp": self.stamp + timedelta(seconds=1)}
        docs = [earlier, *[{"message_id": f"private-{i}", "role": "agent", "mode": "private", "content": "Private note"} for i in range(9)], recent]
        raw = {"product_name": "RTX 4060 Ti", "source_message_id": "m2", "product_source_message_id": "m1",
               "evidence": recent["content"], "confidence": "high"}
        extraction = AsyncMock(return_value=SimpleNamespace(
            product_requests=[SimpleNamespace(model_dump=lambda: raw)], has_intent=True,
            confidence="high", product_name="RTX 4060 Ti", tentative_date="2026-10-11T10:00:00+05:30",
        ))
        messages = Mock()
        messages.find.return_value.sort.return_value.limit.return_value = list(reversed(docs))
        queue = SimpleNamespace(update_one=Mock())
        node = next(n for n in ast.parse(Path("services/intent_queue_worker.py").read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_single_job")
        scope = {"asyncio": asyncio, "datetime": datetime, "timezone": timezone, "ZoneInfo": ZoneInfo,
                 "db": {"messages_org": messages, "purchase_intent_queue": queue}, "logger": Mock(),
                 "MESSAGE_CONTEXT_LIMIT": 30, "COLLECTION": "purchase_intent_queue",
                 "extract_purchase_intent": extraction, "save_intent_to_sheet": AsyncMock(),
                 "schedule_intent_followup": AsyncMock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "intent_worker", "exec"), scope)
        with patch.dict("sys.modules", {"services.product_request_service": SimpleNamespace(capture_product_requests=self.scope["capture_product_requests"])}):
            await scope["_process_single_job"]({**self.job, "job_id": "job"}, asyncio.Semaphore(1))
            saved = list(self.requests.rows.values())[0]
            self.assertEqual(saved["product_source_message_id"], "m1")
            prompt = extraction.await_args.args[0]
            self.assertIn("[m1] customer: RTX 4060 Ti please", prompt)
            self.assertIn("[m2] customer: What is the price of that one?", prompt)
            self.assertNotIn("Private note", prompt)
            self.assertEqual(queue.update_one.call_args.args[1]["$set"]["status"], "completed")
            before = len(self.requests.rows)
            raw.update(source_message_id="m1", evidence=earlier["content"])
            await scope["_process_single_job"]({**self.job, "job_id": "job"}, asyncio.Semaphore(1))
            self.assertEqual(len(self.requests.rows), before)


if __name__ == "__main__":
    unittest.main()
