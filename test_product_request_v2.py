"""Exercise the real service functions with a small Mongo-style memory store."""
import ast
import asyncio
import copy
import json
import re
import types
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import test_restock_dispatch as restock
from test_restock_dispatch import service, row, HTTPException
from services.product_request_policy import grounded_request, needs_verification, product_identity, eligibility_reason


def matches(row, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(row, part) for part in expected): return False
            continue
        if key == "$and":
            if not all(matches(row, part) for part in expected): return False
            continue
        actual = row.get(key)
        if not isinstance(expected, dict):
            if actual != expected: return False
            continue
        for operator, value in expected.items():
            if operator == "$options": continue
            if operator == "$exists" and (key in row) != value: return False
            elif operator == "$in" and actual not in value: return False
            elif operator == "$ne" and actual == value: return False
            elif operator == "$lt" and not (actual is not None and actual < value): return False
            elif operator == "$regex" and not re.search(value, str(actual or ""), re.I if expected.get("$options") == "i" else 0): return False
            elif operator not in {"$exists", "$in", "$ne", "$lt", "$regex"}: raise AssertionError(operator)
    return True


class Cursor:
    def __init__(self, rows): self.rows = rows
    def limit(self, count): self.rows = self.rows[:count]; return self
    async def to_list(self, count): return copy.deepcopy(self.rows[:count])


class MemoryCollection:
    def __init__(self, rows=()):
        self.rows = {r["_id"]: copy.deepcopy(r) for r in rows}
        self.before_update = None
    async def create_index(self, *args, **kwargs): return "product_key"
    def find(self, query): return Cursor([r for r in self.rows.values() if matches(r, query)])
    async def find_one(self, query, *args):
        return next((copy.deepcopy(r) for r in self.rows.values() if matches(r, query)), None)
    async def update_one(self, query, update, upsert=False):
        if self.before_update:
            hook, self.before_update = self.before_update, None
            hook(query, update)
        target = next((r for r in self.rows.values() if matches(r, query)), None)
        inserted = target is None and upsert
        if inserted:
            target = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
            target.update(copy.deepcopy(update.get("$setOnInsert", {})))
            self.rows[target["_id"]] = target
        if target is None: return types.SimpleNamespace(matched_count=0)
        target.update(copy.deepcopy(update.get("$set", {})))
        for key in update.get("$unset", {}): target.pop(key, None)
        for key, value in update.get("$inc", {}).items(): target[key] = target.get(key, 0) + value
        return types.SimpleNamespace(matched_count=int(not inserted))
    async def update_many(self, query, update):
        ids = [r["_id"] for r in self.rows.values() if matches(r, query)]
        for key in ids: await self.update_one({"_id": key}, update)
        return types.SimpleNamespace(matched_count=len(ids))


class MemoryDB:
    def __init__(self, **collections):
        self.collections = collections
        for name in ("product_requests", "product_request_events", "restock_outbox"):
            self.collections.setdefault(name, MemoryCollection())
    def __getattr__(self, name): return self.collections[name]
    def __getitem__(self, name): return self.collections[name]


