#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.stock_gst import DEFAULT_GST_RATE, normalize_gst_inclusive, normalize_gst_rate, select_gst_rows


def source(path: str) -> str:
    return (ROOT / path).read_text()


def check_product() -> None:
    assert DEFAULT_GST_RATE == 18
    assert normalize_gst_inclusive(None) is False
    assert normalize_gst_inclusive("inclusive") is True
    assert normalize_gst_inclusive("exclusive") is False
    assert normalize_gst_rate("18%") == 18
    assert normalize_gst_rate(0) == 0
    assert normalize_gst_rate(True) == 18
    routes = source("api/stock_routes.py")
    models = source("schemas/models.py")
    stock = source("services/stock_service.py")
    assert "gst_inclusive: StrictBool | None = None" in routes
    assert "gst_rate: float | None" in routes
    assert "gst_inclusive: bool = False" in models
    assert "gst_rate: float = Field(default=18" in models
    assert '"gst_inclusive", "gst_rate"' in stock
    print("stock GST product verification passed")


def check_bulk() -> None:
    rows = [{"category": "GPU"}, {"category": "gpu"}, {"category": "CPU"}, {"category": ""}]
    assert len(select_gst_rows(rows, "all")) == 4
    assert len(select_gst_rows(rows, "category", " GPU ")) == 2
    assert select_gst_rows(rows, "category", "Uncategorised") == [rows[3]]
    try:
        select_gst_rows(rows, "visible-page")
        raise AssertionError("invalid scope was accepted")
    except ValueError:
        pass
    stock = source("services/stock_service.py")
    routes = source("api/stock_routes.py")
    sheet = source("services/product_sheet_sync.py")
    assert "if role not in ADMIN_ROLES" in stock
    assert '@router.put("/gst")' in routes
    assert 'Literal["all", "category"]' in routes
    assert "duplicate primary-Sheet rows" in sheet
    assert "formula-protected GST cell" in sheet
    print("stock GST bulk verification passed")


def check_sheet() -> None:
    sheet = source("services/product_sheet_sync.py")
    bulk = sheet.split("async def write_product_sheet_gst_bulk", 1)[1]
    assert '"GST Inclusive"' in sheet and '"GST Rate"' in sheet
    assert '"gst_inclusive": changes.get("gst_inclusive")' in sheet
    assert '"gst_rate": changes.get("gst_rate")' in sheet
    assert 'for field, value in (("gst_inclusive", gst_inclusive), ("gst_rate", gst_rate))' in bulk
    assert "_sheet_value_equal(field, current_value, value)" in bulk
    assert "await sync_product_sheet(org_id, sheet_id)" in bulk
    print("stock GST sheet verification passed")


checks = {"product": check_product, "bulk": check_bulk, "sheet": check_sheet}
mode = sys.argv[1] if len(sys.argv) > 1 else ""
if mode not in checks:
    raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(checks)}")
checks[mode]()
