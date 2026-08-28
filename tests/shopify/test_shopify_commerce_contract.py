#!/usr/bin/env python3
"""Small executable contract for the Shopify trust and delivery boundaries."""

import ast
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = ROOT / "services/shopify_service.py"
ROUTES_PATH = ROOT / "api/shopify_routes.py"
WEBHOOK_PATH = ROOT / "api/webhook_routes.py"
SERVICE = SERVICE_PATH.read_text()
ROUTES = ROUTES_PATH.read_text()
WEBHOOK = WEBHOOK_PATH.read_text()


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


def pure_namespace():
    wanted = {
        "_parse_datetime", "normalize_shop", "normalize_phone", "_phone_and_consent",
        "_record_phone_and_consent", "_phone_key", "_message_preference",
        "_provider_reports_block", "_eligibility_status",
        "_provider_time", "_provider_sort_key", "_reminder_due", "_sync_retry_at",
        "_recovery_activity_time", "_checkout_changed_for_send",
        "_product_summary", "_template_compatible", "_checkout_status_clause", "verify_callback_hmac", "_iso_utc",
    }
    tree = ast.parse(SERVICE)
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    namespace = {
        "Any": object, "datetime": datetime, "timedelta": timedelta, "timezone": timezone, "re": re,
        "HTTPException": HTTPException, "hashlib": hashlib, "hmac": hmac, "json": json,
        "SHOPIFY_CLIENT_SECRET": "contract-secret", "SHOP_RE": re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$"),
        "PHONE_RE": re.compile(r"^\+[1-9]\d{7,14}$"),
        "OPT_OUT_COMMANDS": {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"},
        "OPT_IN_COMMANDS": {"start", "subscribe"},
        "OVERDUE_GRACE_SECONDS": 90,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SERVICE_PATH), "exec"), namespace)
    return namespace