class CaptureV2Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = service()
        self.db = MemoryDB()
        self.db.collections["conversations_org"] = MemoryCollection([{
            "_id": "chat", "conversation_id": "chat", "platform": "whatsapp", "customer_id": "919123456789", "business_phone_id": "12345"}])
        self.scope["get_async_mongo_db"] = lambda: self.db
        self.scope["catalogue_org"] = AsyncMock(return_value="org")
        self.exact_catalogue_product = self.scope["exact_catalogue_product"]
        self.scope["exact_catalogue_product"] = AsyncMock(return_value=None)
        self.scope["require_conversation_access"] = AsyncMock(return_value={"platform": "whatsapp", "customer_id": "919123456789", "business_phone_id": "12345"})
        self.verifier = AsyncMock(side_effect=self.confirm)
        self.modules = patch.dict("sys.modules", {"agents.intent_agent.agent": types.SimpleNamespace(verify_product_observations=self.verifier)})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.job = {"org_id": "org", "conversation_id": "chat", "business_phone_id": "12345"}
        self.stamp = datetime.now(timezone.utc)
        self.user = types.SimpleNamespace(org_id="org", user_id="agent", role="admin")

    async def confirm(self, candidates, messages):
        return [{**c, "verification_status": "confirmed", "reason": "Fixture verified"} for c in candidates]

    def observation(self, name="Laptop", content="I want Laptop", offset=0, **fields):
        message = {"message_id": "m" + str(offset), "role": "customer", "content": content, "timestamp": self.stamp + timedelta(seconds=offset)}
        raw = {"product_name": name, "source_message_id": message["message_id"], "evidence": content, "confidence": "high", **fields}
        return raw, message

    async def capture(self, raw, message, context=()):
        await self.scope["capture_product_requests"](self.job, [raw], [message], context)
        return next(iter(self.db.product_requests.rows.values()), None)

    def seed(self, **changes):
        entry = {**row(), "conversation_id": "chat", "product_name": "Laptop", **changes}
        self.db.product_requests.rows[entry["_id"]] = entry
        return entry

    async def test_catalogue_matching_is_tenant_exact_unique_and_variant_compatible(self):
        self.db.collections["products_selected"] = MemoryCollection([{
            "_id": "gpu", "product_name": "RTX-4060 Ti", "color": "Black", "description": "White also offered"}])
        raw, message = self.observation("RTX-4060 Ti", "I want RTX-4060 Ti White")
        item = grounded_request(raw, [message])
        self.assertEqual((await self.exact_catalogue_product("selected", item))["_id"], "gpu")
        item["requirements"] = [{"name": "colour", "value": "White"}]
        self.assertIsNone(await self.exact_catalogue_product("selected", item))
        item["requirements"] = [{"name": "colour", "value": "Black"}]
        self.assertEqual((await self.exact_catalogue_product("selected", item))["_id"], "gpu")
        item["anchor_product_id"] = "different-offer"
        self.assertIsNone(await self.exact_catalogue_product("selected", item))
        item.pop("anchor_product_id")
        self.db.collections["products_selected"].rows["duplicate"] = {"_id": "duplicate", "product_name": "RTX-4060 Ti"}
        self.assertIsNone(await self.exact_catalogue_product("selected", item))

    async def test_changed_product_cannot_inherit_unchanged_consent_note(self):
        saved = self.seed()
        self.scope["catalogue_org"].return_value = "org"
        self.scope["product_for"] = AsyncMock(return_value={"product_name": "Camera"})
        payload = self.review_payload(saved, product_id="camera", product_name="Camera",
            consent_note=saved["consent_note"], model_fields_set={"product_id", "product_name", "consent_note"})
        await self.scope["review_request"](self.user, saved["_id"], payload)
        self.assertFalse(saved["consent_note"])
        self.assertEqual(saved["product_id"], "camera")

    async def test_unknown_title_and_category_are_confirmable_without_catalogue(self):
        for name in ("Hello Kitty backpack", "Nothing Phone (2)", "M.2 SSD", "लैपटॉप"):
            raw, message = self.observation(name, "I need " + name, offset=len(self.db.product_request_events.rows))
            saved = await self.capture(raw, message)
        self.assertEqual(len(self.db.product_requests.rows), 4)
        self.assertTrue(all(r["verification_status"] == "confirmed" and not r.get("product_id") for r in self.db.product_requests.rows.values()))

    async def test_verifier_failure_and_semantic_disagreement_go_to_review(self):
        self.verifier.side_effect = TimeoutError()
        saved = await self.capture(*self.observation("Laptop", "I do not want Laptop"))
        self.assertEqual(saved["verification_status"], "needs_review")
        self.assertTrue(saved["review_hold"])
        self.assertFalse(saved.get("consent_by"))
        self.verifier.side_effect = lambda candidates, messages: [{**candidates[0], "verification_status": "confirmed", "action": "withdrawal", "reason": "Negated"}]
        saved = await self.capture(*self.observation("Laptop", "I do not want Laptop", offset=1))
        self.assertEqual(saved["verification_status"], "needs_review")
        self.assertEqual(saved["pending_event"]["evidence"], "I do not want Laptop")

    async def test_rejected_sentences_and_mentions_never_create_active_rows(self):
        self.verifier.side_effect = lambda candidates, messages: [{**c, "verification_status": "rejected", "reason": "Sentence not product"} for c in candidates]
        self.assertIsNone(await self.capture(*self.observation("I need a Laptop", "I need a Laptop")))
        self.verifier.side_effect = self.confirm
        self.assertIsNone(await self.capture(*self.observation("Laptop", "Laptop works fine", offset=1, action="mention_only")))
        self.assertEqual(len(self.db.product_request_events.rows), 2)

    async def test_rejected_sentence_withdrawal_still_holds_existing_outreach(self):
        saved = self.seed(notification_status="queued", notification_id="job")
        self.verifier.side_effect = lambda candidates, messages: [{**candidates[0], "product_name": "Laptop",
            "action": "withdrawal", "verification_status": "rejected", "reason": "Sentence supplied as title"}]
        await self.capture(*self.observation("I no longer want Laptop", "I no longer want Laptop", offset=2))
        self.assertEqual(saved["product_name"], "Laptop")
        self.assertEqual(saved["status"], "requested")
        self.assertTrue(saved["review_hold"])
        self.assertEqual(saved["notification_status"], "cancelled")

    async def test_only_direct_known_enquiry_avoids_extra_call(self):
        self.scope["exact_catalogue_product"].return_value = {"_id": "p1"}
        saved = await self.capture(*self.observation())
        self.verifier.assert_not_awaited()
        self.assertEqual(saved["verification_status"], "confirmed")
        self.assertEqual(saved["candidate_product_id"], "p1")
        self.assertFalse(saved.get("reviewed_by"))
        await self.capture(*self.observation(content="I do not want Laptop", offset=1))
        self.verifier.assert_awaited_once()

    async def test_cache_and_replay_do_not_refresh_time_or_revision(self):
        raw, message = self.observation()
        saved = await self.capture(raw, message)
        original = copy.deepcopy(saved)
        await self.capture(raw, message)
        self.assertEqual(saved, original)
        self.verifier.assert_awaited_once()
        event = next(iter(self.db.product_request_events.rows.values()))
        event["applied"] = False  # Crash after row write but before event acknowledgement.
        await self.capture(raw, message)
        self.assertEqual(saved, original)
        self.assertTrue(event["applied"])
        self.verifier.assert_awaited_once()

    async def test_withdrawal_cancels_queued_job_and_old_enquiry_cannot_revive(self):
        saved = self.seed(notification_status="queued", notification_id="job")
        self.db.restock_outbox.rows["job"] = {"_id": "job", "org_id": "org", "request_id": saved["_id"], "status": "checking"}
        saved = await self.capture(*self.observation(content="Cancel Laptop", offset=5, action="withdrawal"))
        self.assertEqual(saved["status"], "dismissed")
        self.assertEqual(saved["notification_status"], "cancelled")
        self.assertEqual(self.db.restock_outbox.rows["job"]["status"], "cancelled")
        await self.capture(*self.observation(offset=1))
        self.assertEqual(saved["status"], "dismissed")

    async def test_new_interest_in_closed_row_requires_review_and_cannot_restore_consent(self):
        saved = self.seed(status="dismissed", last_event_at=self.stamp, last_action="withdrawal", consent_note="", consent_by=None)
        await self.capture(*self.observation(offset=2))
        self.assertEqual(saved["status"], "dismissed")
        self.assertTrue(saved["review_hold"])
        payload = self.review_payload(saved, status="requested")
        await self.scope["review_request"](self.user, saved["_id"], payload)
        self.assertEqual(saved["status"], "requested")
        self.assertFalse(saved.get("consent_by"))

    async def test_ambiguous_withdrawal_holds_existing_interest_without_erasing_it(self):
        saved = self.seed(notification_status="queued", notification_id="job")
        await self.capture(*self.observation(None, "Do not notify me about that one", offset=2,
                                            entity_type="unresolved", action="withdrawal", notification_action="revoke"))
        self.assertEqual(saved["product_name"], "Laptop")
        self.assertEqual(saved["status"], "requested")
        self.assertTrue(saved["review_hold"])
        self.assertEqual(saved["notification_status"], "cancelled")
        self.assertTrue(eligibility_reason(saved, {"stock": 1, "updated_at": self.stamp}))

    async def test_conflicting_same_time_observations_and_variants(self):
        await self.capture(*self.observation(offset=1))
        await self.capture(*self.observation(content="Cancel Laptop", offset=1, action="withdrawal"))
        # Same source/entity is replayed, not a second state transition.
        self.assertEqual(len(self.db.product_request_events.rows), 1)
        await self.capture(*self.observation("Laptop 16GB", "I want Laptop 16GB", offset=2))
        self.assertEqual(len(self.db.product_requests.rows), 2)
        raw, message = self.observation(content="I want Laptop, cancel Laptop", offset=3)
        other = {**raw, "action": "withdrawal"}
        await self.scope["capture_product_requests"](self.job, [raw, other], [message])
        saved = next(r for r in self.db.product_requests.rows.values() if r["product_name"] == "Laptop")
        self.assertTrue(saved["review_hold"])

    async def test_reference_verifier_receives_intervening_switch_and_full_source(self):
        old = {"message_id": "old", "role": "customer", "content": "Laptop", "timestamp": self.stamp}
        switched = {"message_id": "switch", "role": "customer", "content": "Actually I want Camera", "timestamp": self.stamp + timedelta(seconds=1)}
        raw, message = self.observation("Laptop", "What is the price of that one?", offset=2, product_source_message_id="old")
        self.verifier.side_effect = lambda candidates, messages: [{**candidates[0], "verification_status": "needs_review", "reason": "Switched topic"}]
        saved = await self.capture(raw, message, [old, switched])
        self.assertEqual(saved["verification_status"], "needs_review")
        self.assertIn("Actually I want Camera", str(self.verifier.await_args.args[1]))
        raw, message = self.observation("SSD", "I want SSD. " + "extra context " * 200 + "Do not notify me", offset=4)
        raw["evidence"] = "I want SSD."
        await self.capture(raw, message)
        candidate = self.verifier.await_args.args[0][0]
        self.assertTrue(candidate["source_text"].endswith("Do not notify me"))

    async def test_staff_review_revision_and_reject_pending_preserves_accepted_interest(self):
        saved = self.seed(review_hold=True, revision=2, pending_event={"product_name": "Laptop", "action": "withdrawal",
            "source_at": self.stamp + timedelta(seconds=2), "event_id": "event", "evidence": "maybe cancel Laptop"})
        stale = self.review_payload(saved, expected_revision=1)
        with self.assertRaises(HTTPException) as caught:
            await self.scope["review_request"](self.user, saved["_id"], stale)
        self.assertEqual(caught.exception.status_code, 409)
        payload = self.review_payload(saved, verification_status="rejected", status="dismissed")
        await self.scope["review_request"](self.user, saved["_id"], payload)
        self.assertEqual(saved["status"], "requested")
        self.assertEqual(saved["verification_status"], "confirmed")
        self.assertFalse(saved["review_hold"])
        self.assertNotIn("pending_event", saved)

    async def test_catalogue_identity_adopts_existing_unknown_row_without_changing_id(self):
        saved = await self.capture(*self.observation())
        request_id = saved["_id"]
        self.scope["exact_catalogue_product"].return_value = {"_id": "catalogue-product"}
        await self.capture(*self.observation(offset=2))
        self.assertEqual(list(self.db.product_requests.rows), [request_id])
        self.assertEqual(saved["identity_key"], "catalogue:org:catalogue-product")

    async def test_one_batch_verification_and_invalid_sibling_are_isolated(self):
        raw, message = self.observation("Laptop", "I need Laptop and Camera")
        other = {**raw, "product_name": "Camera"}
        invalid = {**raw, "product_name": "Invented"}
        self.verifier.side_effect = lambda candidates, messages: [
            {**candidates[0], "verification_status": "confirmed", "reason": "Supported"},
            {"candidate_id": candidates[1]["candidate_id"], "verification_status": "confirmed"}]
        await self.scope["capture_product_requests"](self.job, [raw, invalid, other], [message])
        self.verifier.assert_awaited_once()
        states = {r["product_name"]: r["verification_status"] for r in self.db.product_requests.rows.values()}
        self.assertEqual(states, {"Laptop": "confirmed", "Camera": "needs_review"})

    async def test_keeping_pending_review_preserves_accepted_facts(self):
        saved = self.seed(verification_status="confirmed", evidence="I want Laptop", revision=1,
            pending_event={"source_at": self.stamp + timedelta(seconds=2), "event_id": "pending",
                           "product_name": "Laptop", "action": "withdrawal", "evidence": "Maybe cancel Laptop"})
        await self.scope["review_request"](self.user, saved["_id"], self.review_payload(saved, verification_status="needs_review"))
        self.assertEqual(saved["verification_status"], "confirmed")
        self.assertEqual(saved["evidence"], "I want Laptop")
        self.assertEqual(saved["pending_event"]["evidence"], "Maybe cancel Laptop")
        self.assertTrue(saved["review_hold"])

    async def test_reopening_without_pending_clears_old_consent_and_keeps_notification_dedup(self):
        saved = self.seed(status="dismissed", notification_status="accepted", notification_id="original")
        payload = self.review_payload(saved, consent_note=saved["consent_note"], status="requested")
        await self.scope["review_request"](self.user, saved["_id"], payload)
        self.assertFalse(saved["consent_note"])
        self.assertEqual(saved["notification_status"], "accepted")
        self.assertEqual(saved["notification_id"], "original")

    async def test_same_timestamp_conflicting_sources_need_review(self):
        saved = await self.capture(*self.observation(offset=1))
        raw, message = self.observation(content="Cancel Laptop", offset=1, action="withdrawal")
        raw["source_message_id"] = message["message_id"] = "different-source"
        await self.capture(raw, message)
        self.assertEqual(saved["status"], "requested")
        self.assertTrue(saved["review_hold"])

    async def test_unresolved_positive_gets_own_review_row(self):
        original = self.seed()
        await self.capture(*self.observation(None, "What is its price?", offset=1, entity_type="unresolved"))
        self.assertEqual(original["status"], "requested")
        self.assertFalse(original.get("review_hold"))
        pending = next(r for r in self.db.product_requests.rows.values() if not r["product_name"])
        self.assertEqual(pending["verification_status"], "needs_review")

    def review_payload(self, saved, **changes):
        return types.SimpleNamespace(**{
            "expected_revision": saved.get("revision", 0), "verification_status": "confirmed", "product_name": None,
            "product_id": None, "status": "requested", "consent_note": None, "model_fields_set": set(), **changes})

    async def test_capture_edit_cas_retries_if_sender_claims_first(self):
        saved = self.seed(notification_status="queued", notification_id="job")
        self.db.restock_outbox.rows["job"] = {"_id": "job", "org_id": "org", "request_id": saved["_id"], "status": "checking"}
        self.db.product_requests.before_update = lambda q, u: saved.update(notification_status="sending")
        await self.capture(*self.observation(content="Cancel Laptop", offset=2, action="withdrawal"))
        self.assertEqual(saved["status"], "dismissed")
        self.assertEqual(saved["notification_status"], "sending")
        self.assertEqual(self.db.restock_outbox.rows["job"]["status"], "checking")

    async def test_original_line_and_tenant_are_required(self):
        self.job["business_phone_id"] = "other"
        await self.capture(*self.observation())
        self.assertEqual(self.db.product_requests.rows, {})


