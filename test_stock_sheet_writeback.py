"""Stock writeback regressions. No network, credentials or database required."""
import copy
import ast
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, Mock

from test_product_sheet_pricing_foundation import _load_product_sheet_sync


class Client:
    def __init__(self, rows, *, expire_write=False, mismatch=False, write_status=200):
        self.rows = copy.deepcopy(rows)
        self.expire_write = expire_write
        self.mismatch = mismatch
        self.write_status = write_status
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        if url.endswith("!A2:ZZ"):
            values = self.rows
        else:
            values = [self.rows[0]]
            if kwargs.get("params", {}).get("valueRenderOption") == "UNFORMATTED_VALUE" and self.mismatch:
                values = [["", "OTHER", "Other", 150, 80, 18, False]]
        return types.SimpleNamespace(status_code=200, json=lambda: {"values": values})

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.expire_write:
            self.expire_write = False
            return types.SimpleNamespace(status_code=401, json=lambda: {})
        if self.write_status != 200:
            return types.SimpleNamespace(status_code=self.write_status, json=lambda: {})
        for cell in kwargs["json"].get("data", []):
            column = cell["range"].split("!")[1][0]
            self.rows[0][ord(column) - ord("A")] = cell["values"][0][0]
        return types.SimpleNamespace(status_code=200, json=lambda: {})


class StockSheetWritebackTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = _load_product_sheet_sync()
        self.module.refresh_google_token = AsyncMock(return_value="initial")
        self.module._force_token_refresh = AsyncMock(return_value="refreshed")
        self.module._detect_header_row = AsyncMock(return_value=(
            ["Part", "Title", "Retail", "Floor", "GST Rate", "GST Inclusive"], 1, 1,
        ))
        self.connection = {
            "sheet_id": "sheet", "tab_name": "Bob's Stock", "product_type": "component",
            "column_mapping": {"model_no": "Part", "product_name": "Title",
                               "target_selling_price": "Retail", "approved_floor_price": "Floor"},
        }
        self.product = {"model_no": "SKU-1", "source_sheet_ids": ["sheet"],
                        "source_rows": {"sheet": 99},
                        "source_values": {"sheet": {"target_selling_price": 100}}}

    async def write(self, rows=None, **options):
        self.client = Client(rows or [["", "SKU-1", "Laptop", 100, 80, 18, False]], **options)
        self.module.httpx.AsyncClient = lambda: self.client
        return await self.module._write_product_sheet_values("org", self.product, {"price": 150}, self.connection)

    async def test_mapping_offset_moved_row_and_canonical_price(self):
        result = await self.write()
        self.assertEqual(result["row_number"], 2)  # Cached row 99 is never trusted.
        cell = self.client.posts[0][1]["json"]["data"][0]
        self.assertTrue(cell["range"].endswith("!D2"))
        self.assertIn("%27Bob%27%27s%20Stock%27", cell["range"])
        self.assertEqual(result["product_document"]["price"], 150)
        self.assertEqual(result["product_document"]["target_selling_price"], 150)
        self.assertEqual(result["product_document"]["approved_floor_price"], 80)
        self.assertTrue(result["product_document"]["price_version"])

    async def test_expired_write_token_retried_once(self):
        await self.write(expire_write=True)
        self.assertEqual(len(self.client.posts), 2)
        self.assertEqual(self.client.posts[-1][1]["headers"]["Authorization"], "Bearer refreshed")

    async def test_sheet_permission_failure_is_not_success(self):
        with self.assertRaisesRegex(ValueError, "rejected the stock update"):
            await self.write(write_status=403)
        self.assertEqual(len(self.client.posts), 1)

    async def test_floor_is_never_edited_as_selling_price(self):
        self.module._detect_header_row.return_value = (["Part", "Title", "Floor"], 1, 0)
        self.connection["column_mapping"].pop("target_selling_price")
        with self.assertRaisesRegex(ValueError, "approved floor prices are not changed"):
            await self.write()
        self.assertFalse(self.client.posts)

    async def test_duplicate_sku_rejected_before_write(self):
        row = ["", "SKU-1", "Laptop", 100, 80, 18, False]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            await self.write([row, row])
        self.assertFalse(self.client.posts)

    async def test_changed_price_rejected_before_write(self):
        with self.assertRaisesRegex(ValueError, "changed since"):
            await self.write([["", "SKU-1", "Laptop", 120, 80, 18, False]])
        self.assertFalse(self.client.posts)

    async def test_retry_already_applied_price_is_idempotent(self):
        result = await self.write([["", "SKU-1", "Laptop", 150, 80, 18, False]])
        self.assertEqual(result["product_document"]["price"], 150)

    async def test_formula_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, "Formula-protected"):
            await self.write([["", "SKU-1", "Laptop", "=80*1.25", 80, 18, False]])
        self.assertFalse(self.client.posts)

    async def test_deleted_source_product_is_not_appended(self):
        with self.assertRaisesRegex(ValueError, "removed or renamed"):
            await self.write([["", "OTHER", "Laptop", 100, 80, 18, False]])
        self.assertFalse(self.client.posts)

    async def test_readback_must_verify_identity(self):
        with self.assertRaisesRegex(ValueError, "row moved"):
            await self.write(mismatch=True)

    async def test_primary_sheet_uses_database_factory(self):
        collection = Mock()
        collection.find.return_value = [{"_id": "connection", "sheet_id": "sheet", "is_primary": True}]
        self.module.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=collection)
        result = await self.module.primary_product_sheet("org")
        self.assertEqual(result["sheet_id"], "sheet")

    async def test_multiple_sheets_require_explicit_primary(self):
        collection = Mock()
        collection.find.return_value = [{"sheet_id": "a"}, {"sheet_id": "b"}]
        self.module.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=collection)
        with self.assertRaisesRegex(ValueError, "Choose one primary"):
            await self.module.primary_product_sheet("org")

    async def test_busy_primary_never_falls_back_to_secondary(self):
        collection = Mock()
        collection.find.return_value = [
            {"sheet_id": "a", "is_primary": True, "status": "syncing"},
            {"sheet_id": "b", "status": "active"},
        ]
        self.module.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=collection)
        with self.assertRaisesRegex(ValueError, "Primary product Sheet is syncing"):
            await self.module.primary_product_sheet("org")

    async def test_lease_conflict_prevents_external_write(self):
        collection = Mock()
        collection.update_one.return_value = types.SimpleNamespace(matched_count=0)
        self.module.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=collection)
        self.module.primary_product_sheet = AsyncMock(return_value=self.connection)
        self.module._write_product_sheet_values = AsyncMock()
        with self.assertRaisesRegex(ValueError, "syncing or being edited"):
            await self.module.write_product_sheet_values("org", self.product, {"price": 150})
        self.module._write_product_sheet_values.assert_not_awaited()

    async def test_write_failure_releases_claim_and_schedules_reconciliation(self):
        collection = Mock()
        collection.update_one.return_value = types.SimpleNamespace(matched_count=1)
        self.module.get_mongo_db = lambda: types.SimpleNamespace(product_sheet_connections=collection)
        self.module.primary_product_sheet = AsyncMock(return_value=self.connection)
        self.module._write_product_sheet_values = AsyncMock(side_effect=TimeoutError("unknown delivery"))
        with self.assertRaises(TimeoutError):
            await self.module.write_product_sheet_values("org", self.product, {"price": 150})
        release = collection.update_one.call_args_list[-1].args
        self.assertEqual(release[1]["$set"]["status"], "active")
        self.assertIn("next_reconcile_at", release[1]["$set"])
        self.assertIn("sync_claim_id", release[1]["$unset"])

    async def test_verified_product_and_price_provenance_are_published(self):
        # The merchant removed the old approval column before this edit.
        self.module._detect_header_row.return_value = (["Part", "Title", "Retail", "GST Rate", "GST Inclusive"], 1, 1)
        result = await self.write(rows=[["", "SKU-1", "Laptop", 100, 18, False]])
        collection = Mock()
        collection.update_one.return_value = types.SimpleNamespace(matched_count=1)
        products = Mock()
        class Database:
            product_sheet_connections = collection
            def __getitem__(self, name):
                return products
        self.module.get_mongo_db = lambda: Database()
        self.module.primary_product_sheet = AsyncMock(return_value=self.connection)
        self.module._write_product_sheet_values = AsyncMock(return_value=result)
        self.module._renew_product_sheet_sync_claim = AsyncMock()
        self.product["_id"] = "existing-id"
        response = await self.module.write_product_sheet_values("org", self.product, {"price": 150})
        query, update = products.update_one.call_args.args
        self.assertEqual(query, {"_id": "existing-id"})
        self.assertEqual(update["$set"]["price"], 150)
        self.assertEqual(update["$set"]["source_values.sheet"]["target_selling_price"], 150)
        self.assertEqual(update["$set"]["source_rows.sheet"], 2)
        self.assertTrue(update["$set"]["price_version"])
        self.assertIn("approved_floor_price", update["$unset"])
        self.assertNotIn("approved_floor_price", update["$set"]["source_values.sheet"])
        self.assertNotIn("product_document", response)

    async def test_header_detection_preserves_internal_blank_columns(self):
        path = Path(__file__).parent / "api/google_sheets_routes.py"
        tree = ast.parse(path.read_text())
        node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "_detect_header_row")
        client = Client([])
        client.get = AsyncMock(return_value=types.SimpleNamespace(
            status_code=200, json=lambda: {"values": [["", "Part", "", "Retail"]]},
        ))
        namespace = {"httpx": types.SimpleNamespace(AsyncClient=lambda: client)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
        headers, row, offset = await namespace["_detect_header_row"]("sheet", "Sheet1", "token", "org")
        self.assertEqual((headers, row, offset), (["Part", "", "Retail"], 1, 1))


if __name__ == "__main__":
    unittest.main()