def assert_admin_writes():
    tree = ast.parse(ROUTES)
    write_functions = {
        "shopify_oauth_start", "shopify_create_template", "shopify_settings",
        "shopify_sync", "shopify_disconnect", "shopify_checkout_retry",
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
    assert ns["_eligibility_status"]("+14155550004", [{"name": "Cart item"}]) == "pending"
    assert ns["_message_preference"](" STOP! ") == "opted_out"
    assert ns["_message_preference"]("unsubscribe") == "opted_out"
    assert ns["_message_preference"]("START") == "subscribed"
    assert ns["_message_preference"]("please stop by tomorrow") is None
    assert ns["_provider_reports_block"]({"errors": [{"message": "The user blocked your business"}]})
    assert ns["_provider_reports_block"]({"error": {"message": "The recipient blocked your business"}})
    assert not ns["_provider_reports_block"]({"errors": [{"message": "Template was paused"}]})
    provider_time = datetime(2026, 8, 28, 10, tzinfo=timezone.utc)
    local_status_time = datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
    older_provider_time = datetime(2026, 8, 27, 10, tzinfo=timezone.utc)
    assert ns["_provider_sort_key"]({"provider_id": "new", "provider_updated_at": provider_time, "status_updated_at": older_provider_time}) > ns["_provider_sort_key"]({"provider_id": "old", "provider_updated_at": older_provider_time, "status_updated_at": local_status_time})
    assert ns["_reminder_due"](provider_time, 30) == provider_time + timedelta(minutes=30)
    assert ns["_reminder_due"](provider_time, 30, provider_time + timedelta(hours=24)) == provider_time + timedelta(hours=24)
    enabled_at = provider_time
    assert ns["_recovery_activity_time"](provider_time - timedelta(hours=2), enabled_at) == enabled_at
    assert ns["_recovery_activity_time"](provider_time - timedelta(days=2), enabled_at) is None
    unchanged = {"provider_updated_at": provider_time, "phone": "+14155550004", "products": [{"name": "A"}], "recovery_url": "https://example.test", "completed_at": None, "record_status": "abandoned"}
    assert not ns["_checkout_changed_for_send"](unchanged, dict(unchanged))
    assert ns["_checkout_changed_for_send"](unchanged, {**unchanged, "products": [{"name": "B"}]})
    assert ns["_sync_retry_at"](1, provider_time) == provider_time + timedelta(minutes=1)
    assert ns["_sync_retry_at"](20, provider_time) == provider_time + timedelta(minutes=30)
    assert ns["_iso_utc"](datetime(2026, 8, 28, 13, 2, 14)) == "2026-08-28T13:02:14+00:00"
    assert ns["_checkout_status_clause"]("overdue", provider_time)["reminder_due_at"]["$lte"] == provider_time - timedelta(seconds=90)
    assert ns["_checkout_status_clause"]("accepted", provider_time)["$or"][1]["delivery_status"] == "accepted"
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
        "/api/commerce/shopify/checkouts/retry",
        "/api/conversations/{conversation_id}/shopify-activity",
    ):
        assert endpoint in ROUTES

    for required in (
        'find_one_and_delete({', 'hmac.compare_digest', 'create_index("shop", unique=True',
        'create_index([("shop", 1), ("webhook_id", 1)], unique=True)',
        'create_index([("org_id", 1), ("phone", 1)], unique=True)', '"opted_out"',
        '"automation_enabled_at"', 'timedelta(minutes=5)', '"updated_at"', '"UPDATED_AT"',
        'await _register_webhooks(connection)',
        '"claim_expires_at": now + timedelta(minutes=CLAIM_LEASE_MINUTES)',
        'consent_event = "whatsapp_marketing_consent" in topic',
        'Disconnect the current Shopify store before connecting another',
        '"provider_started_at"', '"delivery_unknown"', '"provider_accepted"',
        'for attempt in range(3)', '"source": "shopify_abandoned_checkout"',
        '"record_type": record_type, "record_id": record_id',
    ):
        assert required in SERVICE, f"missing boundary: {required}"
    assert 'consent != "SUBSCRIBED"' not in SERVICE
    assert 'API_VERSION = "2025-10"' in SERVICE
    assert 'filter_field = "created_at" if kind == "checkouts"' in SERVICE
    assert 'mutation ShopifyWebhook($topic: WebhookSubscriptionTopic!, $uri: String!)' in SERVICE
    assert 'data = await _graphql(connection, CHECKOUT_NODE_QUERY' in SERVICE
    assert SERVICE.index('data = await _graphql(connection, CHECKOUT_NODE_QUERY') < SERVICE.index('"provider_started_at": utcnow(), **frozen')
    assert 'idempotency_key=message_id' in SERVICE
    assert '"reminder_status": {"$in": ["sent", "delivery_unknown"]}' in SERVICE
    assert 'await list_compatible_templates(org_id, connection["whatsapp_id"], phone_id=connection.get("whatsapp_phone_id"))' in SERVICE
    assert 'current, current_connection, current_wa, opted_out = await asyncio.gather' in SERVICE
    assert '"whatsapp_phone_id": wa["phone_id"]' in SERVICE
    assert '"automation_enabled": {"$ne": False}' in SERVICE
    assert 'update_shopify_reminder_delivery' in SERVICE
    assert '"delivery_status": "accepted"' in SERVICE
    assert '"provider_updated_at": checkout.get("provider_updated_at")' in SERVICE
    assert 'await asyncio.to_thread(record_whatsapp_preference' in WEBHOOK
    assert '"customer_updated_at": customer_updated' in SERVICE
    assert 'update = {"provider_updated_at": provider_updated' not in SERVICE
    assert 'headers.get("x-shopify-triggered-at")' in SERVICE
    assert '"record_status": {"$ne": "deleted"}' in SERVICE
    assert '"record_status": "deleted", "provider_updated_at": event_time' in SERVICE
    assert '{"provider_updated_at": {"$lt": provider_time}}' in SERVICE
    assert "await asyncio.to_thread(record_whatsapp_preference, org_id, customer_phone_no, content)" in WEBHOOK
    assert "await asyncio.to_thread(\n                                record_whatsapp_provider_block," in WEBHOOK
    assert 'order_matches.append({"phone"' not in SERVICE
    assert 'asyncio.Semaphore(REMINDER_CONCURRENCY)' in SERVICE
    assert 'asyncio.wait_for(_send_claimed_checkout' in SERVICE
    assert '"inbox_sync_status": "pending"' in SERVICE
    assert '"inbox_sync_status": "complete"' in SERVICE
    assert '"reminder_status": "sent", "inbox_sync_status": {"$in": ["pending", "retryable"]}' in SERVICE

    logger_lines = [line for line in SERVICE.splitlines() if "logger." in line]
    assert not any(word in line for line in logger_lines for word in ("access_token", "recovery_url", "customer_name", "phone", "products", "raw_body"))
    print("SHOPIFY_REMINDER_BEHAVIOR_OK")
    print("SHOPIFY_COMMERCE_CONTRACT_OK")


if __name__ == "__main__":
    main()