class AtomicSendTest(unittest.IsolatedAsyncioTestCase):
    setUp = restock.RestockSafetyTest.setUp
    dispatch_mocks = restock.RestockSafetyTest.dispatch_mocks

    async def test_withdrawal_between_final_read_and_row_claim_prevents_provider_call(self):
        modules, wa = self.dispatch_mocks()
        def guard(match, update):
            if update.get("$set", {}).get("notification_status") == "sending":
                self.assertIn("revision", match)
                self.assertIn("consent_note", match)
                self.assertEqual(match["status"], "requested")
                return types.SimpleNamespace(matched_count=0)
            return types.SimpleNamespace(matched_count=1)
        self.db.product_requests.update_one.side_effect = guard
        with patch.dict("sys.modules", modules):
            await self.scope["process_restock_outbox"]()
        wa.send_whatsapp_template_message.assert_not_awaited()
        self.assertEqual(self.db.restock_outbox.update_one.call_args.args[1]["$set"]["status"], "blocked")

    async def test_queue_race_returns_conflict_instead_of_reporting_already_queued(self):
        modules, wa = self.dispatch_mocks()
        self.scope["restock_preview"] = AsyncMock(return_value=[{"id": "request", "eligible": True}])
        self.scope["accessible_request"] = AsyncMock(return_value=self.row)
        self.scope["publish_restock_job"] = AsyncMock(return_value=False)
        self.db.restock_outbox.insert_one = AsyncMock()
        with patch.dict("sys.modules", modules), self.assertRaises(HTTPException) as caught:
            await self.scope["queue_restock"](self.user, ["request"], {"request": 0})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("0 already queued", caught.exception.detail)
        wa.send_whatsapp_template_message.assert_not_awaited()

    async def test_crash_between_request_claim_and_outbox_claim_is_unknown(self):
        modules, wa = self.dispatch_mocks()
        self.row["notification_status"] = "sending"
        old = {"_id": self.row["notification_id"], "request_id": self.row["_id"], "org_id": "org",
               "status": "checking", "started_at": datetime.now(timezone.utc) - timedelta(minutes=6)}
        self.db.restock_outbox.find.return_value.limit.return_value.to_list.side_effect = [[old], []]
        self.db.restock_outbox.find_one.return_value = None
        with patch.dict("sys.modules", modules):
            await self.scope["process_restock_outbox"]()
        update = self.db.restock_outbox.update_one.call_args_list[0].args[1]["$set"]
        self.assertEqual(update["status"], "unknown")
        self.assertTrue(update["send_attempted"])
        modules["services.entitlement_service"].release_quota_async.assert_not_awaited()
        wa.send_whatsapp_template_message.assert_not_awaited()

    async def test_send_rejects_changed_revision_since_preview(self):
        modules, wa = self.dispatch_mocks()
        self.scope["restock_preview"] = AsyncMock(return_value=[{"id": "request", "eligible": True}])
        self.scope["accessible_request"] = AsyncMock(return_value={**self.row, "revision": 2})
        with patch.dict("sys.modules", modules), self.assertRaises(HTTPException) as caught:
            await self.scope["queue_restock"](self.user, ["request"], {"request": 1})
        self.assertEqual(caught.exception.status_code, 409)
        wa.get_templates.assert_not_awaited()


