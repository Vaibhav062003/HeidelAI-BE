#!/usr/bin/env python3
"""Small executable contract for the Shopify trust and delivery boundaries."""

import ast
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = ROOT / "services/shopify_service.py"
ROUTES_PATH = ROOT / "api/shopify_routes.py"
SERVICE = SERVICE_PATH.read_text()
ROUTES = ROUTES_PATH.read_text()


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def pure_namespace():
    wanted = {
        "_parse_datetime", "normalize_shop", "normalize_phone", "_phone_and_consent",
        "_record_phone_and_consent",
        "_product_summary", "_template_compatible", "verify_callback_hmac",
    }
    tree = ast.parse(SERVICE)
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    namespace = {
        "Any": object, "datetime": datetime, "timezone": timezone, "re": re,
        "HTTPException": HTTPException, "hashlib": hashlib, "hmac": hmac, "json": json,
        "SHOPIFY_CLIENT_SECRET": "contract-secret", "SHOP_RE": re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$"),
        "PHONE_RE": re.compile(r"^\+[1-9]\d{7,14}$"),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SERVICE_PATH), "exec"), namespace)
    return namespace


def assert_admin_writes():
    tree = ast.parse(ROUTES)
    write_functions = {
        "shopify_oauth_start", "shopify_create_template", "shopify_settings",
        "shopify_sync", "shopify_disconnect",
    }
    functions = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    for name in write_functions:
        assert "admin_required" in ast.unparse(functions[name]), f"{name} must require an organization admin"


def main():
    compile(SERVICE, str(SERVICE_PATH), "exec")
    compile(ROUTES, str(ROUTES_PATH), "exec")
    ns = pure_namespace()

    assert ns["normalize_shop"]("HTTPS://demo-store.myshopify.com/") == "demo-store.myshopify.com"
    for invalid in ("shopify.com", "demo.myshopify.com.evil.test", "demo.myshopify.com@evil.test"):
        try:
            ns["normalize_shop"](invalid)
            raise AssertionError("invalid Shopify domain accepted")
        except HTTPException:
            pass
    assert ns["normalize_phone"]("+91 98765-43210") == "+919876543210"
    assert ns["normalize_phone"]("9876543210") == ""

    phone, consent, changed = ns["_phone_and_consent"]({
        "customer_id": 7,
        "phone_number": "+14155552671",
        "whats_app_marketing_consent": {"state": "subscribed", "consent_updated_at": "2026-08-28T10:00:00Z"},
    })
    assert (phone, consent) == ("+14155552671", "SUBSCRIBED") and changed is not None
    resolve_phone = ns["_record_phone_and_consent"]
    assert resolve_phone(
        {"defaultPhoneNumber": {"phoneNumber": "+14155550001"}},
        {"phone": "+14155550002"}, {"phone": "+14155550003"}, "+14155550004",
    )[0] == "+14155550001"
    assert resolve_phone({}, {"phone": "+14155550002"}, {"phone": "+14155550003"})[0] == "+14155550002"
    assert resolve_phone({}, {}, {"phone": "+14155550003"})[0] == "+14155550003"
    assert resolve_phone({}, {}, {}, "+14155550004")[0] == "+14155550004"
    assert SERVICE.count("shippingAddress { phone }") >= 2
    assert SERVICE.count("billingAddress { phone }") >= 2
    assert ns["_product_summary"]([{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]) == "A, B, C +1 more"
    assert ns["_template_compatible"]({"status": "APPROVED", "category": "MARKETING", "components": [{"type": "BODY", "text": "Hi {{1}}: {{2}} {{3}}"}]})
    assert not ns["_template_compatible"]({"status": "APPROVED", "category": "UTILITY", "components": [{"type": "BODY", "text": "{{1}} {{2}} {{3}}"}]})
    assert not ns["_template_compatible"]({"status": "APPROVED", "category": "MARKETING", "components": [{"type": "HEADER", "text": "{{1}}"}, {"type": "BODY", "text": "{{1}} {{2}} {{3}}"}]})

    params = {"shop": "demo.myshopify.com", "code": "abc", "state": "xyz", "timestamp": "1"}
    canonical = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
    params["hmac"] = hmac.new(b"contract-secret", canonical.encode(), hashlib.sha256).hexdigest()
    ns["verify_callback_hmac"](params)

    assert_admin_writes()
    for endpoint in (
        "/api/integrations/shopify", "/api/integrations/shopify/oauth/start",
        "/api/integrations/shopify/oauth/callback", "/api/integrations/shopify/settings",
        "/api/integrations/shopify/sync", "/api/webhooks/shopify",
        "/api/commerce/shopify/summary", "/api/commerce/shopify/records",
        "/api/conversations/{conversation_id}/shopify-activity",
    ):
        assert endpoint in ROUTES

    for required in (
        'find_one_and_delete({', 'hmac.compare_digest', 'create_index("shop", unique=True',
        'create_index([("shop", 1), ("webhook_id", 1)], unique=True)',
        'CUSTOMERS_WHATS_APP_MARKETING_CONSENT_UPDATE', 'consent != "SUBSCRIBED"',
        '"claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES)',
        'consent_event = "whatsapp_marketing_consent" in topic',
        'Disconnect the current Shopify store before connecting another',
        '"provider_started_at"', '"delivery_unknown"', '"provider_accepted"',
        'for attempt in range(3)', '"source": "shopify_abandoned_checkout"',
        '"record_type": record_type, "record_id": record_id',
    ):
        assert required in SERVICE, f"missing boundary: {required}"
    assert 'order_matches.append({"phone"' not in SERVICE

    logger_lines = [line for line in SERVICE.splitlines() if "logger." in line]
    assert not any(word in line for line in logger_lines for word in ("access_token", "recovery_url", "customer_name", "phone", "products", "raw_body"))
    print("SHOPIFY_COMMERCE_CONTRACT_OK")


if __name__ == "__main__":
    main()
