import asyncio
import copy
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _assert_bson_safe(value):
    if isinstance(value, Decimal):
        raise TypeError("Decimal is not BSON encodable without a custom codec")
    if isinstance(value, dict):
        for nested in value.values():
            _assert_bson_safe(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_bson_safe(nested)


def _matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, option) for option in expected):
                return False
            continue
        actual_exists = key in document
        actual = document.get(key)
        if isinstance(expected, dict):
            for operator, value in expected.items():
                if operator == "$exists" and actual_exists is not bool(value):
                    return False
                if operator == "$gt" and (not actual_exists or not actual > value):
                    return False
                if operator == "$in" and actual not in value:
                    return False
            continue
        if not actual_exists or actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, documents):
        self.documents = documents

    def limit(self, length):
        self.documents = self.documents[:length]
        return self

    async def to_list(self, length):
        return copy.deepcopy(self.documents[:length])


class _Result:
    def __init__(self, matched_count=0):
        self.matched_count = matched_count
        self.modified_count = matched_count


class _Collection:
    def __init__(self, documents=None):
        self.documents = [copy.deepcopy(item) for item in (documents or [])]

    async def find_one(self, query, *_args, **_kwargs):
        return next(
            (copy.deepcopy(item) for item in self.documents if _matches(item, query)),
            None,
        )

    def find(self, query):
        return _Cursor([item for item in self.documents if _matches(item, query)])

    async def insert_one(self, document):
        # Match the real Motor/PyMongo trust boundary, including BSON types.
        _assert_bson_safe(document)
        self.documents.append(copy.deepcopy(document))
        return types.SimpleNamespace(inserted_id=document.get("_id"))

    async def update_one(self, query, update, upsert=False):
        for item in self.documents:
            if not _matches(item, query):
                continue
            for key, value in update.get("$set", {}).items():
                item[key] = copy.deepcopy(value)
            for key in update.get("$unset", {}):
                item.pop(key, None)
            return _Result(1)
        if upsert:
            document = {key: value for key, value in query.items() if not key.startswith("$")}
            document.update(copy.deepcopy(update.get("$setOnInsert", {})))
            document.update(copy.deepcopy(update.get("$set", {})))
            self.documents.append(document)
            return _Result(1)
        return _Result(0)


class _Database:
    def __init__(self):
        self.collections = {
            "pricing_settings": _Collection([{
                "_id": "org-1",
                "org_id": "org-1",
                "mode": "review",
                "shadow_started_at": datetime.now(timezone.utc) - timedelta(days=8),
            }]),
            "organizations": _Collection([]),
        }

    def __getitem__(self, name):
        return self.collections.setdefault(name, _Collection())

    def __getattr__(self, name):
        return self[name]