class VariantAndOfferPolicyTest(unittest.TestCase):
    def test_requirements_need_exact_customer_quotes_and_identity_preserves_variants(self):
        stamp = datetime.now(timezone.utc)
        message = {"message_id": "source", "role": "customer", "content": "I want Laptop 16GB Black, 2 units", "timestamp": stamp}
        raw = {"source_message_id": "source", "product_name": "Laptop", "evidence": message["content"], "confidence": "high",
               "requirements": [{"name": "capacity", "value": "16GB", "quote": "16GB"},
                                {"name": "colour", "value": "Black", "quote": "Black"}]}
        item = grounded_request(raw, [message])
        self.assertIsNotNone(item)
        original = product_identity(item)
        item["requirements"].append({"name": "quantity", "value": "2 units"})
        self.assertEqual(product_identity(item), original)
        item["requirements"][0]["value"] = "32GB"
        self.assertNotEqual(product_identity(item), original)
        raw["requirements"][0] = {"name": "capacity", "value": "32GB", "quote": "16GB"}
        self.assertIsNone(grounded_request(raw, [message]))

    def test_agent_offer_requires_customer_reply_and_structured_catalogue_identity(self):
        stamp = datetime.now(timezone.utc)
        offer = {"message_id": "offer", "role": "agent", "content": "Laptop available", "timestamp": stamp,
                 "metadata": {"product_id": "p1"}}
        message = {"message_id": "choice", "role": "customer", "content": "I want that one", "context": {"id": "offer"},
                   "timestamp": stamp + timedelta(seconds=1)}
        raw = {"product_name": "Laptop", "product_source_message_id": "offer", "source_message_id": "choice",
               "evidence": message["content"], "confidence": "high"}
        item = grounded_request(raw, [message], [offer])
        self.assertEqual(item["anchor_product_id"], "p1")
        self.assertTrue(needs_verification(item, "p1"))
        self.assertIsNone(grounded_request(raw, [{**message, "context": {}}], [offer]))
        self.assertIsNone(grounded_request(raw, [message], [{**offer, "metadata": {}}]))


