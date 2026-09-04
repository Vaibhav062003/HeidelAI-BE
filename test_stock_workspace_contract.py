import ast
import re
from pathlib import Path
from typing import Any
from services.stock_gst import normalize_category, normalize_gst_inclusive, normalize_gst_rate, DEFAULT_GST_RATE


source = Path("services/stock_service.py").read_text(encoding="utf-8")
tree = ast.parse(source)


def function(name: str):
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


namespace = {"Any": Any, "re": re, "normalize_category": normalize_category,
             "normalize_gst_inclusive": normalize_gst_inclusive, "normalize_gst_rate": normalize_gst_rate,
             "DEFAULT_GST_RATE": DEFAULT_GST_RATE}
helpers = [function(name) for name in ("normalize_sku", "_sheet_sku", "_number", "_sheet_quantity", "_version", "_row", "_public")]
exec(compile(ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[])), "<stock>", "exec"), namespace)

sheet = {
    "_id": "sheet-1", "model_no": "B760-AYW-WIFI", "product_name": "ASUS B760-AYW-WIFI",
    "description": "Motherboard", "price": 10620, "stock": 2, "updated_at": "sheet-v1",
    "source_sheet_ids": ["sheet"], "source_rows": {"sheet": 4},
}
tally = {
    "master_id": "42", "model_number": "b760 ayw wifi", "name": "B760-AYW-WIFI",
    "standard_rate": 10500, "closing_quantity": 7, "unit": "Nos", "alter_id": "9",
}
row = namespace["_row"]([sheet], [tally], namespace["normalize_sku"]("B760-AYW-WIFI"))
assert row["source"] == "both"
assert row["price"] == 10620  # Sheet is authoritative for price.
assert row["quantity"] == 7   # Tally is authoritative for quantity.
assert row["version"] == "sheet-v1|9"
assert namespace["_row"]([sheet], [tally], "b760", False)["quantity"] == 2
assert namespace["_row"]([], [tally], "b760", False)["quantity"] == 7
assert namespace["normalize_sku"](" B760 / AYW_WIFI ") == "b760aywwifi"
assert namespace["_row"]([sheet, {**sheet, "_id": "sheet-2"}], [], "b760")["status"] == "conflict"

route_source = Path("api/stock_routes.py").read_text(encoding="utf-8")
for contract in (
    'router = APIRouter(prefix="/api/stock"', '"/{product_id}/changes"',
    '"/changes/{change_id}/approve"', '"/changes/{change_id}/reject"',
    '"/changes/{change_id}/retry"', '"/tally-link"', '"/sync"',
):
    assert contract in route_source
assert "require_stock_access" in route_source and 'feature_access(user.org_id, "TALLY")' in route_source

tally_source = Path("services/tally_service.py").read_text(encoding="utf-8")
for tag in ("Physical Stock", "BATCHALLOCATIONS.LIST", "GODOWNNAME", "BATCHNAME", "ACTUALQTY"):
    assert tag in tally_source
assert 'command["job_type"] == "stock_change"' in tally_source
assert "after_readback=True" in tally_source
assert 'delivery_unknown=status == "delivery_unknown"' in tally_source

sheet_source = Path("services/product_sheet_sync.py").read_text(encoding="utf-8")
assert "valueRenderOption" in sheet_source and "FORMULA" in sheet_source
assert "values:batchUpdate" in sheet_source and "insertDataOption" in sheet_source
assert "source_rows" in sheet_source and "is_primary" in sheet_source
assert "source_values" in sheet_source
assert "Primary Sheet changed since the last sync" in sheet_source

sheet_routes = Path("api/product_sheets_routes.py").read_text(encoding="utf-8")
assert "unlink_product_sheet_source" in sheet_routes
assert 'f"source_rows.{removed_sheet_id}"' in sheet_source and 'f"source_values.{removed_sheet_id}"' in sheet_source

pricing_source = Path("services/pricing_service.py").read_text(encoding="utf-8")
assert '"event_type": "buyer_product_interest"' in pricing_source

assert "fail_tally_stock_readback" in tally_source

print("stock workspace contract passed")
