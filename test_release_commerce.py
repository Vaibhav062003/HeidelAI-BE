"""Offline regressions against production functions; no credentials or providers."""
import ast
import importlib.util
import re
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent


def functions(path, names, **namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [node for node in tree.body if getattr(node, "name", None) in names or
             isinstance(node, ast.Assign) and any(getattr(t, "id", None) in names for t in node.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)
    return types.SimpleNamespace(**namespace)


spec = importlib.util.spec_from_file_location("shopify_helpers", ROOT / "tests/shopify/test_shopify_worker_runtime.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


class CommerceRelease(unittest.IsolatedAsyncioTestCase):
    def oos(self, **values):
        return functions("services/oos_service.py", {
            "_format_brand", "CONVERSATIONAL_PREFIXES", "BUDGET_SUFFIXES", "clean_product_query",
            "_format_product_title", "format_dealer_sourcing_message", "handle_dealer_reply",
            "_is_pending_dealer_acknowledgement",
        }, re=re, Optional=Optional, datetime=datetime, timezone=timezone, logger=Mock(), **values)

    def test_variant_preserved_without_budget(self):
        output = self.oos().format_dealer_sourcing_message({"brand": "Lenovo", "model_name": "LOQ", "category": "laptop",
            "specs": {"cpu": "i7", "ram": "16GB", "storage": "512GB SSD", "gpu": "RTX 5060", "budget_max": 90000}})
        for value in ("i7", "16GB", "512GB SSD", "RTX 5060"):
            self.assertIn(value, output)
        self.assertNotIn("90000", output)

    def test_variant_not_duplicated(self):
        output = self.oos().format_dealer_sourcing_message({"brand": "Apple", "model_name": "iPhone 16 128GB", "specs": {"storage": "128GB"}})
        self.assertEqual(output.count("128GB"), 1)

    async def test_pending_ack_requires_exact_reply_and_sender(self):
        doc = {"query_id": "q", "status": "dealers_contacted", "sent_dealers": [{"dealer_phone": "9199", "message_ids": ["out"]}]}
        collection = Mock(find_one=AsyncMock(return_value=doc), update_one=AsyncMock(return_value=types.SimpleNamespace(modified_count=1)))
        complete = AsyncMock()
        module = self.oos(get_async_mongo_db=lambda: {"oos_requests": collection}, _complete_dealer_reply_steps=complete)
        with patch.dict(sys.modules, {"services.pricing_service": types.SimpleNamespace(is_supplier_response=lambda text: False)}):
            self.assertTrue(await module.handle_dealer_reply("org", "9199", "Let me check and confirm tomorrow", {"id": "out"}, source_message_id="in"))
            update = collection.update_one.call_args.args[1]
            reply = update["$push"]["dealer_replies"]
            self.assertEqual(reply["reply_kind"], "pending_acknowledgement")
            self.assertEqual(reply["steps"], {"pricing": "done", "sheet": "pending", "escalation": "done"})
            self.assertNotIn("$set", update)
            for phone, content, context in [("other", "Let me check and confirm tomorrow", {"id": "out"}),
                ("9199", "Can you check and confirm tomorrow?", {"id": "out"}),
                ("9199", "Let me check and confirm tomorrow", None)]:
                self.assertFalse(await module.handle_dealer_reply("org", phone, content, context, source_message_id="other"))
        self.assertEqual(collection.update_one.await_count, 1)

    def test_sheet_automation_selected_secondary_account(self):
        resolver = Mock(side_effect=lambda org, wa, phone: {"wa_id": "secondary", "phone_id": phone} if wa is None else None)
        module = functions("services/google_sheet_automation.py", {"_get_wa_connection"}, Optional=Optional,
            db=types.SimpleNamespace(organizations=Mock(find_one=Mock(return_value={"wa_id": "primary"}))))
        with patch.dict(sys.modules, {"services.wa_service": types.SimpleNamespace(resolve_outbound_whatsapp_connection=resolver)}):
            self.assertEqual(module._get_wa_connection("org", "line-B"), {"wa_id": "secondary", "phone_id": "line-B"})

    async def test_shopify_uses_canonical_id_and_rejects_ambiguity(self):
        # Existing worker harness loads the full service with fake provider modules.
        with patch.dict(sys.modules):
            service = helpers.load_service()
        db = helpers.FakeDB()
        service.db = db
        db.shopify_checkouts.documents = [{"org_id": "org", "provider_id": "1", "phone": "+919876543210"}]
        conversations = db["conversations_org"].documents
        conversations.append({"platform": "whatsapp", "customer_id": "919876543210", "business_phone_id": "line-A", "conversation_id": "whatsapp_line-A_919876543210"})
        async def link():
            return (await service.list_records("org", "checkout"))["items"][0]["conversation_id"]
        self.assertEqual(await link(), "whatsapp_line-A_919876543210")
        conversations.append({"platform": "whatsapp", "customer_id": "919876543210", "business_phone_id": "line-B", "conversation_id": "whatsapp_line-B_919876543210"})
        self.assertIsNone(await link())
        db.shopify_checkouts.documents[0]["send_phone_id"] = "line-B"
        self.assertEqual(await link(), "whatsapp_line-B_919876543210")
        db.shopify_checkouts.documents[0]["send_phone_id"] = "missing"
        self.assertIsNone(await link())
        db.shopify_checkouts.documents[0].pop("send_phone_id")
        db.shopify_connections.documents = [{"org_id": "org", "whatsapp_phone_id": "line-A"}]
        self.assertEqual(await link(), "whatsapp_line-A_919876543210")
        db.shopify_orders.documents = [{"org_id": "org", "provider_id": "order", "phone": "+919876543210"}]
        db.shopify_order_notifications.documents = [{"org_id": "org", "order_provider_id": "order", "send_phone_id": "line-B", "provider_started_at": datetime.now(timezone.utc)}]
        self.assertEqual((await service.list_records("org", "order"))["items"][0]["conversation_id"], "whatsapp_line-B_919876543210")
        # Unbound legacy threads are still usable, but never borrow them for a secondary line.
        db.shopify_connections.documents = []
        conversations[:] = [{"platform": "whatsapp", "customer_id": "919876543210", "conversation_id": "whatsapp_919876543210"}]
        self.assertEqual(await link(), "whatsapp_919876543210")
        db.shopify_checkouts.documents[0]["send_phone_id"] = "line-B"
        self.assertIsNone(await link())
        db.whatsapp_connections.documents = [{"_id": "primary", "org_id": "org", "phone_id": "line-A", "slot": 1}]
        db.shopify_checkouts.documents[0]["send_phone_id"] = "line-A"
        self.assertEqual(await link(), "whatsapp_919876543210")
        db.shopify_checkouts.documents[0]["org_id"] = "other-org"
        self.assertEqual((await service.list_records("org", "checkout"))["items"], [])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CommerceRelease))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("RELEASE_COMMERCE_OK")
