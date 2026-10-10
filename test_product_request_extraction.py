"""Offline regression: python3 -m unittest test_product_request_extraction -v."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from services.product_request_policy import request_key, eligibility_reason
from services.product_request_legacy_policy import grounded_request
from scripts.cleanup_product_requests import plan_conversation, make_report, apply_report
from test_restock_dispatch import service

STAMP = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


def message(content="Do you have Zotac RTX 5060?", **changes):
    return {"_id": "m1", "message_id": "m1", "role": "customer", "content": content, "timestamp": STAMP, **changes}


def extraction(name="Zotac RTX 5060", **changes):
    return {"product_name": name, "product_mention": name, "product_reference_message_id": "m1",
            "source_message_id": "m1", "evidence": "Do you have Zotac RTX 5060?",
            "specificity": "specific_product", "confidence": "high", **changes}


def item(name="Zotac RTX 5060", **changes):
    return {**extraction(name), "source_at": STAMP, "notification_requested": False, "withdrawn": False, **changes}


def row(id="old", name="DDR4 setup with GPU", **changes):
    return {"_id": id, "org_id": "org", "conversation_id": "chat", "product_name": name,
            "source_message_id": "m1", "evidence": "Whats the avg fps ill get?", "status": "requested", "last_asked_at": STAMP,
            "customer_phone": "919123456789", "business_phone_id": "12345", **changes}


class ExtractionTests(unittest.TestCase):
    def test_obvious_prose_and_specs_rejected_even_if_model_claims_specificity(self):
        for name in ["Whats the avg fps ill get?", "Whats the avg fps ill get", "DDR4 setup with GPU", "PC build (DDR4 setup with GPU)",
                     "For dd4 setup?", "16GB RAM", "512GB NVMe SSD", "DDR4", "Please send Zotac RTX 5060"]:
            with self.subTest(name=name):
                self.assertIsNone(grounded_request(extraction(name, evidence=name), [message(name)]))

    def test_specificity_provenance_and_confidence_are_required(self):
        raw, msg = extraction(), message()
        self.assertIsNotNone(grounded_request(raw, [msg]))
        for change in [{"specificity": "generic"}, {"specificity": "ambiguous"}, {"confidence": "low"},
                       {"product_reference_message_id": "missing"}, {"product_mention": "MSI RTX 5060"},
                       {"product_name": "Zotac RTX 5060 Ti"}, {"evidence": "invented"}]:
            self.assertIsNone(grounded_request({**raw, **change}, [msg]), change)
        self.assertIsNone(grounded_request(raw, [{**msg, "mode": "private"}]))
        self.assertIsNone(grounded_request(raw, [{**msg, "role": "agent"}]))

    def test_product_names_need_no_digits_and_can_be_long_or_multilingual(self):
        for name in ["Apple AirPods Pro", "Sony Alpha ILCE-7M4 Mirrorless Camera with FE 28-70mm Lens Kit", "सैमसंग गैलेक्सी S24"]:
            content = f"{name} ka price?"
            self.assertIsNotNone(grounded_request(extraction(name, evidence=content), [message(content)]))

    def test_build_components_and_followup_reference_older_context(self):
        older = message("AMD Ryzen 5 5600, Zotac RTX 5060, 16GB RAM, 512GB SSD", role="agent")
        filler = [message("ok", _id=f"f{i}", message_id=f"f{i}") for i in range(11)]
        question = message("Whats the avg fps ill get?", _id="m2", message_id="m2", timestamp=STAMP + timedelta(minutes=1))
        docs = [older, *filler, question]
        for name in ["AMD Ryzen 5 5600", "Zotac RTX 5060"]:
            raw = extraction(name, source_message_id="m2", evidence=question["content"])
            self.assertIsNotNone(grounded_request(raw, docs))
            self.assertIsNone(grounded_request(raw, [question, older]))
            self.assertIsNone(grounded_request(raw, [{**older, "mode": "private"}, *filler, question]))
        self.assertIsNone(grounded_request(extraction(evidence=older["content"]), [older, *filler, question]))

    def test_prompt_schema_and_worker_preserve_independent_purchase_intent(self):
        tree = ast.parse(Path("agents/intent_agent/agent.py").read_text())
        prompt = "\n".join(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str))
        self.assertIn("Unknown catalogue products and categories are valid", prompt)
        self.assertIn("Split explicitly named components", prompt)
        self.assertIn("product_source_message_id", prompt)
        worker = Path("services/intent_queue_worker.py").read_text()
        self.assertIn('getattr(result, "product_requests", [])], targets, context)', worker)
        self.assertIn('elif result.has_intent and result.confidence != "low"', worker)


class CleanupTests(unittest.TestCase):
    def test_missing_or_partial_extraction_preserves_specific_product_identity(self):
        cpu = row(name="AMD Ryzen 5 5600", evidence="I want AMD Ryzen 5 5600 and Zotac RTX 5060")
        for values in ([], [item()]):
            original = deepcopy(cpu)
            plan = plan_conversation([cpu], {"old": values}, "report", STAMP)
            self.assertIn("review", plan)
            self.assertNotIn("after", plan)
            self.assertEqual(cpu, original)
        unfamiliar = row(name="Unfamiliar Acme Edition")
        self.assertIn("review", plan_conversation([unfamiliar], {"old": []}, "report", STAMP))

    def test_build_splits_into_products_and_preserves_original(self):
        original = row(status="fulfilled")
        saved = deepcopy(original)
        plan = plan_conversation([original], {"old": [item("AMD Ryzen 5 5600"), item()]}, "report", STAMP)
        live = [r for r in plan["after"] if not r.get("archived_at")]
        self.assertEqual({r["product_name"] for r in live}, {"AMD Ryzen 5 5600", "Zotac RTX 5060"})
        self.assertTrue(all(r["status"] == "fulfilled" and not r.get("consent_note") and not r.get("product_id") for r in live))
        archived = next(r for r in plan["after"] if r["_id"] == "old")
        self.assertEqual(archived["cleanup_original"], saved)
        self.assertEqual(set(archived["replaced_by"]), {r["_id"] for r in live})
        self.assertEqual(original, saved)

    def test_duplicates_merge_without_rekeying_and_take_latest_evidence(self):
        first = row("a", "Zotac RTX 5060")
        second = row("b", "What is the price of Zotac RTX 5060?")
        newer = item(source_at=STAMP + timedelta(minutes=1), source_message_id="m2", evidence="Any stock?")
        plan = plan_conversation([first, second], {"a": [item()], "b": [newer]}, "report", STAMP)
        live = [r for r in plan["after"] if not r.get("archived_at")]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["_id"], "a")
        self.assertEqual(live[0]["evidence"], "Any stock?")
        self.assertEqual(live[0]["last_asked_at"], newer["source_at"])
        self.assertEqual(next(r for r in plan["after"] if r["_id"] == "b")["replaced_by"], ["a"])

    def test_existing_components_do_not_split_again_and_variants_stay_distinct(self):
        items = [item(), item("Zotac RTX 5060 Ti")]
        rows = [row("a", items[0]["product_name"]), row("b", items[1]["product_name"])]
        plan = plan_conversation(rows, {"a": items, "b": items}, "report", STAMP)
        self.assertEqual(len(plan["after"]), 2)
        self.assertTrue(all(not r.get("archived_at") for r in plan["after"]))
        again = plan_conversation(plan["after"], {"a": items, "b": items}, "report2", STAMP)
        self.assertEqual(again["changes"], [])

    def test_invalid_archives_but_protected_and_conflicting_rows_need_review(self):
        plan = plan_conversation([row()], {"old": []}, "report", STAMP)
        self.assertEqual(plan["after"][0]["archive_reason"], "not_a_specific_product")
        self.assertEqual(plan["after"][0]["status"], "requested")
        self.assertIn("archived", eligibility_reason(plan["after"][0], {}))
        for extra in [{"consent_note": "Customer asked today"}, {"reviewed_by": "agent"}, {"notification_status": "sent"}]:
            self.assertIn("review", plan_conversation([row(**extra)], {"old": []}, "report", STAMP))
        rows = [row("a", status="fulfilled"), row("b")]
        self.assertIn("review", plan_conversation(rows, {"a": [item()], "b": [item()]}, "report", STAMP))

    def test_reviewed_id_and_permissions_survive_exact_duplicate_merge(self):
        original = row("z", "Zotac RTX 5060", reviewed_by="agent", product_id="catalogue", consent_note="Explicit request", notification_status="sent")
        plan = plan_conversation([row("a"), original], {"a": [item()], "z": [item()]}, "report", STAMP)
        live = [r for r in plan["after"] if not r.get("archived_at")]
        self.assertEqual(live[0]["_id"], "z")
        for field in ["reviewed_by", "product_id", "consent_note", "notification_status"]:
            self.assertEqual(live[0][field], original[field])

    def test_existing_canonical_key_stays_with_its_owner(self):
        owner = row("z", "Zotac RTX 5060", product_key=request_key("org", "chat", "Zotac RTX 5060"))
        alias = row("a", "Zotac RTX 5060")
        plan = plan_conversation([alias, owner], {"a": [item()], "z": [item()]}, "report", STAMP)
        live = [r for r in plan["after"] if not r.get("archived_at")]
        self.assertEqual(live[0]["_id"], "z")
        alias["reviewed_by"] = "agent"
        self.assertIn("review", plan_conversation([alias, owner], {"a": [item()], "z": [item()]}, "report", STAMP))


class Cursor:
    def __init__(self, rows):
        self.rows = deepcopy(rows)
    def sort(self, *args):
        return self
    def limit(self, *args):
        return self
    def __aiter__(self):
        async def values():
            for row in self.rows:
                yield row
        return values()
    async def to_list(self, *args):
        return self.rows


class Context:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    def start_transaction(self):
        return self


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_earlier_evidence_cannot_turn_valid_request_into_archive(self):
        older = message()
        followup = message("Any stock for this?", _id="m2", message_id="m2", timestamp=STAMP + timedelta(minutes=1))
        original = row(name="Zotac RTX 5060", source_message_id="m2", evidence=followup["content"])
        class DB:
            product_requests = SimpleNamespace(find=lambda *a, **k: Cursor([original]))
            def __getitem__(self, name):
                if name.startswith("conversations_"):
                    return SimpleNamespace(find_one=AsyncMock(return_value={"conversation_id": "chat", "platform": "whatsapp"}))
                return SimpleNamespace(find_one=AsyncMock(return_value=followup), find=lambda *a, **k: Cursor([followup, older]))
        raw = extraction()
        self.assertIsNotNone(grounded_request(raw, [older, followup]))
        output = SimpleNamespace(source_message_id="m1", model_dump=lambda: raw)
        report = await make_report(DB(), "org", AsyncMock(return_value=SimpleNamespace(product_requests=[output])))
        self.assertEqual(report["conversations"], [])
        self.assertIn("source", report["review"][0]["reason"].lower())

    async def test_original_customer_evidence_is_required_for_every_cached_row(self):
        source = message("I want Zotac RTX 5060")
        class DB:
            rows = []
            def __init__(self):
                self.product_requests = SimpleNamespace(find=lambda *a, **k: Cursor(self.rows))
            def __getitem__(self, name):
                if name.startswith("conversations_"):
                    return SimpleNamespace(find_one=AsyncMock(return_value={"conversation_id": "chat", "platform": "whatsapp"}))
                return SimpleNamespace(find_one=AsyncMock(return_value=source), find=lambda *a, **k: Cursor([source]))
        db = DB()
        raw = extraction(evidence=source["content"])
        output = SimpleNamespace(source_message_id="m1", model_dump=lambda: raw)
        extract = AsyncMock(return_value=SimpleNamespace(product_requests=[output]))
        db.rows = [row("a", evidence=source["content"]), row("b", evidence="Invented quote")]
        report = await make_report(db, "org", extract)
        self.assertEqual(report["conversations"], [])
        self.assertEqual(len(report["review"]), 1)
        self.assertEqual(extract.await_count, 1)
        for change in ({"role": "agent"}, {"mode": "private"}):
            original_source = deepcopy(source)
            source.update(change)
            db.rows = [row(evidence=source["content"])]
            extract.reset_mock()
            report = await make_report(db, "org", extract)
            self.assertEqual(report["conversations"], [])
            self.assertEqual(len(report["review"]), 1)
            extract.assert_not_awaited()
            source.clear()
            source.update(original_source)

    async def test_v2_output_never_generates_legacy_rewrite_plan(self):
        original = row(evidence="I want Zotac RTX 5060")
        source = message("I want Zotac RTX 5060")
        class DB:
            product_requests = SimpleNamespace(find=lambda *a, **k: Cursor([original]))
            def __getitem__(self, name):
                if name.startswith("conversations_"):
                    return SimpleNamespace(find_one=AsyncMock(return_value={"conversation_id": "chat", "platform": "whatsapp"}))
                return SimpleNamespace(find_one=AsyncMock(return_value=source), find=lambda *a, **k: Cursor([source]))
        output = SimpleNamespace(model_dump=lambda: {**extraction(evidence=source["content"]), "entity_type": "product"})
        report = await make_report(DB(), "org", AsyncMock(return_value=SimpleNamespace(product_requests=[output])))
        self.assertEqual(report["conversations"], [])
        self.assertIn("V2 observations", report["review"][0]["reason"])

    async def test_version_one_report_rejected_before_database_access(self):
        with self.assertRaisesRegex(ValueError, "version.*regenerate"):
            await apply_report(SimpleNamespace(), "org", {"version": 1, "org_id": "org", "report_id": "old", "conversations": []})

    async def test_dry_run_only_reads_and_failed_extraction_never_archives(self):
        original = row(evidence="I want Zotac RTX 5060")
        source = message("I want Zotac RTX 5060")
        class DB:
            product_requests = SimpleNamespace(find=lambda *a, **k: Cursor([original]))
            def __getitem__(self, name):
                if name.startswith("conversations_"):
                    return SimpleNamespace(find_one=AsyncMock(return_value={"conversation_id": "chat", "platform": "whatsapp"}))
                return SimpleNamespace(find_one=AsyncMock(return_value=source), find=lambda *a, **k: Cursor([source]))
        raw = extraction(evidence=source["content"])
        output = SimpleNamespace(source_message_id="m1", model_dump=lambda: raw)
        extract = AsyncMock(return_value=SimpleNamespace(product_requests=[output]))
        report = await make_report(DB(), "org", extract)
        self.assertEqual(report["version"], 2)
        self.assertEqual(len(report["conversations"]), 1)
        self.assertEqual(report["review"], [])
        self.assertEqual(extract.await_count, 1)
        self.assertEqual(original["product_name"], "DDR4 setup with GPU")
        extract.return_value = None
        report = await make_report(DB(), "org", extract)
        self.assertEqual(report["conversations"], [])
        self.assertIn("Extraction failed", report["review"][0]["reason"])
        extract.return_value = SimpleNamespace(product_requests=[])
        report = await make_report(DB(), "org", extract)
        self.assertEqual(report["conversations"][0]["changes"][0]["action"], "archive")

    async def test_apply_does_not_reextract_and_rejects_stale_snapshots(self):
        original = row()
        plan = plan_conversation([original], {"old": [item()]}, "report", STAMP)
        plan.update(conversation_id="chat", contexts=[], conversation={"conversation_id": "chat"})
        report = {"version": 2, "org_id": "org", "report_id": "report", "conversations": [plan]}
        collection = SimpleNamespace(create_index=AsyncMock(), find=lambda *a, **k: Cursor([original]),
                                     replace_one=AsyncMock(), insert_one=AsyncMock())
        class DB:
            product_requests = collection
            client = SimpleNamespace(start_session=AsyncMock(return_value=Context()))
            restock_outbox = SimpleNamespace(find_one=AsyncMock(return_value=None))
            def __getitem__(self, name):
                return SimpleNamespace(find_one=AsyncMock(return_value=plan["conversation"]))
        db = DB()
        result = await apply_report(db, "org", report)
        self.assertEqual(result["applied_conversations"], 1)
        self.assertEqual(collection.replace_one.await_count, 1)
        collection.find = lambda *a, **k: Cursor(plan["after"])
        result = await apply_report(db, "org", report)
        self.assertEqual(result["already_applied"], 1)
        self.assertEqual(collection.replace_one.await_count, 1)
        collection.find = lambda *a, **k: Cursor([{**original, "status": "fulfilled"}])
        with self.assertRaisesRegex(ValueError, "Stale report"):
            await apply_report(db, "org", report)
        with self.assertRaisesRegex(ValueError, "organization"):
            await apply_report(db, "other-org", report)
        collection.find = lambda *a, **k: Cursor([original])
        db.restock_outbox.find_one.return_value = {"status": "sending"}
        with self.assertRaisesRegex(ValueError, "Pending outreach"):
            await apply_report(db, "org", report)


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_archived_requests_are_excluded_from_capture_and_outreach(self):
        from test_product_request_v2 import CaptureV2Test
        harness = CaptureV2Test()
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        archived = harness.seed(archived_at=STAMP)
        await harness.capture(*harness.observation())
        self.assertEqual(archived["archived_at"], STAMP)
        self.assertNotIn("revision", archived)
        self.assertEqual(len(harness.db.product_requests.rows), 2)
        self.assertIn("archived", eligibility_reason(archived, {}))


async def evaluate_live():
    """Keep the old command usable with the current bounded v2 evaluator."""
    from scripts.evaluate_product_requests import evaluate
    raise SystemExit(await evaluate(24))


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--live"]:
        import asyncio
        asyncio.run(evaluate_live())
    else:
        unittest.main()