class SourceQueueTest(unittest.TestCase):
    def test_pending_coalescing_retains_source_and_line_and_processing_race_creates_job(self):
        source = ast.parse(Path("services/intent_queue_worker.py").read_text())
        node = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "enqueue_intent_check")
        collection = Mock()
        collection.find_one.return_value = {"job_id": "pending"}
        collection.update_one.return_value = types.SimpleNamespace(matched_count=1)
        scope = {"datetime": datetime, "timezone": timezone, "timedelta": timedelta, "uuid": uuid,
                 "db": {"queue": collection}, "COLLECTION": "queue", "logger": Mock()}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "queue", "exec"), scope)
        enqueue = scope["enqueue_intent_check"]
        self.assertEqual(enqueue("org", "chat", "919123456789", "Customer", "Need SSD", "m2", "12345"), "pending")
        self.assertEqual(collection.find_one.call_args.args[0]["business_phone_id"], "12345")
        self.assertEqual(collection.update_one.call_args.args[1]["$addToSet"]["source_message_ids"]["$each"], ["m2"])
        collection.update_one.return_value.matched_count = 0
        enqueue("org", "chat", "919123456789", "Customer", "Need SSD", "m3", "12345")
        self.assertEqual(collection.insert_one.call_args.args[0]["source_message_ids"], ["m3"])


