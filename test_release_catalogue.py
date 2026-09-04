"""Offline behavioural release regressions; never connects to a database/provider."""
import ast
import asyncio
import copy
import math
import re
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from test_product_sheet_pricing_foundation import _load_product_sheet_sync
from services.product_request_policy import available_stock, eligibility_reason
from services.stock_gst import normalize_category, normalize_gst_rate, normalize_gst_inclusive, DEFAULT_GST_RATE

MODULE = _load_product_sheet_sync()


def get(doc, path):
    for part in path.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def matches(doc, query):
    for field, wanted in query.items():
        if field in {"$or", "$and"}:
            if not (any if field == "$or" else all)(matches(doc, part) for part in wanted):
                return False
            continue
        actual = get(doc, field)
        if isinstance(wanted, dict):
            for op, val in wanted.items():
                values = actual if isinstance(actual, list) else [actual]
                if op == "$in" and not any(item in val for item in values): return False
                if op == "$nin" and any(item in val for item in values): return False
                if op == "$ne" and actual == val: return False
                if op == "$exists" and (actual is not None) != val: return False
                if op == "$regex" and not re.search(val, str(actual or ""), re.I): return False
        elif wanted not in (actual if isinstance(actual, list) else [actual]):
            return False
    return True


def apply(doc, update, inserted=False):
    for op, fields in update.items():
        if op == "$setOnInsert" and not inserted: continue
        for path, value in fields.items():
            current = doc
            parts = path.split(".")
            for part in parts[:-1]: current = current.setdefault(part, {})
            key = parts[-1]
            if op == "$unset": current.pop(key, None)
            elif op == "$addToSet":
                if value not in current.setdefault(key, []): current[key].append(value)
            else: current[key] = copy.deepcopy(value)


class Collection:
    def __init__(self, rows=()): self.rows = copy.deepcopy(list(rows))
    def find(self, query, *_): return [copy.deepcopy(row) for row in self.rows if matches(row, query)]
    def find_one(self, query, *_): return next(iter(self.find(query)), None)
    def create_index(self, *a, **kw): pass
    def update_one(self, query, update, upsert=False):
        for row in self.rows:
            if matches(row, query):
                apply(row, update)
                return types.SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if upsert:
            row = {**query, "_id": f"new-{len(self.rows)}"}
            apply(row, update, inserted=True)
            self.rows.append(row)
            return types.SimpleNamespace(matched_count=0, modified_count=0, upserted_id=row["_id"])
        return types.SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
    def update_many(self, query, update):
        for row in self.rows:
            if matches(row, query): apply(row, update)
    def bulk_write(self, operations, **kw):
        results = [self.update_one(op.query, op.update, **op.kwargs) for op in operations]
        return types.SimpleNamespace(upserted_count=sum(r.upserted_id is not None for r in results), modified_count=sum(r.modified_count for r in results))
    def count_documents(self, query): return len(self.find(query))
    def delete_many(self, query):
        old = len(self.rows)
        self.rows = [row for row in self.rows if not matches(row, query)]
        return types.SimpleNamespace(deleted_count=old-len(self.rows))


class Cursor:
    def __init__(self, rows): self.rows = rows
    def limit(self, value): self.rows = self.rows[:value]; return self
    def max_time_ms(self, value): assert value <= 2000; return self
    async def to_list(self, length): return copy.deepcopy(self.rows[:length])


class AsyncCollection(Collection):
    def find(self, query, *_): return Cursor(Collection.find(self, query))
    async def find_one(self, query, *_): return next(iter(Collection.find(self, query)), None)


class Database:
    def __init__(self, products, connections=(), tally=(), links=(), integration=(), async_mode=False):
        cls = AsyncCollection if async_mode else Collection
        self.products = cls(products)
        self.product_sheet_connections = cls(connections)
        self.organizations_metadata = cls([{"org_id": "org", "product_dedup_backfilled": True}])
        self.tally_stock_items, self.stock_product_links, self.tally_integrations = cls(tally), cls(links), cls(integration)
    def __getitem__(self, name):
        assert name == "products_org", name
        return self.products


