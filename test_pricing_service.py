import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch


for key in (
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_PROJECT",
    "AZURE_OPENAI_SUBSCRIPTION_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_DEPLOYMENT",
):
    os.environ.setdefault(key, "test")

logger = types.SimpleNamespace(
    warning=lambda *_args, **_kwargs: None,
    error=lambda *_args, **_kwargs: None,
    info=lambda *_args, **_kwargs: None,
    success=lambda *_args, **_kwargs: None,
)
loguru = types.ModuleType("loguru")
loguru.logger = logger
settings = types.ModuleType("config.settings")
settings.KOMAL_ORG_ID = "komal"
settings.LEE_DEALER_ORG_ID = "lee"
settings.PRICING_FLOOR_MAX_AGE_MINUTES = 60
settings.PRICING_INTELLIGENCE_ORG_IDS = {"komal", "lee"}
database = types.ModuleType("database")
database.get_async_mongo_db = lambda: None
database.get_mongo_db = lambda: None
utils = types.ModuleType("services.utils")
utils.normalize_phone = lambda value: "".join(character for character in str(value) if character.isdigit())
utils.serialize_mongo = lambda value: value.isoformat() if isinstance(value, datetime) else value
core_services = types.ModuleType("core.services")
core_services.services = types.SimpleNamespace(agent_clients={})
fastapi = types.ModuleType("fastapi")
fastapi.HTTPException = RuntimeError
httpx = types.ModuleType("httpx")
sys.modules.setdefault("loguru", loguru)
sys.modules.setdefault("config.settings", settings)
sys.modules.setdefault("database", database)
sys.modules.setdefault("services.utils", utils)
sys.modules.setdefault("core.services", core_services)
sys.modules.setdefault("fastapi", fastapi)
sys.modules.setdefault("httpx", httpx)

from services.pricing_service import (
    _deliver_pricing_escalation,
    _escalate,
    _looks_like_claim,
    _process_accepted_ai_audit,
    _record_accepted_ai_response,
    _reconcile_ai_pricing_delivery,
    _resolve_product,
    extract_prices,
    extract_quoted_prices,
    ensure_pricing_indexes,
    floor_snapshot,
    is_pricing_enabled,
    is_price_signal,
    is_supplier_response,
    quote_learning,
    record_accepted_ai_response,
    record_oos_supplier_reply,
    observe_agent_message,
    observe_customer_message,
    update_ai_pricing_delivery,
    validate_ai_pricing_response,
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, _length):
        return self

    def sort(self, *_args):
        return self

    async def to_list(self, length):
        return self.rows[:length]


class PricingPolicyTest(unittest.TestCase):
    def test_floor_and_price_boundaries(self):
        now = datetime.now(timezone.utc)
        product = {
            "approved_floor_price": "₹70,000",
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
        }
        self.assertEqual(floor_snapshot(product, now), (70_000.0, "current_sheet_floor"))
        for invalid in (float("nan"), float("inf"), "NaN", "Infinity"):
            self.assertEqual(floor_snapshot({**product, "approved_floor_price": invalid}, now)[1], "missing_floor")
        self.assertEqual(
            floor_snapshot({**product, "price_verified_at": now - timedelta(minutes=61)}, now)[1],
            "stale_floor",
        )
        self.assertEqual(
            floor_snapshot({"price": 70_000, "source": "google_sheet", "updated_at": now}, now)[1],
            "missing_floor",
        )
        for text in (
            "RTX 5060 8GB",
            "My PIN is 411001",
            "Payment of 45000 sent",
            "Yes RTX 5060 available",
            "Getting RTX 5060 8GB",
        ):
            self.assertEqual(extract_prices(text, established_context=True), [], text)
        self.assertFalse(is_price_signal("5060"))
        self.assertEqual(extract_prices("Already getting at Rs. 70,250"), [70_250.0])
        self.assertEqual(extract_prices("getting 70250"), [70_250.0])
        self.assertEqual(extract_prices("got 70250"), [70_250.0])
        self.assertEqual(extract_prices("offered 70250"), [70_250.0])
        self.assertEqual(extract_prices("price 70k"), [70_000.0])
        self.assertEqual(extract_prices("price 1.2 lakh"), [120_000.0])
        self.assertEqual(extract_prices("₹999"), [999.0])
        self.assertEqual(extract_prices("70250", established_context=True), [70_250.0])
        self.assertEqual(extract_prices("50-55k", established_context=True), [50_000.0, 55_000.0])
        self.assertEqual(extract_quoted_prices("You can have it at 70,500 all inclusive"), [70_500.0])
        self.assertEqual(extract_quoted_prices("70k all inclusive"), [70_000.0])
        self.assertEqual(extract_quoted_prices("Available for 70,500"), [70_500.0])
        self.assertEqual(extract_quoted_prices("Our price for RTX 5060 is 45,000"), [45_000.0])
        self.assertEqual(extract_quoted_prices("RTX 5060: 45,000"), [45_000.0])
        self.assertEqual(extract_quoted_prices("RTX 5060 - 45000"), [45_000.0])
        self.assertEqual(extract_quoted_prices("45000"), [45_000.0])
        self.assertEqual(extract_quoted_prices("Price range 70k-72k"), [70_000.0, 72_000.0])
        self.assertEqual(extract_quoted_prices("Price range 50-55k"), [50_000.0, 55_000.0])
        self.assertEqual(extract_quoted_prices("We are located at 411001"), [])
        self.assertEqual(extract_quoted_prices("Payment of ₹45,000 received"), [])
        self.assertEqual(extract_quoted_prices("Discount ₹500 and final price ₹45,000"), [45_000.0])
        self.assertTrue(_looks_like_claim("Already getting at 70,250", [70_250.0], True))
        self.assertTrue(is_supplier_response("Yes, available at 70,250"))
        self.assertTrue(is_supplier_response("70250"))
        self.assertFalse(is_supplier_response("What is the price for ASUS FA608PP?"))
        self.assertEqual(quote_learning(70_000, 70_500)[3], "valid_human_markup")
        self.assertEqual(quote_learning(70_000, 69_500)[3], "below_floor")
        self.assertTrue(is_pricing_enabled("lee"))
        self.assertFalse(is_pricing_enabled("not-allowlisted"))


class PricingRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        b2b_mode = patch("services.b2b_pricing_service.enabled", new=AsyncMock(return_value=False))
        b2b_mode.start()
        self.addCleanup(b2b_mode.stop)
        self.pricing_mode = patch(
            "services.pricing_service.get_pricing_mode",
            new=AsyncMock(return_value="review"),
        )
        self.pricing_mode.start()
        self.addCleanup(self.pricing_mode.stop)

    async def test_direct_ai_guard_and_receipt_promotion(self):
        now = datetime.now(timezone.utc)
        product = {
            "model_no": "SKU-1",
            "approved_floor_price": 70_000,
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
            "selling_gst_inclusive": True,
        }
        record = AsyncMock(return_value=True)
        queue = AsyncMock()
        with (
            patch("services.pricing_service.is_pricing_enabled", return_value=True),
            patch("services.pricing_service._event_for_message", new=AsyncMock(return_value={"sku": "SKU-1"})),
            patch("services.pricing_service._message_for_id", new=AsyncMock(return_value=None)),
            patch("services.pricing_service._resolve_product", new=AsyncMock(return_value=product)),
            patch("services.pricing_service._record", new=record),
            patch("services.pricing_service._queue_pricing_escalation", new=queue),
            patch("services.pricing_service._reconcile_ai_pricing_delivery", new=AsyncMock()),
        ):
            self.assertEqual(
                await validate_ai_pricing_response(
                    org_id="lee",
                    conversation_id="whatsapp_919999999999",
                    source_message_id=None,
                    content="You can have SKU-1 at 70,500 all inclusive",
                ),
                (True, "validated_ai_prices"),
            )
            valid, reason = await validate_ai_pricing_response(
                org_id="lee",
                conversation_id="whatsapp_919999999999",
                source_message_id=None,
                content="Price SKU-1: Rs. 69,000",
            )
            self.assertFalse(valid)
            self.assertEqual(reason, "below_floor_ai_price")
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="Price SKU-1: ₹0",
                    )
                )[1],
                "invalid_ai_price",
            )
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="Price SKU-1: ₹10,000,000",
                    )
                )[1],
                "invalid_ai_price",
            )
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="RTX 5060: ₹70,500\nRyzen 9700X: ₹80,000",
                    )
                )[1],
                "ambiguous_multiple_product_prices",
            )
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="Ryzen 7 9700X and RTX 5060 build total ₹90,000",
                    )
                )[1],
                "ambiguous_multiple_products",
            )
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="2 units SKU-1 total ₹140,000",
                    )
                )[1],
                "structured_quantity_required",
            )
            for aggregate in (
                "2 RTX 5060 total ₹45,000",
                "RTX 5060 x2 total ₹45,000",
                "two units RTX 5060 total ₹45,000",
                "2 ASUS FA608PP total ₹45,000",
            ):
                self.assertEqual(
                    (
                        await validate_ai_pricing_response(
                            org_id="lee",
                            conversation_id="whatsapp_919999999999",
                            source_message_id=None,
                            content=aggregate,
                        )
                    )[1],
                    "structured_quantity_required",
                    aggregate,
                )
            for bundle in (
                "RTX 5060 and Samsung 1TB SSD build total ₹70,000",
                "SKU-1 and SKU-2 bundle total ₹70,000",
            ):
                self.assertEqual(
                    (
                        await validate_ai_pricing_response(
                            org_id="lee",
                            conversation_id="whatsapp_919999999999",
                            source_message_id=None,
                            content=bundle,
                        )
                    )[1],
                    "ambiguous_multiple_products",
                    bundle,
                )
            self.assertEqual(
                (
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content="Price SKU-1 ₹70,500 GST extra",
                    )
                )[1],
                "tax_basis_mismatch",
            )
            self.assertEqual(
                await validate_ai_pricing_response(
                    org_id="lee",
                    conversation_id="whatsapp_919999999999",
                    source_message_id=None,
                    content="Your budget is ₹50,000, correct?",
                ),
                (True, "no_outbound_price"),
            )
            for contextual_quote in (
                "Hi Rahul, you can have it at ₹70,500 all inclusive",
                "You can have it at ₹70,500 all inclusive with warranty",
                "Price is ₹70,500 all inclusive, delivery in 3 working days",
                "Price is ₹70,500 all inclusive, valid until 30 August",
            ):
                self.assertEqual(
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id=None,
                        content=contextual_quote,
                    ),
                    (True, "validated_ai_prices"),
                    contextual_quote,
                )
            gst_extra_product = {**product, "selling_gst_inclusive": False}
            with patch(
                "services.pricing_service._resolve_product",
                new=AsyncMock(return_value=gst_extra_product),
            ):
                self.assertEqual(
                    (
                        await validate_ai_pricing_response(
                            org_id="lee",
                            conversation_id="whatsapp_919999999999",
                            source_message_id=None,
                            content="Final price RTX 5060 ₹70,000",
                        )
                    )[1],
                    "tax_basis_mismatch",
                )
            queue.assert_awaited()

            await _record_accepted_ai_response(
                org_id="lee",
                conversation_id="whatsapp_919999999999",
                inbound_message_id=None,
                outbound_message_id="out-1",
                content="Price SKU-1: Rs. 70,500",
            )

        accepted = next(
            call.args[1]
            for call in record.await_args_list
            if call.args[1].get("event_type") == "ai_customer_quote"
        )
        self.assertEqual(accepted["delivery_status"], "provider_accepted")
        self.assertNotIn("delivered_at", accepted)

        class EventCollection:
            def __init__(self):
                self.updates = []
                self.rows = []

            def find(self, *_args, **_kwargs):
                return Cursor(self.rows)

            async def update_one(self, _query, update):
                self.updates.append(update["$set"])

        class ReceiptCollection:
            def __init__(self):
                self.receipt = None

            async def update_one(self, query, _update, upsert=False):
                if isinstance(_update, list):
                    self.receipt = {"_id": query["_id"], "status": "delivered", "rank": 2}
                elif self.receipt:
                    self.receipt.update(_update.get("$set", {}))

            async def find_one(self, query):
                return self.receipt if self.receipt and self.receipt["_id"] == query["_id"] else None

            async def delete_one(self, _query):
                self.receipt = None

        events = EventCollection()
        receipts = ReceiptCollection()
        class DB:
            def __getitem__(self, name):
                return receipts if name.startswith("pricing_delivery_") else events

        with (
            patch("services.pricing_service.is_pricing_enabled", return_value=True),
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
        ):
            await update_ai_pricing_delivery("lee", "out-1", "delivered")
            self.assertIsNotNone(receipts.receipt)
            events.rows.append({"event_key": "event-1", "delivery_status": "provider_accepted"})
            await _reconcile_ai_pricing_delivery("lee", "out-1")
        self.assertEqual(events.updates[-1]["delivery_status"], "delivered")
        self.assertIn("delivered_at", events.updates[-1])
        self.assertIsNotNone(receipts.receipt)
        self.assertIn("reconciled_at", receipts.receipt)

    async def test_real_model_resolution_keeps_model_number(self):
        product = {"model_no": "RTX-5060", "product_name": "NVIDIA RTX 5060"}

        class ProductCollection:
            def __init__(self):
                self.query = None

            def find(self, query):
                self.query = query
                return Cursor([product])

        products = ProductCollection()
        class DB:
            def __getitem__(self, _name):
                return products

        with patch("services.pricing_service.get_async_mongo_db", return_value=DB()):
            resolved = await _resolve_product("lee", "What is price for RTX 5060?")
            resolved_with_price = await _resolve_product("lee", "RTX 5060: 45,000")
        self.assertEqual(resolved, product)
        self.assertEqual(resolved_with_price, product)
        query_text = str(products.query).lower()
        self.assertIn("rtx", query_text)
        self.assertIn("5060", query_text)
        self.assertNotIn("'for'", query_text)
        self.assertNotIn("'45'", query_text)
        self.assertNotIn("'000'", query_text)

    async def test_pricing_decision_indexes_cover_ttl_and_conversation_history(self):
        collections = {}

        class Collection:
            def __init__(self):
                self.indexes = []

            async def create_index(self, keys, **kwargs):
                self.indexes.append((keys, kwargs))

        class DB:
            def __getitem__(self, name):
                return collections.setdefault(name, Collection())

        with patch("services.pricing_service.get_async_mongo_db", return_value=DB()):
            await ensure_pricing_indexes()

        for org_id in ("lee", "komal"):
            indexes = collections[f"pricing_decisions_{org_id}"].indexes
            self.assertIn(("expires_at", {"expireAfterSeconds": 0}), indexes)
            self.assertIn(([('conversation_id', 1), ('created_at', -1)], {}), indexes)

    async def test_explicit_product_cannot_reuse_context_floor(self):
        now = datetime.now(timezone.utc)
        context_product = {
            "_id": "p-5060",
            "model_no": "SKU-5060",
            "product_name": "NVIDIA RTX 5060",
            "approved_floor_price": 45_000,
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
            "selling_gst_inclusive": True,
        }
        explicit_product = {
            "_id": "p-5070",
            "model_no": "SKU-5070",
            "product_name": "NVIDIA RTX 5070",
            "approved_floor_price": 60_000,
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
            "selling_gst_inclusive": True,
        }
        generic_product = {
            "_id": "p-asus",
            "model_no": "FA608PP",
            "product_name": "ASUS FA608PP",
            "approved_floor_price": 40_000,
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
            "selling_gst_inclusive": True,
        }

        class Products:
            async def find_one(self, query):
                return context_product if "SKU-5060" in str(query) else None

            def find(self, query):
                query_text = str(query)
                if "5070" in query_text:
                    return Cursor([explicit_product])
                if "FA608PP" in query_text:
                    return Cursor([generic_product])
                return Cursor([])

        class DB:
            def __getitem__(self, _name):
                return Products()

        with (
            patch("services.pricing_service.is_pricing_enabled", return_value=True),
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
            patch("services.pricing_service._event_for_message", new=AsyncMock(return_value={"sku": "SKU-5060"})),
            patch("services.pricing_service._message_for_id", new=AsyncMock(return_value=None)),
            patch("services.pricing_service._record", new=AsyncMock(return_value=True)),
            patch("services.pricing_service._queue_pricing_escalation", new=AsyncMock()),
        ):
            for content in (
                "Our price for RTX 5070 is ₹50,000 GST included",
                "Our price for ASUS FA608PP is ₹50,000 GST included",
            ):
                valid, reason = await validate_ai_pricing_response(
                    org_id="lee",
                    conversation_id="whatsapp_919999999999",
                    source_message_id="in-5060",
                    content=content,
                )
                self.assertFalse(valid, content)
                self.assertEqual(reason, "product_unresolved", content)
            for content in (
                "Hi Rahul, you can have it at ₹50,000 all inclusive",
                "You can have it at ₹50,000 all inclusive with warranty",
            ):
                self.assertEqual(
                    await validate_ai_pricing_response(
                        org_id="lee",
                        conversation_id="whatsapp_919999999999",
                        source_message_id="in-5060",
                        content=content,
                    ),
                    (True, "validated_ai_prices"),
                    content,
                )

    async def test_accepted_audit_survives_transient_failure(self):
        class Outbox:
            def __init__(self):
                self.doc = None

            async def update_one(self, query, update, upsert=False):
                if "$setOnInsert" in update and self.doc is None:
                    self.doc = {"_id": query["_id"], **update["$setOnInsert"]}
                    return types.SimpleNamespace(modified_count=0, upserted_id=query["_id"])
                if not self.doc or self.doc["_id"] != query.get("_id"):
                    return types.SimpleNamespace(modified_count=0)
                if query.get("claim_id") and query["claim_id"] != self.doc.get("claim_id"):
                    return types.SimpleNamespace(modified_count=0)
                if "$or" in query:
                    due = self.doc.get(
                        "next_attempt_at", datetime.min.replace(tzinfo=timezone.utc)
                    ) <= datetime.now(timezone.utc)
                    stale = self.doc.get("status") == "processing" and self.doc.get(
                        "claimed_at", datetime.max.replace(tzinfo=timezone.utc)
                    ) < datetime.now(timezone.utc) - timedelta(minutes=5)
                    if self.doc.get("status") not in {"pending", "failed"} and not stale:
                        return types.SimpleNamespace(modified_count=0)
                    if not due and not stale:
                        return types.SimpleNamespace(modified_count=0)
                if "$set" in update:
                    self.doc.update(update["$set"])
                for key in update.get("$unset", {}):
                    self.doc.pop(key, None)
                return types.SimpleNamespace(modified_count=1)

            async def find_one(self, query):
                if not self.doc or self.doc["_id"] != query["_id"]:
                    return None
                if query.get("claim_id") and self.doc.get("claim_id") != query["claim_id"]:
                    return None
                return self.doc

            async def delete_one(self, query):
                if self.doc and self.doc["_id"] == query["_id"]:
                    self.doc = None

        outbox = Outbox()
        class DB:
            def __getitem__(self, _name):
                return outbox

        processor = AsyncMock(side_effect=[RuntimeError("temporary"), None])
        with (
            patch("services.pricing_service.is_pricing_enabled", return_value=True),
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
            patch("services.pricing_service._record_accepted_ai_response", new=processor),
        ):
            await record_accepted_ai_response(
                org_id="lee",
                conversation_id="whatsapp_919999999999",
                inbound_message_id="in-1",
                outbound_message_id="out-2",
                content="RTX 5060: ₹70,500",
            )
            self.assertEqual(outbox.doc["status"], "failed")
            outbox.doc["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
            await _process_accepted_ai_audit("lee", "out-2")
        self.assertEqual(outbox.doc["status"], "completed")
        self.assertEqual(processor.await_count, 2)

    async def test_pricing_disabled_does_not_resolve_or_write_interest(self):
        resolver = AsyncMock()
        with (
            patch(
                "services.pricing_service.get_pricing_mode",
                new=AsyncMock(return_value="off"),
            ),
            patch("services.pricing_service._resolve_product", new=resolver),
            patch("services.pricing_service.get_async_mongo_db") as database,
        ):
            result = await observe_customer_message(
                org_id="disabled",
                conversation_id="whatsapp_919999999999",
                content="Price for RTX 5060?",
                source_message_id="in-disabled",
                reply_to_message_id=None,
                customer_name="Buyer",
            )
            await observe_agent_message(
                org_id="disabled",
                conversation_id="whatsapp_919999999999",
                content="Our price is Rs. 70,000",
                source_message_id="out-disabled",
                reply_to_message_id=None,
                sender_name="Agent",
            )
            await record_oos_supplier_reply(
                org_id="disabled",
                dealer_phone="919999999999",
                content="Rs. 69,000",
                source_message_id="oos-disabled",
                oos_doc={},
            )
        self.assertEqual(result, {"intercept": False})
        resolver.assert_not_awaited()
        database.assert_not_called()

    async def test_shadow_audits_ai_and_buyer_risk_without_blocking(self):
        now = datetime.now(timezone.utc)
        product = {
            "model_no": "SKU-1",
            "approved_floor_price": 70_000,
            "price_source_sheet_id": "sheet-1",
            "price_verified_at": now,
            "selling_gst_inclusive": True,
        }
        record = AsyncMock(return_value=True)
        queue = AsyncMock()
        with (
            patch(
                "services.pricing_service.get_pricing_mode",
                new=AsyncMock(return_value="shadow"),
            ),
            patch("services.pricing_service._event_for_message", new=AsyncMock(return_value=None)),
            patch("services.pricing_service._message_for_id", new=AsyncMock(return_value={"role": "customer"})),
            patch("services.pricing_service._last_event", new=AsyncMock(return_value=None)),
            patch("services.pricing_service._active_hold", new=AsyncMock(return_value=None)),
            patch("services.pricing_service._resolve_product", new=AsyncMock(return_value=product)),
            patch("services.pricing_service._record", new=record),
            patch("services.pricing_service._queue_pricing_escalation", new=queue),
        ):
            validation = await validate_ai_pricing_response(
                org_id="lee",
                conversation_id="whatsapp_919999999999",
                source_message_id="in-shadow",
                content="Price SKU-1: Rs. 69,000 GST included",
            )
            observation = await observe_customer_message(
                org_id="lee",
                conversation_id="whatsapp_919999999999",
                content="Can you do SKU-1 for Rs. 69,000?",
                source_message_id="in-shadow",
                reply_to_message_id="previous",
                customer_name="Buyer",
            )
        self.assertEqual(validation, (True, "shadow_below_floor_ai_price"))
        self.assertFalse(observation["intercept"])
        self.assertTrue(observation["shadow_intercept"])
        self.assertEqual(observation["shadow_reason"], "below_floor_claim")
        event_types = [call.args[1]["event_type"] for call in record.await_args_list]
        self.assertIn("ai_output_shadow_warning", event_types)
        self.assertIn("buyer_price_claim", event_types)
        queue.assert_not_awaited()

    async def test_audit_moves_to_dead_after_bounded_failures(self):
        now = datetime.now(timezone.utc)

        class Outbox:
            def __init__(self):
                self.doc = {
                    "_id": "out-dead",
                    "payload": {"outbound_message_id": "out-dead"},
                    "status": "failed",
                    "attempts": 4,
                    "next_attempt_at": now - timedelta(seconds=1),
                }

            async def update_one(self, query, update, **_kwargs):
                if query.get("claim_id") and query["claim_id"] != self.doc.get("claim_id"):
                    return types.SimpleNamespace(modified_count=0)
                self.doc.update(update.get("$set", {}))
                for key in update.get("$unset", {}):
                    self.doc.pop(key, None)
                return types.SimpleNamespace(modified_count=1)

            async def find_one(self, query):
                if query.get("claim_id") and query["claim_id"] != self.doc.get("claim_id"):
                    return None
                return self.doc

        outbox = Outbox()

        class DB:
            def __getitem__(self, _name):
                return outbox

        with (
            patch("services.pricing_service.is_pricing_enabled", return_value=True),
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
            patch(
                "services.pricing_service._record_accepted_ai_response",
                new=AsyncMock(side_effect=RuntimeError("persistent")),
            ),
        ):
            await _process_accepted_ai_audit("lee", "out-dead")
        self.assertEqual(outbox.doc["status"], "dead")
        self.assertEqual(outbox.doc["attempts"], 5)
        self.assertIn("dead_at", outbox.doc)

    async def test_pricing_escalation_has_typed_routing_metadata(self):
        create = AsyncMock(return_value={})
        team = types.ModuleType("services.team_service")
        team.create_ai_escalation = create
        with (
            patch("services.pricing_service._market_summary", new=AsyncMock(return_value={
                "dealer_count": 1, "quote_min": 70_000, "quote_max": 71_000,
            })),
            patch("services.pricing_service._resolve_product", new=AsyncMock(return_value={
                "unique_identifier": "SKU-1",
                "target_selling_price": 72_000,
                "mrp": 75_000,
                "gst_rate": 18,
                "selling_gst_inclusive": True,
                "approved_floor_price": 70_000,
                "price_source_sheet_id": "sheet-1",
                "price_verified_at": datetime.now(timezone.utc),
                "price_version": "v3",
            })),
            patch.dict(sys.modules, {"services.team_service": team}),
        ):
            await _escalate(
                "lee",
                event_key="event-1",
                conversation_id="whatsapp_919999999999",
                channel="whatsapp",
                dealer_phone="919999999999",
                customer_name="Buyer",
                raw_text="Can you do 69000?",
                event_type="buyer_price_claim",
                sku="SKU-1",
                product_context="GPU",
                floor=70_000,
                observed_price=69_000,
                reason="below_floor_claim",
            )
        payload = create.await_args.args[1]
        self.assertEqual(payload["conversation_id"], "whatsapp_919999999999")
        self.assertEqual(payload["channel"], "whatsapp")
        self.assertEqual(payload["metadata"]["type"], "pricing_guardrail")
        self.assertEqual(payload["type"], "pricing")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["context"]["pricing"]["floor_unit_price"], 70_000)
        self.assertEqual(payload["context"]["pricing"]["target_unit_price"], 72_000)
        self.assertEqual(payload["context"]["pricing"]["review_reason_codes"], ["below_floor_claim"])

    async def test_failed_escalation_is_retryable(self):
        now = datetime.now(timezone.utc)
        event = {
            "event_key": "blocked-1",
            "escalation_status": "pending",
            "next_attempt_at": now - timedelta(seconds=1),
            "escalation_payload": {
                "dealer_phone": "919999999999",
                "customer_name": "Dealer",
                "raw_text": "Price 69000",
                "event_type": "ai_output_blocked",
                "sku": "SKU-1",
                "product_context": "SKU-1",
                "floor": 70000,
                "observed_price": 69000,
                "reason": "below_floor_ai_price",
            },
        }

        class EventCollection:
            async def update_one(self, query, update):
                if query.get("escalation_claim_id") and query["escalation_claim_id"] != event.get("escalation_claim_id"):
                    return types.SimpleNamespace(modified_count=0)
                if "$or" in query and event["escalation_status"] not in {"pending", "failed", "sending"}:
                    return types.SimpleNamespace(modified_count=0)
                event.update(update.get("$set", {}))
                event["escalation_attempts"] = event.get("escalation_attempts", 0) + update.get("$inc", {}).get("escalation_attempts", 0)
                return types.SimpleNamespace(modified_count=1)

            async def find_one(self, query):
                return event if query.get("event_key") == event["event_key"] else None

        class DB:
            def __getitem__(self, _name):
                return EventCollection()

        with (
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
            patch("services.pricing_service._escalate", new=AsyncMock(side_effect=RuntimeError("temporary"))),
        ):
            await _deliver_pricing_escalation("lee", "blocked-1")
        self.assertEqual(event["escalation_status"], "failed")

        event["next_attempt_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        with (
            patch("services.pricing_service.get_async_mongo_db", return_value=DB()),
            patch("services.pricing_service._escalate", new=AsyncMock()),
        ):
            await _deliver_pricing_escalation("lee", "blocked-1")
        self.assertEqual(event["escalation_status"], "sent")


class TeamEscalationTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_and_create_are_executable_and_idempotent(self):
        from services import team_service

        class Collection:
            def __init__(self):
                self.docs = {}

            async def find_one(self, query, _projection=None):
                if "id" in query:
                    return self.docs.get(query["id"])
                return None

            async def update_one(self, query, update, upsert=False):
                identifier = query["id"]
                inserted = identifier not in self.docs
                if inserted:
                    self.docs[identifier] = dict(update["$setOnInsert"])
                return types.SimpleNamespace(upserted_id=identifier if inserted else None)

            def find(self, query, _projection=None):
                return Cursor([doc for doc in self.docs.values() if doc.get("status") == query.get("status")])

        collection = Collection()
        class DB:
            def __getitem__(self, _name):
                return collection

        broadcasts = []
        websocket = types.ModuleType("services.websocket_service")
        async def broadcast(_org_id, payload):
            broadcasts.append(payload)
        websocket.broadcast_ai_escalation = broadcast

        with (
            patch("services.team_service.get_async_mongo_db", return_value=DB()),
            patch.dict(sys.modules, {"services.websocket_service": websocket}),
        ):
            payload = {
                "id": "pricing:one",
                "customer_number": "919999999999",
                "status": "pending_human_review",
                "metadata": {"sku": "SKU-1", "reason": "below_floor"},
            }
            first = await team_service.create_ai_escalation("lee", payload)
            second = await team_service.create_ai_escalation("lee", payload)
            rows = await team_service.fetch_ai_escalations("lee")

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(broadcasts), 1)

    async def test_v2_cursor_review_lifecycle_and_best_effort_broadcast(self):
        from services import team_service

        now = datetime.now(timezone.utc)

        class Collection:
            def __init__(self):
                self.docs = {
                    f"esc-{index}": {
                        "id": f"esc-{index}",
                        "status": "pending_human_review",
                        "timestamp": now - timedelta(minutes=index),
                        "created_at": now - timedelta(minutes=index),
                    }
                    for index in range(3)
                }

            async def create_index(self, *_args, **_kwargs):
                return None

            async def find_one(self, query, _projection=None):
                return self.docs.get(query.get("id"))

            async def update_one(self, query, update, upsert=False):
                identifier = query["id"]
                existing = self.docs.get(identifier)
                if "$setOnInsert" in update:
                    if existing:
                        return types.SimpleNamespace(upserted_id=None, matched_count=1)
                    self.docs[identifier] = dict(update["$setOnInsert"])
                    return types.SimpleNamespace(upserted_id=identifier, matched_count=1)
                if not existing:
                    return types.SimpleNamespace(matched_count=0)
                existing.update(update.get("$set", {}))
                for field in update.get("$unset", {}):
                    existing.pop(field, None)
                return types.SimpleNamespace(matched_count=1)

            def find(self, query, _projection=None):
                expected_status = query.get("status")
                rows = [
                    doc.copy() for doc in self.docs.values()
                    if (
                        doc.get("status") in expected_status["$in"]
                        if isinstance(expected_status, dict) and "$in" in expected_status
                        else doc.get("status") == expected_status
                    )
                ]
                if query.get("$or"):
                    cursor_time = query["$or"][0]["timestamp"]["$lt"]
                    cursor_id = query["$or"][1]["id"]["$lt"]
                    rows = [
                        row for row in rows
                        if row["timestamp"] < cursor_time
                        or (row["timestamp"] == cursor_time and row["id"] < cursor_id)
                    ]
                rows.sort(key=lambda row: (row["timestamp"], row["id"]), reverse=True)
                return Cursor(rows)

        collection = Collection()

        class DB:
            def __getitem__(self, _name):
                return collection

        websocket = types.ModuleType("services.websocket_service")

        async def failing_broadcast(*_args, **_kwargs):
            raise RuntimeError("socket down")

        websocket.broadcast_ai_escalation = failing_broadcast
        with (
            patch("services.team_service.get_async_mongo_db", return_value=DB()),
            patch.dict(sys.modules, {"services.websocket_service": websocket}),
            patch.object(team_service, "_escalation_indexes_ready", set()),
        ):
            first = await team_service.fetch_ai_escalations_v2("lee", limit=2)
            second = await team_service.fetch_ai_escalations_v2(
                "lee", limit=2, cursor=first["next_cursor"],
            )
            created = await team_service.create_ai_escalation("lee", {
                "id": "pricing:durable",
                "conversation_id": "whatsapp_1",
                "channel": "whatsapp",
                "metadata": {"type": "pricing_guardrail"},
            })
            reviewed = await team_service.update_escalation_status(
                "lee", "pricing:durable", "reviewed", reviewer_id="agent-1",
            )
            all_rows = await team_service.fetch_ai_escalations_v2("lee", status="all", limit=10)
        self.assertEqual([item["id"] for item in first["items"]], ["esc-0", "esc-1"])
        self.assertEqual([item["id"] for item in second["items"]], ["esc-2"])
        self.assertEqual(created["id"], "pricing:durable")
        self.assertTrue(reviewed)
        stored = collection.docs["pricing:durable"]
        self.assertEqual(stored["status"], "reviewed")
        self.assertEqual(stored["reviewed_by"], "agent-1")
        self.assertIn("expires_at", stored)
        self.assertEqual(all_rows["status"], "all")
        self.assertIn("pricing:durable", {item["id"] for item in all_rows["items"]})


class OOSRoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_supplier_reply_retries_only_failed_steps(self):
        await self._exercise_reply_retry("YES AVAILABLE 15000 RS")

    async def test_pending_acknowledgement_logs_once_without_offer_or_escalation(self):
        await self._exercise_reply_retry("Let me check and confirm tomorrow", pending=True)

    async def _exercise_reply_retry(self, content, pending=False):
        from services import oos_service

        oos_doc = {
            "org_id": "lee",
            "query_id": "query-1",
            "status": "dealers_contacted",
            "sent_dealers": [
                {"dealer_phone": "919999999999", "dealer_name": "Supplier", "message_ids": ["oos-message-1"]}
            ],
        }

        class Collection:
            def __init__(self):
                self.doc = oos_doc

            async def find_one(self, query, *_args, **_kwargs):
                if query.get("sent_dealers.message_ids") == "oos-message-1":
                    return self.doc
                if query.get("dealer_replies.source_message_id") == "incoming-1":
                    return self.doc if self.doc.get("dealer_replies") else None
                return None

            async def update_one(self, query, update, **_kwargs):
                if "$push" in update:
                    source_id = update["$push"]["dealer_replies"]["source_message_id"]
                    if any(reply.get("source_message_id") == source_id for reply in self.doc.get("dealer_replies", [])):
                        return types.SimpleNamespace(modified_count=0)
                    self.doc.setdefault("dealer_replies", []).append(update["$push"]["dealer_replies"])
                    return types.SimpleNamespace(modified_count=1)
                for key, status in update.get("$set", {}).items():
                    step = key.rsplit(".", 1)[-1]
                    self.doc["dealer_replies"][0]["steps"][step] = status
                return types.SimpleNamespace(modified_count=1)

        collection = Collection()
        class DB:
            def __getitem__(self, _name):
                return collection

        sheet = AsyncMock()
        escalation = AsyncMock()
        pricing = AsyncMock(side_effect=[RuntimeError("temporary"), None])

        with (
            patch("services.oos_service.get_async_mongo_db", return_value=DB()),
            patch("services.oos_service.log_dealer_response_to_sheet", new=sheet),
            patch("services.oos_service.create_oos_escalation", new=escalation),
            patch("services.pricing_service.record_oos_supplier_reply", new=pricing),
        ):
            matched = await oos_service.handle_dealer_reply(
                "lee",
                "919999999999",
                content,
                {"id": "oos-message-1"},
                source_message_id="incoming-1",
            )
            duplicate = await oos_service.handle_dealer_reply(
                "lee",
                "919999999999",
                content,
                {"id": "oos-message-1"},
                source_message_id="incoming-1",
            )
        self.assertTrue(matched)
        self.assertTrue(duplicate)
        sheet.assert_awaited_once()
        if pending:
            escalation.assert_not_awaited()
            pricing.assert_not_awaited()
            self.assertEqual(collection.doc["status"], "dealers_contacted")
            self.assertEqual(len(collection.doc["dealer_replies"]), 1)
        else:
            escalation.assert_awaited_once()
            self.assertEqual(pricing.await_count, 2)
        self.assertEqual(collection.doc["dealer_replies"][0]["steps"]["pricing"], "done")

    async def test_phone_only_reply_is_not_assigned_when_two_requests_match(self):
        from services import oos_service

        class Collection:
            def find(self, _query):
                return Cursor([{"query_id": "one"}, {"query_id": "two"}])

            async def find_one(self, *_args, **_kwargs):
                return None

            async def update_one(self, *_args, **_kwargs):
                raise AssertionError("ambiguous reply must not be stored")

        class DB:
            def __getitem__(self, _name):
                return Collection()

        with patch("services.oos_service.get_async_mongo_db", return_value=DB()):
            matched = await oos_service.handle_dealer_reply(
                "lee",
                "919999999999",
                "70250",
                source_message_id="incoming-2",
            )
        self.assertFalse(matched)


if __name__ == "__main__":
    unittest.main()