class WorkerSourceTest(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, broken_capture=False):
        from zoneinfo import ZoneInfo
        stamp = datetime.now(timezone.utc)
        trigger = {"message_id": "old-source", "role": "customer", "content": "I need SSD", "timestamp": stamp}
        chatter = [{"message_id": f"private{i}", "role": "agent", "mode": "private", "content": "Private",
                    "timestamp": stamp + timedelta(seconds=i + 1)} for i in range(35)]
        class SyncCursor:
            def __init__(self, docs): self.docs = docs
            def sort(self, field, direction): self.docs = sorted(self.docs, key=lambda d: d[field], reverse=direction < 0); return self
            def limit(self, size): self.docs = self.docs[:size]; return self
            def __iter__(self): return iter(self.docs)
        def find(query):
            if "message_id" in query: return SyncCursor([trigger])
            if "timestamp" in query: return SyncCursor([trigger])
            return SyncCursor(chatter)
        queue = types.SimpleNamespace(update_one=Mock())
        extraction = types.SimpleNamespace(has_intent=True, confidence="high", product_name="SSD",
            tentative_date="2026-10-11T10:00:00+05:30", product_requests=[])
        scope = {"asyncio": asyncio, "datetime": datetime, "timezone": timezone, "ZoneInfo": ZoneInfo,
                 "db": {"messages_org": types.SimpleNamespace(find=find), "queue": queue}, "COLLECTION": "queue",
                 "MESSAGE_CONTEXT_LIMIT": 30, "logger": Mock(), "extract_purchase_intent": AsyncMock(return_value=extraction),
                 "save_intent_to_sheet": AsyncMock(), "schedule_intent_followup": AsyncMock()}
        node = next(n for n in ast.parse(Path("services/intent_queue_worker.py").read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_single_job")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "worker", "exec"), scope)
        capture = AsyncMock(side_effect=RuntimeError("Database unavailable") if broken_capture else None)
        with patch.dict("sys.modules", {"services.product_request_service": types.SimpleNamespace(capture_product_requests=capture)}):
            await scope["_process_single_job"]({"job_id": "job", "org_id": "org", "conversation_id": "chat",
                "source_message_ids": ["old-source"], "business_phone_id": "12345"}, asyncio.Semaphore(1))
        scope["schedule_intent_followup"].assert_awaited_once()
        return scope, capture, queue

    async def test_source_survives_private_chatter_beyond_context_window(self):
        scope, capture, queue = await self.run_worker()
        self.assertEqual(capture.await_args.args[2][0]["message_id"], "old-source")
        self.assertIn("PRODUCT SOURCE IDS: old-source", scope["extract_purchase_intent"].await_args.args[0])
        self.assertNotIn("Private", scope["extract_purchase_intent"].await_args.args[0])
        self.assertEqual(queue.update_one.call_args.args[1]["$set"]["status"], "completed")

    async def test_capture_failure_keeps_followups_then_retries_product_stage(self):
        _, capture, queue = await self.run_worker(broken_capture=True)
        capture.assert_awaited_once()
        self.assertEqual(queue.update_one.call_args.args[1]["$set"]["status"], "pending")


if __name__ == "__main__": unittest.main()