def stock_scope():
    tree = ast.parse(Path("services/stock_service.py").read_text())
    names = {"normalize_sku", "_sheet_sku", "_number", "_sheet_quantity", "_version", "_row",
             "_primary_source_product", "_stock_fresh", "effective_stock_product", "_effective_stock_product", "_tally_online"}
    scope = dict(Any=Any, re=re, math=math, asyncio=asyncio, PyMongoError=type("PyMongoError", (Exception,), {}), datetime=datetime, timedelta=timedelta, timezone=timezone,
                 normalize_category=normalize_category, normalize_gst_rate=normalize_gst_rate,
                 normalize_gst_inclusive=normalize_gst_inclusive, DEFAULT_GST_RATE=DEFAULT_GST_RATE)
    exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names], type_ignores=[]), "stock", "exec"), scope)
    return scope


class SyncRelease(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = {"org_id": "org", "sheet_id": "sheet", "status": "active", "is_primary": True,
            "tab_name": "Sheet1", "column_mapping": {"approved_floor_price": "Floor", "cost_price": "Cost"}}
        self.product = {"_id": "stable-id", "org_id": "org", "type": "laptop", "unique_identifier": "GPU-1", "model_no": "GPU-1",
            "source": "google_sheet", "source_sheet_ids": ["sheet"], "approved_floor_price": 100,
            "price_source_sheet_id": "sheet", "stock": 2}
        self.db = Database([self.product], [self.connection])

    async def sync(self, headers, rows):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = types.SimpleNamespace(status_code=200, json=lambda: {"values": rows})
        with patch.object(MODULE, "get_mongo_db", return_value=self.db), patch.object(MODULE, "refresh_google_token", AsyncMock(return_value="dummy")), patch.object(MODULE, "_detect_header_row", AsyncMock(return_value=(headers, 1, 0))), patch.object(MODULE.httpx, "AsyncClient", return_value=client):
            return await MODULE.sync_product_sheet("org", "sheet")

    async def test_invalid_present_row_retains_id_and_correction_restores_it(self):
        result = await self.sync(["SKU", "Cost", "Floor"], [["GPU-1", "0", "100"]])
        self.assertEqual(result["products_removed"], 0)
        product = self.db.products.rows[0]
        self.assertEqual(product["_id"], "stable-id")
        self.assertNotIn("approved_floor_price", product)
        self.assertFalse(product["source_values"]["sheet"]["source_valid"])
        await self.sync(["SKU", "Cost", "Floor"], [["GPU-1", "90", "105"]])
        self.assertEqual(self.db.products.rows[0]["_id"], "stable-id")
        self.assertEqual(self.db.products.rows[0]["approved_floor_price"], 105)
        self.assertTrue(self.db.products.rows[0]["source_values"]["sheet"]["source_valid"])

    async def test_removed_floor_unset_on_real_sync(self):
        await self.sync(["SKU", "Cost"], [["GPU-1", "90"]])
        self.assertNotIn("approved_floor_price", self.db.products.rows[0])

    async def test_numeric_or_boolean_skus_are_not_deleted_after_upsert(self):
        result = await self.sync(["SKU", "Floor"], [["-1", "100"], ["true", "200"]])
        self.assertEqual({row["unique_identifier"] for row in self.db.products.rows}, {"-1", "True"})
        self.assertEqual(result["products_removed"], 1)

    async def test_duplicate_headers_fail_closed_without_deletion(self):
        result = await self.sync(["SKU", "SKU", "Floor"], [["GPU-1", "GPU-2", "100"]])
        self.assertEqual(result["products_removed"], 0)
        self.assertEqual(len(self.db.products.rows), 1)
        self.assertNotIn("approved_floor_price", self.db.products.rows[0])

    async def test_unsafe_identity_does_not_delete_and_invalidates_old_snapshot(self):
        result = await self.sync(["Description", "Floor"], [["GPU description", "100"]])
        self.assertEqual(result["products_removed"], 0)
        self.assertEqual(len(self.db.products.rows), 1)
        self.assertNotIn("approved_floor_price", self.db.products.rows[0])
        self.assertTrue(any("skipped" in error for error in result["errors"]))

    async def test_real_removed_row_is_reconciled(self):
        result = await self.sync(["SKU", "Floor"], [])
        self.assertEqual(result["products_removed"], 1)

    async def test_duplicate_identity_invalidates_instead_of_publishing_first_row(self):
        await self.sync(["SKU", "Floor"], [["GPU-1", "110"], ["GPU-1", "120"]])
        self.assertEqual(len(self.db.products.rows), 1)
        self.assertNotIn("approved_floor_price", self.db.products.rows[0])

    async def test_invalid_secondary_preserves_other_source_approval(self):
        self.db.products.rows[0].update(price_source_sheet_id="other", source_sheet_ids=["sheet", "other"])
        self.db.product_sheet_connections.rows[0]["is_primary"] = False
        await self.sync(["SKU", "Floor"], [["GPU-1", "bad"]])
        self.assertEqual(self.db.products.rows[0]["approved_floor_price"], 100)
        self.assertEqual(self.db.products.rows[0]["price_source_sheet_id"], "other")


class EffectiveStockRelease(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = stock_scope()
        self.stamp = datetime.now(timezone.utc)
        self.product = {"_id": "p", "model_no": "GPU-1", "stock": 8, "updated_at": self.stamp}
        self.db = Database([self.product], async_mode=True)

    async def resolve(self):
        return await self.scope["effective_stock_product"]("org", self.product, database=self.db)

    async def test_online_tally_zero_overrides_positive_sheet(self):
        self.db.tally_stock_items.rows = [{"org_id": "org", "master_id": "t", "model_number": "gpu 1", "closing_quantity": 0, "synced_at": self.stamp}]
        self.db.tally_integrations.rows = [{"org_id": "org", "last_seen_at": self.stamp}]
        self.assertFalse(available_stock(await self.resolve()))
        self.db.tally_stock_items.rows[0]["closing_quantity"] = 3
        self.product["stock"] = 0
        self.db.products.rows = [self.product]
        self.assertTrue(available_stock(await self.resolve()))
        self.db.tally_integrations.rows = []
        self.assertFalse(available_stock(await self.resolve()))

    async def test_fresh_candidate_read_overrides_stale_callers_snapshot(self):
        self.db.products.rows[0]["stock"] = 0
        self.assertFalse(available_stock(await self.resolve()))

    async def test_primary_snapshot_not_secondary_top_level_stock(self):
        self.product.update(source_sheet_ids=["primary", "other"], source_values={"primary": {"stock": 0, "source_valid": True, "updated_at": self.stamp}})
        self.db.products.rows = [self.product]
        self.db.product_sheet_connections.rows = [{"org_id": "org", "sheet_id": "primary", "status": "active", "is_primary": True}]
        self.assertFalse(available_stock(await self.resolve()))
        self.product["source_values"]["primary"]["stock"] = 4
        self.assertTrue(available_stock(await self.resolve()))
        self.product["source_values"]["primary"]["source_valid"] = False
        self.assertFalse(available_stock(await self.resolve()))

    async def test_primary_source_errors_and_missing_snapshot_fail_closed(self):
        self.product.update(source_sheet_ids=["primary"], source_values={"primary": {"stock": 3, "updated_at": self.stamp}})
        self.db.products.rows = [self.product]
        primary = {"org_id": "org", "sheet_id": "primary", "status": "error", "is_primary": True}
        self.db.product_sheet_connections.rows = [primary]
        self.assertFalse(available_stock(await self.resolve()))
        primary["status"] = "active"
        self.assertTrue(available_stock(await self.resolve()))
        self.product["source_values"] = {}
        self.assertFalse(available_stock(await self.resolve()))

    async def test_duplicate_tally_and_truncated_candidate_set_fail_closed(self):
        self.db.tally_stock_items.rows = [{"org_id": "org", "master_id": str(i), "model_number": "GPU1", "closing_quantity": 9, "synced_at": self.stamp} for i in range(2)]
        self.assertFalse(available_stock(await self.resolve()))
        self.db.tally_stock_items.rows = []
        self.db.products.rows = [{**self.product, "_id": str(i)} for i in range(6)]
        self.assertFalse(available_stock(await self.resolve()))

    async def test_ambiguity_stale_unknown_authority_fail_closed(self):
        self.assertTrue(available_stock(await self.resolve()))
        self.db.products.rows.append({**self.product, "_id": "duplicate"})
        self.assertFalse(available_stock(await self.resolve()))
        self.db.products.rows = [self.product]
        self.product["updated_at"] = self.stamp - timedelta(days=2)
        self.assertFalse(available_stock(await self.resolve()))
        self.product["updated_at"] = self.stamp
        self.product["source_sheet_ids"] = ["missing"]
        self.assertFalse(available_stock(await self.resolve()))

    async def test_explicit_tally_link_and_stale_tally_fail_closed(self):
        self.db.stock_product_links.rows = [{"org_id": "org", "sheet_product_id": "p", "tally_master_id": "t"}]
        self.db.tally_stock_items.rows = [{"org_id": "org", "master_id": "t", "model_number": "other sku", "closing_quantity": 0, "synced_at": self.stamp}]
        self.db.tally_integrations.rows = [{"org_id": "org", "last_seen_at": self.stamp}]
        self.assertFalse(available_stock(await self.resolve()))
        self.db.tally_stock_items.rows[0]["closing_quantity"] = 3
        self.assertTrue(available_stock(await self.resolve()))
        self.db.tally_stock_items.rows[0]["synced_at"] = self.stamp - timedelta(days=2)
        self.assertFalse(available_stock(await self.resolve()))

    async def test_database_timeout_fails_closed_without_crashing_list(self):
        self.db.product_sheet_connections.find_one = AsyncMock(side_effect=TimeoutError())
        self.assertFalse(available_stock(await self.resolve()))

    async def test_request_list_uses_effective_stock_not_raw_catalogue(self):
        self.db.tally_stock_items.rows = [{"org_id": "org", "master_id": "t", "model_number": "GPU1", "closing_quantity": 0, "synced_at": self.stamp}]
        self.db.tally_integrations.rows = [{"org_id": "org", "last_seen_at": self.stamp}]
        data = {"items": [{"product_id": "p", "catalogue_org_id": "org"}], "total": [{"count": 1}]}
        db = self.db
        class ListDB:
            product_requests = types.SimpleNamespace(aggregate=lambda pipeline: Cursor([data]))
            def __getattr__(self, name): return getattr(db, name)
            def __getitem__(self, name): return AsyncCollection() if name == "messages_org" else db[name]
        import typing
        node = next(n for n in ast.parse(Path("api/product_request_routes.py").read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "list_requests")
        node.decorator_list = []
        scope = dict(re=re, asyncio=asyncio, Request=object, CurrentUser=object, Literal=typing.Literal,
                     Query=lambda value, **kw: value, ObjectId=types.SimpleNamespace(is_valid=lambda _: False),
                     get_async_mongo_db=ListDB, catalogue_org=AsyncMock(return_value="org"),
                     conversation_access_condition=AsyncMock(return_value={}), available_stock=available_stock,
                     serialize_mongo=lambda value: value)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "request_list", "exec"), scope)
        stock = types.ModuleType("services.stock_service")
        stock.effective_stock_product = self.scope["effective_stock_product"]
        with patch.dict("sys.modules", {"services.stock_service": stock}):
            response = await scope["list_requests"](None, types.SimpleNamespace(org_id="org", user_id="user", role="admin"))
        self.assertFalse(response["items"][0]["in_stock"])


class CatalogueRelease(unittest.TestCase):
    def test_seen_and_published_identities_have_identical_normalization(self):
        for sku in ("-1", "001", "2.0", "1e3", "true", "FALSE", "Yes", "GPU-1"):
            with self.subTest(sku=sku):
                doc, error = MODULE.transform_row({"SKU": sku}, {}, "sheet", "org")
                self.assertIsNone(error)
                self.assertEqual(MODULE.source_row_identity({"SKU": sku}, {}), doc["unique_identifier"])

    def test_removed_floor_is_not_carried_into_new_authority(self):
        old, error = MODULE.transform_row({"SKU": "GPU-1", "Floor": "100", "Sell": "150"},
            {"approved_floor_price": "Floor", "target_selling_price": "Sell"}, "sheet", "org")
        self.assertIsNone(error)
        fresh, error = MODULE.transform_row({"SKU": "GPU-1", "Sell": "160"},
            {"approved_floor_price": "Floor", "target_selling_price": "Sell"}, "sheet", "org")
        self.assertIsNone(error)
        # Reproduce the old set-only publication, then exercise its replacement.
        builder = getattr(MODULE, "source_snapshot_update", None)
        update = builder(fresh, "sheet", 2, authoritative=True) if builder else {"$set": fresh}
        stored = {**old, **update["$set"]}
        for field in update.get("$unset", {}):
            stored.pop(field, None)
        self.assertIsNone(stored.get("approved_floor_price"))
        self.assertEqual(stored["price"], 160)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__)))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("RELEASE_CATALOGUE_OK")
