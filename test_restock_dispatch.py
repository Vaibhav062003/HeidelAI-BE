"""Offline restock safety tests; no database, credentials or network imports."""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import json
from uuid import uuid4
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from services import product_request_policy as policy
from services.product_request_policy import eligibility_reason, request_key, approved_restock_template


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class DuplicateKeyError(Exception):
    pass


def service():
    source = ast.parse(Path("services/product_request_service.py").read_text())
    functions = [node for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    scope = dict(datetime=datetime, timedelta=timedelta, timezone=timezone, re=re,
                 HTTPException=HTTPException, DuplicateKeyError=DuplicateKeyError,
                 ReturnDocument=types.SimpleNamespace(AFTER=1), request_key=request_key,
                 eligibility_reason=eligibility_reason, approved_restock_template=lambda _: True,
                 RESTOCK_TEMPLATE="restock", RESTOCK_BODY="body", RESTOCK_FOOTER="footer")
    import asyncio
    scope.update({name: value for name, value in vars(policy).items() if not name.startswith("_")})
    scope.update(asyncio=asyncio, json=json, logger=Mock(), uuid4=uuid4)
    scope["approved_restock_template"] = lambda _: True
    exec(compile(ast.Module(body=functions, type_ignores=[]), "product_request_service", "exec"), scope)
    return scope


def row():
    stamp = datetime.now(timezone.utc)
    return {"_id": "request", "org_id": "org", "conversation_id": "conversation", "status": "requested",
            "product_name": "Laptop", "product_id": "product", "catalogue_org_id": "org", "catalogue_name": "Laptop",
            "reviewed_by": "agent", "consent_note": "Customer asked for restock notification today",
            "consent_by": "agent", "consent_at": stamp, "last_asked_at": stamp,
            "customer_phone": "919123456789", "business_phone_id": "12345"}


class RestockSafetyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = service()
        self.user = types.SimpleNamespace(org_id="org", user_id="admin", role="admin")
        self.row = row()
        self.db = types.SimpleNamespace(product_request_events=types.SimpleNamespace(update_one=AsyncMock()), product_requests=Mock(), restock_outbox=Mock(), restock_dispatch=Mock())
        self.db.product_requests.find_one = AsyncMock(return_value=self.row)
        self.db.product_requests.update_one = AsyncMock(return_value=types.SimpleNamespace(matched_count=1))
        self.db.restock_outbox.update_one = AsyncMock(return_value=types.SimpleNamespace(matched_count=1))
        self.db.restock_outbox.update_many = AsyncMock()
        self.db.restock_outbox.find_one = AsyncMock(return_value=None)
        self.scope["get_async_mongo_db"] = lambda: self.db
        self.scope["require_conversation_access"] = AsyncMock(return_value={"platform": "whatsapp", "customer_id": "+91 91234 56789", "business_phone_id": "12345"})

    async def test_current_customer_and_sender_are_required(self):
        self.assertEqual(await self.scope["accessible_request"](self.user, "request"), self.row)
        for change in ({"customer_id": "919999999999"}, {"business_phone_id": "999"}, {"platform": "instagram"}, {"business_phone_id": None}):
            self.scope["require_conversation_access"].return_value = {"platform": "whatsapp", "customer_id": "919123456789", "business_phone_id": "12345", **change}
            with self.assertRaises(HTTPException) as caught:
                await self.scope["accessible_request"](self.user, "request")
            self.assertEqual(caught.exception.status_code, 409)

    async def test_sheet_product_requires_active_source_but_manual_does_not(self):
        stock = types.ModuleType("services.stock_service")
        stock.effective_stock_product = AsyncMock(side_effect=lambda org, product, **kwargs: product)
        module_patch = patch.dict("sys.modules", {"services.stock_service": stock})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        products = types.SimpleNamespace(find_one=AsyncMock(return_value={"_id": "product", "stock": 2, "source_sheet_ids": ["sheet"]}))
        connections = types.SimpleNamespace(find_one=AsyncMock(return_value=None))
        class CatalogueDB:
            product_sheet_connections = connections
            def __getitem__(self, name):
                if name != "products_org":
                    raise AssertionError("Wrong catalogue organization")
                return products
        self.scope["get_async_mongo_db"] = CatalogueDB
        self.scope["catalogue_org"] = AsyncMock(return_value="org")
        self.scope["ObjectId"] = types.SimpleNamespace(is_valid=lambda _: False)
        self.assertIsNone(await self.scope["product_for"](self.row))
        match = connections.find_one.call_args.args[0]
        self.assertEqual(match["org_id"], "org")
        self.assertEqual(match["status"]["$in"], ["active", "syncing"])
        connections.find_one.return_value = {"_id": "active-sheet"}
        self.assertIsNotNone(await self.scope["product_for"](self.row))
        products.find_one.return_value = {"_id": "product", "stock": 2}
        connections.find_one.reset_mock()
        self.assertIsNotNone(await self.scope["product_for"](self.row))
        connections.find_one.assert_not_awaited()
        self.assertEqual(stock.effective_stock_product.await_count, 2)

    async def test_publisher_updates_request_before_exposing_job(self):
        sequence = []
        async def request_update(*args):
            sequence.append("request")
            return types.SimpleNamespace(matched_count=1)
        async def job_update(match, update):
            sequence.append("publish")
            self.assertEqual(match["status"], "preparing")
            self.assertEqual(update["$set"]["status"], "queued")
            return types.SimpleNamespace(matched_count=1)
        self.db.product_requests.update_one.side_effect = request_update
        self.db.restock_outbox.update_one.side_effect = job_update
        self.assertTrue(await self.scope["publish_restock_job"](self.db, self.row, "job"))
        self.assertEqual(sequence, ["request", "publish"])

    async def test_changed_request_never_publishes_job(self):
        self.db.product_requests.update_one.return_value.matched_count = 0
        self.assertFalse(await self.scope["publish_restock_job"](self.db, self.row, "job"))
        self.assertEqual(self.db.restock_outbox.update_one.call_args.args[1]["$set"]["status"], "blocked")

    async def test_consent_withdrawal_claims_row_before_cancelling_outbox(self):
        self.row["notification_status"] = "queued"
        sequence = []
        self.db.restock_outbox.update_many.side_effect = lambda *args: sequence.append("cancel")
        async def update(*args):
            sequence.append("row")
            return types.SimpleNamespace(matched_count=1)
        self.db.product_requests.update_one.side_effect = update
        payload = types.SimpleNamespace(expected_revision=0, verification_status="confirmed", product_name=None, status="requested", product_id=None, consent_note="", model_fields_set={"consent_note"})
        result = await self.scope["review_request"](self.user, "request", payload)
        self.assertFalse(result["outreach_already_sending"])
        self.assertEqual(sequence, ["row", "cancel"])
        self.assertEqual(self.db.product_requests.update_one.call_args.args[1]["$set"]["consent_note"], "")

    async def test_explicit_null_clears_match(self):
        payload = types.SimpleNamespace(expected_revision=0, verification_status="confirmed", product_name=None, status="requested", product_id=None, consent_note=None, model_fields_set={"product_id"})
        await self.scope["review_request"](self.user, "request", payload)
        self.assertIn("product_id", self.db.product_requests.update_one.call_args.args[1]["$unset"])

    async def test_removed_or_demoted_actor_cannot_send(self):
        cache = {"org": [{"role": "admin"}]}
        module = types.ModuleType("api.transfer_routes")
        module._clerk_cache = cache
        module.membership_public_data = lambda membership: membership.get("public_user_data") or {}
        module.get_org_memberships = AsyncMock(return_value=[{"role": "org:member", "public_user_data": {"user_id": "admin"}}])
        with patch.dict("sys.modules", {"api.transfer_routes": module}):
            with self.assertRaises(HTTPException):
                await self.scope["current_outreach_actor"]({"org_id": "org", "actor_id": "admin"})
            self.assertNotIn("org", cache)
            module.get_org_memberships.return_value[0]["role"] = "org:admin"
            actor = await self.scope["current_outreach_actor"]({"org_id": "org", "actor_id": "admin"})
            self.assertEqual(actor.role, "org:admin")

    def dispatch_mocks(self, boundary_wins=True, provider_error=False):
        key = request_key("org", self.row["customer_phone"] + ":12345", "product")
        self.row.update(notification_id=key, notification_status="queued")
        job = {"_id": key, "org_id": "org", "request_id": "request", "actor_id": "admin", "status": "checking", "started_at": datetime.now(timezone.utc), "send_attempted": False, "snapshot": self.scope["outreach_snapshot"](self.row)}
        self.db.restock_outbox.find.return_value.limit.return_value.to_list = AsyncMock(return_value=[])
        self.db.restock_outbox.find_one.return_value = job
        self.db.restock_dispatch.find_one_and_update = AsyncMock(return_value={"_id": "org"})
        self.db.restock_outbox.find_one_and_update = AsyncMock(return_value=job)
        if not boundary_wins:
            self.db.restock_outbox.update_one.return_value.matched_count = 0
        self.scope["current_outreach_actor"] = AsyncMock(return_value=self.user)
        self.scope["product_for"] = AsyncMock(return_value={"stock": 1, "updated_at": datetime.now(timezone.utc)})
        self.scope["connection_for"] = AsyncMock(return_value={"wa_id": "waba", "access_token": "test"})
        wa = types.ModuleType("services.wa_service")
        wa.get_templates = AsyncMock(return_value={"templates": []})
        wa.get_broadcast_suppressed_phone_keys = Mock(return_value=set())
        wa.send_whatsapp_template_message = AsyncMock(return_value={"message_id": "message"})
        if provider_error:
            wa.send_whatsapp_template_message.side_effect = TimeoutError("ambiguous provider timeout")
        quota = types.ModuleType("services.entitlement_service")
        quota.require_feature = Mock()
        quota.consume_quota_async = AsyncMock()
        quota.release_quota_async = AsyncMock()
        return {"services.wa_service": wa, "services.entitlement_service": quota}, wa

    async def test_cancellation_wins_provider_boundary(self):
        modules, wa = self.dispatch_mocks(boundary_wins=False)
        with patch.dict("sys.modules", modules):
            await self.scope["process_restock_outbox"]()
        wa.send_whatsapp_template_message.assert_not_awaited()
        claim_update = self.db.restock_outbox.find_one_and_update.call_args.args[1]["$set"]
        self.assertTrue(claim_update["quota_release_pending"])
        self.assertTrue(claim_update["quota_event_key"].startswith("restock:" + self.row["notification_id"] + ":"))
        modules["services.entitlement_service"].release_quota_async.assert_awaited_once_with(
            "org", "monthly_recipients", claim_update["quota_event_key"])

    async def test_timeout_is_unknown_and_persisted_as_attempted(self):
        modules, wa = self.dispatch_mocks(provider_error=True)
        with patch.dict("sys.modules", modules):
            await self.scope["process_restock_outbox"]()
        wa.send_whatsapp_template_message.assert_awaited_once()
        modules["services.entitlement_service"].release_quota_async.assert_not_awaited()
        updates = [call.args[1]["$set"] for call in self.db.restock_outbox.update_one.call_args_list]
        self.assertTrue(any(update.get("send_attempted") is True for update in updates))
        self.assertEqual(updates[-1]["status"], "unknown")

    async def test_retry_query_excludes_unknown_and_provider_attempts(self):
        modules, _ = self.dispatch_mocks()
        self.db.restock_outbox.find_one_and_update.return_value = None
        with patch.dict("sys.modules", modules), self.assertRaises(HTTPException):
            await self.scope["retry_restock"](self.user, "request")
        match = self.db.restock_outbox.find_one_and_update.call_args.args[0]
        self.assertEqual(set(match["status"]["$in"]), {"blocked", "cancelled"})
        self.assertIs(match["send_attempted"], False)
        self.assertEqual(match["quota_release_pending"], {"$ne": True})

    async def test_delayed_quota_cleanup_is_fenced_to_old_attempt(self):
        modules, wa = self.dispatch_mocks()
        old = {"_id": self.row["notification_id"], "org_id": "org", "status": "cancelled",
               "send_attempted": False, "started_at": datetime.now(timezone.utc),
               "quota_event_key": "restock:old-attempt", "quota_release_pending": True}
        self.db.restock_outbox.find.return_value.limit.return_value.to_list.side_effect = [[], [old]]
        self.db.restock_outbox.find_one.return_value = None
        with patch.dict("sys.modules", modules):
            await self.scope["process_restock_outbox"]()
        modules["services.entitlement_service"].release_quota_async.assert_awaited_once_with(
            "org", "monthly_recipients", "restock:old-attempt")
        match = self.db.restock_outbox.update_one.call_args.args[0]
        self.assertEqual(match["quota_event_key"], "restock:old-attempt")
        self.assertEqual(match["started_at"], old["started_at"])
        self.assertIs(match["send_attempted"], False)
        self.assertEqual(match["status"]["$in"], ["blocked", "cancelled"])
        wa.send_whatsapp_template_message.assert_not_awaited()

    async def test_explicit_blocked_retry_requeues_without_sending(self):
        modules, wa = self.dispatch_mocks()
        self.row["notification_status"] = "blocked"
        with patch.dict("sys.modules", modules):
            result = await self.scope["retry_restock"](self.user, "request")
        self.assertEqual(result["queued"], 1)
        wa.send_whatsapp_template_message.assert_not_awaited()
        self.assertEqual(self.db.restock_outbox.update_one.call_args.args[1]["$set"]["status"], "queued")

    async def test_corrected_product_retry_uses_new_deduplication_key(self):
        modules, wa = self.dispatch_mocks()
        self.row.update(product_id="corrected-product", notification_status="blocked")
        self.db.restock_outbox.insert_one = AsyncMock()
        with patch.dict("sys.modules", modules):
            result = await self.scope["retry_restock"](self.user, "request")
        self.assertEqual(result["queued"], 1)
        inserted = self.db.restock_outbox.insert_one.call_args.args[0]
        self.assertEqual(inserted["_id"], request_key("org", self.row["customer_phone"] + ":12345", "corrected-product"))
        wa.send_whatsapp_template_message.assert_not_awaited()

    def test_stock_must_have_fresh_trustworthy_timestamp(self):
        stamp = datetime.now(timezone.utc)
        for updated in (None, "yesterday", stamp - timedelta(hours=25), stamp + timedelta(hours=1)):
            self.assertIn("stale", eligibility_reason(self.row, {"stock": 1, "updated_at": updated}, stamp))
        self.assertEqual(eligibility_reason(self.row, {"stock": 1, "updated_at": stamp.astimezone(timezone(timedelta(hours=5, minutes=30)))}, stamp), "")


if __name__ == "__main__":
    unittest.main()
