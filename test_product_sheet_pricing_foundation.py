import asyncio
import copy
import importlib.util
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _UpdateOne:
    def __init__(self, query, update, **kwargs):
        self.query = query
        self.update = update
        self.kwargs = kwargs


def _load_product_sheet_sync():
    saved = {name: sys.modules.get(name) for name in (
        "httpx", "pymongo", "pymongo.errors", "loguru", "database",
        "api", "api.google_sheets_routes",
    )}
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    pymongo = types.ModuleType("pymongo")
    pymongo.ReturnDocument = types.SimpleNamespace(AFTER="after")
    pymongo.UpdateOne = _UpdateOne
    pymongo_errors = types.ModuleType("pymongo.errors")
    pymongo_errors.BulkWriteError = type("BulkWriteError", (Exception,), {})
    loguru = types.ModuleType("loguru")
    loguru.logger = _Logger()
    database = types.ModuleType("database")
    database.get_mongo_db = lambda: None
    api = types.ModuleType("api")
    api.__path__ = []
    sheets = types.ModuleType("api.google_sheets_routes")
    sheets.refresh_google_token = None
    sheets._force_token_refresh = None
    sheets._detect_header_row = None
    sys.modules.update({
        "httpx": httpx,
        "pymongo": pymongo,
        "pymongo.errors": pymongo_errors,
        "loguru": loguru,
        "database": database,
        "api": api,
        "api.google_sheets_routes": sheets,
    })
    try:
        spec = importlib.util.spec_from_file_location(
            "_product_sheet_sync_pricing_test", ROOT / "services/product_sheet_sync.py"
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


MODULE = _load_product_sheet_sync()


class ProductSheetMappingTest(unittest.TestCase):
    def test_product_picker_uses_the_effective_sales_catalogue(self):
        routes = (ROOT / "api/common_routes.py").read_text(encoding="utf-8")
        self.assertIn('organization.get("sales_catalogue_org_id")', routes)
        self.assertIn("get_products_for_org(\n        catalogue_org_id,", routes)
        self.assertIn("count_products_for_org(catalogue_org_id, q)", routes)

    def test_disconnect_fences_sync_before_unlink_and_deletes_last(self):
        routes = (ROOT / "api/product_sheets_routes.py").read_text(encoding="utf-8")
        disconnect = routes[routes.index("async def disconnect_product_sheet"):]
        self.assertLess(disconnect.index("begin_product_sheet_disconnect"), disconnect.index("unlink_product_sheet_source"))
        self.assertLess(disconnect.index("unlink_product_sheet_source"), disconnect.index("delete_one"))
        sync = (ROOT / "services/product_sheet_sync.py").read_text(encoding="utf-8")
        self.assertIn("disconnected during sync", sync)
        self.assertIn('"sync_claim_id": sync_claim_id', sync)
        self.assertIn("except asyncio.CancelledError", sync)
        self.assertIn('"disconnect_requested_at": {"$exists": False}', sync)

    def test_saved_mapping_wins_over_an_unmapped_alias_collision(self):
        mapped = MODULE.apply_saved_column_mapping(
            {"Chosen Sell": "120", "Selling Price": "999"},
            {"target_selling_price": "Chosen Sell"},
        )
        self.assertEqual(mapped["target_selling_price"], "120")

    def test_saved_mapping_populates_distinct_price_fields_and_provenance(self):
        row = {
            "Item Label": "RTX 5060",
            "Part Code": "RTX-5060",
            "Buy": "Rs. 50,000",
            "Floor": "59,000",
            "Sell": "61,000",
            "Retail": "70,000",
            "Sell Tax": "included",
            "Buy Tax": "extra",
            "Money": "inr",
            "Available": "3",
        }
        mapping = {
            "product_name": "Item Label",
            "model_no": "Part Code",
            "cost_price": "Buy",
            "approved_floor_price": "Floor",
            "target_selling_price": "Sell",
            "mrp": "Retail",
            "selling_gst_inclusive": "Sell Tax",
            "cost_gst_inclusive": "Buy Tax",
            "currency": "Money",
            "stock": "Available",
        }
        product, error = MODULE.transform_row(row, mapping, "sheet-primary", "org-1")
        self.assertIsNone(error)
        self.assertEqual(product["cost_price"], 50_000)
        self.assertEqual(product["approved_floor_price"], 59_000)
        self.assertEqual(product["target_selling_price"], 61_000)
        self.assertEqual(product["mrp"], 70_000)
        self.assertEqual(product["price"], 61_000)  # legacy display
        self.assertEqual(product["price_source_sheet_id"], "sheet-primary")
        self.assertIsNotNone(product["price_verified_at"])
        self.assertEqual(len(product["price_version"]), 64)

    def test_cost_and_mrp_aliases_no_longer_collapse_into_legacy_price(self):
        product, error = MODULE.transform_row(
            {
                "Name": "GPU", "SKU": "GPU-1", "Cost": "100",
                "MRP": "150", "Price": "140",
            },
            {},
            "sheet-1",
            "org-1",
        )
        self.assertIsNone(error)
        self.assertEqual(product["cost_price"], 100)
        self.assertEqual(product["mrp"], 150)
        self.assertEqual(product["price"], 140)
        self.assertIsNone(product.get("approved_floor_price"))
        self.assertIsNone(product.get("target_selling_price"))

    def test_invalid_explicit_gst_rate_is_not_silently_replaced(self):
        product, error = MODULE.transform_row(
            {"Name": "GPU", "SKU": "GPU-1", "Price": "140", "GST Rate": "not-a-rate"},
            {},
            "sheet-1",
            "org-1",
        )
        self.assertIsNone(product)
        self.assertIn("gst_rate must be a percentage", error)

    def test_source_unlink_preserves_current_authority_or_clears_safety(self):
        verified_at = datetime(2026, 8, 31, tzinfo=timezone.utc)
        document = {
            "source_sheet_ids": ["a", "b"],
            "price_source_sheet_id": "a",
            "approved_floor_price": 50_000,
            "source_values": {
                "a": {
                    "price": 51_000,
                    "approved_floor_price": 50_000,
                    "target_selling_price": 51_000,
                    "price_source_sheet_id": "a",
                    "price_verified_at": verified_at,
                    "price_version": "a-version",
                },
                "b": {
                    "price": 61_000,
                    "approved_floor_price": 59_000,
                    "target_selling_price": 61_000,
                    "price_source_sheet_id": "b",
                    "price_verified_at": verified_at,
                    "price_version": "b-version",
                }
            },
        }
        update = MODULE.source_unlink_update(document, "b")
        self.assertEqual(update["$set"]["price_source_sheet_id"], "a")
        self.assertEqual(update["$set"]["approved_floor_price"], 50_000)
        self.assertNotIn("approved_floor_price", update["$unset"])

        update = MODULE.source_unlink_update(document, "a")
        self.assertEqual(update["$set"]["price"], 61_000)
        self.assertIn("approved_floor_price", update["$unset"])
        self.assertIn("price_verified_at", update["$unset"])

    def test_unlinking_unrelated_source_preserves_legacy_current_authority(self):
        document = {
            "source_sheet_ids": ["current", "removed"],
            "source_sheet_id": "removed",
            "price_source_sheet_id": "current",
            "approved_floor_price": 100,
            "price_version": "legacy-version",
            # This product predates per-source snapshots.
            "source_values": {},
        }
        update = MODULE.source_unlink_update(document, "removed")
        self.assertIn("source_sheet_id", update["$unset"])
        self.assertNotIn("approved_floor_price", update["$unset"])
        self.assertNotIn("price_version", update["$unset"])


class _Connections:
    def __init__(self, document):
        self.document = document
        self.lock = threading.Lock()
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def find_one_and_update(self, _query, update, **_kwargs):
        with self.lock:
            now = datetime.now(timezone.utc)
            next_at = self.document.get("next_reconcile_at")
            lease_until = self.document.get("reconcile_lease_until")
            if next_at and next_at > now:
                return None
            if lease_until and lease_until > now:
                return None
            self.document.update(update["$set"])
            return copy.deepcopy(self.document)

    def update_one(self, query, update):
        with self.lock:
            if query.get("reconcile_lease_id") != self.document.get("reconcile_lease_id"):
                return types.SimpleNamespace(modified_count=0)
            self.document.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                self.document.pop(key, None)
            return types.SimpleNamespace(modified_count=1)


def _connection_matches(document, query):
    for key, expected in query.items():
        actual = document.get(key)
        if isinstance(expected, dict):
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$exists" in expected and (key in document) is not expected["$exists"]:
                return False
            continue
        if actual != expected:
            return False
    return True


class _DisconnectConnections:
    def __init__(self, document):
        self.document = document
        self.lock = threading.Lock()

    def find_one(self, query, _projection=None):
        with self.lock:
            return copy.deepcopy(self.document) if _connection_matches(self.document, query) else None

    def update_one(self, query, update):
        with self.lock:
            if not _connection_matches(self.document, query):
                return types.SimpleNamespace(matched_count=0)
            self.document.update(copy.deepcopy(update.get("$set", {})))
            for key in update.get("$unset", {}):
                self.document.pop(key, None)
            return types.SimpleNamespace(matched_count=1)


class ProductSheetLeaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_sync_claim_cannot_block_disconnect_forever(self):
        connections = _DisconnectConnections({
            "_id": "connection-1",
            "org_id": "org-1",
            "sheet_id": "sheet-1",
            "status": "syncing",
            "sync_claim_id": "abandoned-sync",
            "sync_claim_until": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1),
        })
        MODULE.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=connections)
        await MODULE.begin_product_sheet_disconnect("org-1", "sheet-1", wait_seconds=0.1)
        self.assertEqual(connections.document["status"], "disconnecting")
        self.assertNotIn("sync_claim_id", connections.document)

    async def test_cancelled_sync_claim_is_released(self):
        connections = _DisconnectConnections({
            "_id": "connection-1",
            "org_id": "org-1",
            "sheet_id": "sheet-1",
            "status": "syncing",
            "sync_claim_id": "cancelled-sync",
            "sync_claim_until": datetime.now(timezone.utc) + timedelta(minutes=30),
        })
        database = types.SimpleNamespace(product_sheet_connections=connections)
        await MODULE._release_product_sheet_sync_claim(
            database, "org-1", "sheet-1", "cancelled-sync",
        )
        self.assertEqual(connections.document["status"], "error")
        self.assertNotIn("sync_claim_id", connections.document)

    async def test_disconnect_waits_for_the_active_sync_write_claim(self):
        connections = _DisconnectConnections({
            "_id": "connection-1",
            "org_id": "org-1",
            "sheet_id": "sheet-1",
            "status": "syncing",
            "sync_claim_id": "sync-1",
        })
        MODULE.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=connections)
        disconnect = asyncio.create_task(
            MODULE.begin_product_sheet_disconnect("org-1", "sheet-1", wait_seconds=0.5),
        )
        for _ in range(20):
            if connections.document.get("disconnect_requested_at"):
                break
            await asyncio.sleep(0.01)
        self.assertIn("disconnect_requested_at", connections.document)
        self.assertFalse(disconnect.done())

        connections.update_one(
            {"_id": "connection-1", "status": "syncing", "sync_claim_id": "sync-1"},
            {"$set": {"status": "disconnecting"}, "$unset": {"sync_claim_id": ""}},
        )
        await disconnect
        self.assertEqual(connections.document["status"], "disconnecting")

    async def test_concurrent_reconcilers_share_one_lease(self):
        connections = _Connections({
            "_id": "connection-1",
            "org_id": "org-1",
            "sheet_id": "sheet-1",
            "status": "active",
            "updated_at": datetime.now(timezone.utc) - timedelta(hours=1),
        })
        MODULE.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=connections)
        calls = []

        async def sync(org_id, sheet_id, *, reconcile_claim_id=None):
            calls.append((org_id, sheet_id))
            self.assertIsNotNone(reconcile_claim_id)
            await asyncio.sleep(0.02)

        MODULE.sync_product_sheet = sync
        first, second = await asyncio.gather(
            MODULE.reconcile_active_product_sheets(),
            MODULE.reconcile_active_product_sheets(),
        )
        self.assertEqual(first["claimed"] + second["claimed"], 1)
        self.assertEqual(calls, [("org-1", "sheet-1")])
        self.assertNotIn("reconcile_lease_id", connections.document)
        self.assertGreater(connections.document["next_reconcile_at"], datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