def _load_service():
    saved = {name: sys.modules.get(name) for name in ("config.settings", "database")}
    settings = types.ModuleType("config.settings")
    settings.PRICING_FLOOR_MAX_AGE_MINUTES = 60
    settings.PRICING_INTELLIGENCE_ORG_IDS = {"org-1"}
    database = types.ModuleType("database")
    database.get_async_mongo_db = lambda: None
    sys.modules.update({"config.settings": settings, "database": database})
    try:
        spec = importlib.util.spec_from_file_location(
            "_pricing_decision_service_test", ROOT / "services/pricing_decision_service.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


MODULE = _load_service()


def _product(**overrides):
    now = datetime.now(timezone.utc)
    product = {
        "unique_identifier": "SKU-1",
        "approved_floor_price": 100,
        "target_selling_price": 120,
        "mrp": 150,
        "currency": "INR",
        "gst_rate": 18,
        "selling_gst_inclusive": True,
        "stock": 5,
        "price_source_sheet_id": "sheet-primary",
        "price_verified_at": now,
        "price_version": "v1",
    }
    product.update(overrides)
    return product


def _offer(unit_price=90):
    return {"offer": {"items": [{
        "description": "Test GPU",
        "sku": "SKU-1",
        "quantity": 1,
        "unit_price": unit_price,
        "gst_rate": 18,
        "gst_inclusive": True,
    }]}}


class PricingDecisionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = _Database()
        self.db["products_org-1"].documents.append(_product())
        MODULE.get_async_mongo_db = lambda: self.db

    async def _require(self, context, decision_id=None):
        offer = MODULE.structured_offer_snapshot(context)
        return await MODULE.require_human_pricing_acknowledgement(
            org_id="org-1",
            user_id="user-1",
            conversation_id="conversation-1",
            content=f"Price offer. Total: ₹{offer['total_amount']:,.2f}",
            context=context,
            temp_id="temp-1",
            pricing_decision_id=decision_id,
        )

    async def test_below_floor_requires_exact_acknowledgement(self):
        first = await self._require(_offer(90))
        self.assertTrue(first["requires_acknowledgement"])
        self.assertEqual(first["temp_id"], "temp-1")
        self.assertFalse(first.get("acknowledged", False))
        self.assertIn("quote_below_floor", first["warnings"])
        self.assertGreater(first["expires_at"] - first["ack_expires_at"], timedelta(days=29))

        acknowledged = await self._require(_offer(90), first["_id"])
        self.assertTrue(acknowledged["acknowledged"])
        stored = self.db["pricing_decisions_org-1"].documents[0]
        self.assertIn("acknowledged_at", stored)
        self.assertIn("sending_at", stored)

    async def test_stale_or_version_change_invalidates_existing_token(self):
        first = await self._require(_offer(90))
        product = self.db["products_org-1"].documents[0]
        product["price_version"] = "v2"

        version_changed = await self._require(_offer(90), first["_id"])
        self.assertFalse(version_changed.get("acknowledged", False))
        self.assertNotEqual(version_changed["_id"], first["_id"])

        product["price_verified_at"] = datetime.now(timezone.utc) - timedelta(hours=2)
        stale = await self._require(_offer(90), version_changed["_id"])
        self.assertFalse(stale.get("acknowledged", False))
        self.assertNotEqual(stale["_id"], version_changed["_id"])
        self.assertIn("stale_or_future_price", stale["warnings"])

    async def test_shadow_mode_records_warning_without_blocking(self):
        self.db.pricing_settings.documents[0]["mode"] = "shadow"
        decision = await self._require(_offer(90))
        self.assertFalse(decision["requires_acknowledgement"])
        self.assertFalse(decision.get("acknowledged", False))
        self.assertIn("quote_below_floor", decision["warnings"])
        self.assertEqual(len(self.db["pricing_decisions_org-1"].documents), 1)
        self.assertNotIn("sending_at", self.db["pricing_decisions_org-1"].documents[0])

    async def test_formal_quote_discount_is_resolved_and_warned(self):
        self.db["quotes_org-1"].documents.append({
            "org_id": "org-1",
            "conversation_id": "conversation-1",
            "quote_id": "quote-1",
            "revision_number": 1,
            "discount": 10,
            "items": [{
                "description": "Test GPU",
                "sku": "SKU-1",
                "quantity": 1,
                "private_unit_price": 120,
                "gst_rate": 18,
                "gst_inclusive": True,
            }],
        })
        decision = await MODULE.evaluate_human_pricing(
            org_id="org-1",
            user_id="user-1",
            conversation_id="conversation-1",
            content="Formal quote attached",
            context={"quote": {"id": "quote-1", "revision": 1}},
            persist=False,
        )
        self.assertEqual(decision["source"], "formal_quote")
        self.assertEqual(decision["price_versions"], ["v1"])
        self.assertEqual(decision["warnings"], ["quote_discount_requires_review"])
        _assert_bson_safe(decision)

    async def test_token_claim_and_sent_state_prevent_replay(self):
        self.db.pricing_settings.documents[0]["mode"] = "shadow"
        shadow = await self._require(_offer(90))
        self.db.pricing_settings.documents[0]["mode"] = "review"
        first = await self._require(_offer(90), shadow["_id"])
        self.assertFalse(first.get("acknowledged", False))
        self.assertNotEqual(first["_id"], shadow["_id"])

        left, right = await asyncio.gather(
            self._require(_offer(90), first["_id"]),
            self._require(_offer(90), first["_id"]),
        )
        acknowledged = [item for item in (left, right) if item.get("acknowledged")]
        rejected = [item for item in (left, right) if not item.get("acknowledged")]
        self.assertEqual(len(acknowledged), 1)
        self.assertEqual(len(rejected), 1)

        await MODULE.mark_pricing_decision_sent("org-1", first["_id"], "wamid-1")
        replay = await self._require(_offer(90), first["_id"])
        self.assertFalse(replay.get("acknowledged", False))
        self.assertNotEqual(replay["_id"], first["_id"])
        sent = next(
            item for item in self.db["pricing_decisions_org-1"].documents
            if item["_id"] == first["_id"]
        )
        self.assertEqual(sent["provider_message_id"], "wamid-1")
        self.assertIn("sent_at", sent)
        self.assertNotIn("sending_at", sent)

    async def test_acknowledgement_is_bound_to_the_original_send_operation(self):
        first = await self._require(_offer(90))
        changed_operation = await MODULE.require_human_pricing_acknowledgement(
            org_id="org-1",
            user_id="user-1",
            conversation_id="conversation-1",
            content="Price offer. Total: ₹90.00",
            context=_offer(90),
            temp_id="temp-2",
            pricing_decision_id=first["_id"],
        )
        self.assertFalse(changed_operation.get("acknowledged", False))
        self.assertNotEqual(changed_operation["_id"], first["_id"])

    async def test_structured_offer_fingerprint_uses_server_rendered_content(self):
        arbitrary = await MODULE.evaluate_human_pricing(
            org_id="org-1", user_id="user-1", conversation_id="conversation-1",
            content="A ₹200, B ₹100, or an unprefixed 50", context=_offer(90),
            temp_id="temp-1", persist=False,
        )
        canonical = await MODULE.evaluate_human_pricing(
            org_id="org-1", user_id="user-1", conversation_id="conversation-1",
            content="anything else", context=_offer(90), temp_id="temp-1", persist=False,
        )
        self.assertEqual(arbitrary["fingerprint"], canonical["fingerprint"])

    async def test_review_requires_seven_continuous_shadow_days(self):
        await MODULE.update_pricing_settings("org-1", "off", "admin-1")
        shadow = await MODULE.update_pricing_settings("org-1", "shadow", "admin-1")
        self.assertGreater(shadow["shadow_started_at"], datetime.now(timezone.utc) - timedelta(seconds=2))
        with self.assertRaisesRegex(ValueError, "seven completed days"):
            await MODULE.update_pricing_settings("org-1", "review", "admin-1")

    async def test_allowlisted_org_starts_a_persisted_shadow_period(self):
        self.db.pricing_settings.documents.clear()
        settings = await MODULE.get_pricing_settings("org-1")
        self.assertEqual(settings["mode"], "shadow")
        self.assertIsInstance(settings["shadow_started_at"], datetime)
        self.assertEqual(self.db.pricing_settings.documents[0]["updated_by"], "pricing_rollout")


if __name__ == "__main__":
    unittest.main()
