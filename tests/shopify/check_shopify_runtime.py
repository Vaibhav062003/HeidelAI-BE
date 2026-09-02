#!/usr/bin/env python3
"""Compile and wire-check the Shopify runtime without external services."""

import ast
import py_compile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FILES = [
    "api/shopify_routes.py", "services/shopify_service.py", "services/wa_service.py",
    "config/settings.py", "core/lifespan.py", "core/scheduler.py", "api/__init__.py",
]

for relative in FILES:
    path = ROOT / relative
    py_compile.compile(str(path), doraise=True)

api_init = (ROOT / "api/__init__.py").read_text()
lifespan = (ROOT / "core/lifespan.py").read_text()
scheduler = (ROOT / "core/scheduler.py").read_text()
assert "shopify_routes.router" in api_init
assert "await ensure_shopify_indexes()" in lifespan
assert "repair_legacy_shopify_connections()" in lifespan
assert "stop_scheduler" in lifespan
assert "process_shopify_reminders" in scheduler and "process_shopify_syncs" in scheduler
assert "next_run_time=datetime.now(timezone.utc)" in scheduler
ast.parse((ROOT / "services/shopify_service.py").read_text())
print("SHOPIFY_RUNTIME_OK")
