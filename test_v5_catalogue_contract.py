"""Offline checks: additive catalogue writes, sourcing payloads and optional learning."""
import ast
import asyncio
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from services.catalogue_fields import canonical_fields
from services.oos_service import format_dealer_sourcing_message
from test_product_sheet_pricing_foundation import MODULE


def test_source_sync_allocates_identity_once_and_carries_specs():
    document = {"org_id": "org_test", "model_no": "ABC-5060-X", "product_name": "Laptop", "brand": "ASUS",
                "cpu": "i7", "gpu": "RTX 5060", "ram": "16 GB", "storage": "1 TB", "stock": 2}
    first = MODULE.source_snapshot_update(document, "sheet", 2, authoritative=True)
    assert first["$setOnInsert"]["product_id"] == "mongo:oid:" + str(first["$setOnInsert"]["_id"])
    assert "product_id" not in first["$set"] and "_id" not in first["$set"]
    assert first["$set"]["specs"] == {"processor": "i7", "graphics_card": "RTX 5060", "ram_gb": 16, "storage_gb": 1024}
    assert first["$set"]["stock_state"] == "listed"
    second = MODULE.source_snapshot_update({**document, "stock": 0}, "sheet", 2, authoritative=True)
    assert second["$set"]["stock_state"] == "out_of_stock"
    assert {"abc-5060-x", "abc5060x"} <= set(second["$set"]["search_codes"])


def test_lee_stock_and_sku_suffix_are_canonicalized_for_query_search():
    document = {"_id": "lee-1", "org_id": "org_lee", "type": "laptop", "brand": "ASUS",
                "model_name": "ASUS TUF A15", "model_no": "FA506NCQ-HN007WS",
                "sku_full": "FA506NCQ-HN007WS", "sku_suffix": "HN007WS",
                "graphics_card": "NVIDIA_GeForce_RTX_5060", "ram": 16, "storage": 512,
                "stock_available": True}
    fields = canonical_fields(document)
    assert fields["category"] == "laptop" and fields["stock_state"] == "listed"
    assert fields["specs"]["graphics_card"] == "NVIDIA GeForce RTX 5060"
    assert {"fa506ncq-hn007ws", "fa506ncqhn007ws", "hn007ws"} <= set(fields["search_codes"])
    assert "5060" in fields["search_text"] and fields["semantic_text"]


def test_sourcing_keeps_bounds_gpu_and_exclusions_without_customer_budget():
    text = format_dealer_sourcing_message({"category": "laptop", "brand": "asus", "model_name": "ROG",
        "specs": {"processor": "i7", "graphics_card": "5060", "ram_gb_gte": 16, "ram_gb_lte": 32,
                  "storage_gb_in": [512, 1024], "price_max": 90000, "excluded_brands": ["dell"]}})
    assert all(value in text for value in ("i7", "5060", "at least 16 GB", "at most 32 GB", "512 or 1024", "Exclude brands: dell"))
    assert "90000" not in text


def test_learning_writes_do_not_block_customer_processing_and_tasks_are_released():
    source = Path(__file__).with_name("services") / "agent_service.py"
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_record_learning_later")
    registry = SimpleNamespace(background_tasks=set())
    services = ModuleType("core.services")
    services.services = registry
    namespace = {"asyncio": asyncio, "logger": SimpleNamespace(warning=lambda *args: None)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    async def run():
        release, started = asyncio.Event(), asyncio.Event()
        async def slow_write():
            started.set()
            await release.wait()
        with patch.dict("sys.modules", {"core.services": services}):
            namespace["_record_learning_later"](slow_write())
            await asyncio.wait_for(started.wait(), .5)
            assert len(registry.background_tasks) == 1
            release.set()
            await asyncio.gather(*registry.background_tasks)
            await asyncio.sleep(0)
            assert not registry.background_tasks
    asyncio.run(run())
