import ast
import asyncio
from pathlib import Path


ROOT = Path(__file__).parent


def _load_webhook_helpers():
    tree = ast.parse((ROOT / "api/webhook_routes.py").read_text(encoding="utf-8"))
    wanted = {
        "pricing_runtime_mode",
        "normalize_whatsapp_message_content",
        "send_pricing_intercept_reply",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "webhook_helpers", "exec"), namespace)
    return namespace


def test_message_content_is_normalized_per_webhook_message():
    normalize = _load_webhook_helpers()["normalize_whatsapp_message_content"]
    assert normalize({"type": "text", "text": {"body": "new text"}}) == "new text"
    assert normalize({"type": "image", "image": {"caption": "  Price ₹50k  "}}) == "Price ₹50k"
    assert normalize({"type": "document", "document": {}}) == "Shared a document"
    assert normalize({"type": "audio", "audio": {}}) == "Shared an audio"
    assert normalize({"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "yes", "title": "Yes"},
    }}) == "Yes"


def test_only_buyer_interception_sends_one_idempotent_holding_reply():
    helper = _load_webhook_helpers()["send_pricing_intercept_reply"]
    calls = []

    async def send(message, org_id, mode, **kwargs):
        calls.append((message, org_id, mode, kwargs))

    helper.__globals__["send_whatsapp_message"] = send
    common = {
        "org_id": "org-1",
        "conversation_id": "whatsapp_919999999999",
        "whatsapp_business_id": "wa-1",
        "customer_phone_no": "919999999999",
        "customer_name": "Buyer",
        "source_message_id": "wamid-1",
    }
    assert asyncio.run(helper({"intercept": True, "reply": None}, **common)) is True
    assert calls == []
    assert asyncio.run(helper({"intercept": False, "reply": "hold"}, **common)) is False
    assert calls == []
    assert asyncio.run(helper({"intercept": True, "reply": "hold"}, **common)) is True
    assert len(calls) == 1
    message, org_id, mode, kwargs = calls[0]
    assert message["content"] == "hold"
    assert org_id == "org-1" and mode == "reply"
    assert kwargs["temp_id"] == "pricing-hold:wamid-1"


def test_webhook_fallback_blocks_only_review_mode():
    helper = _load_webhook_helpers()["pricing_runtime_mode"]

    class Logger:
        def error(self, *_args, **_kwargs):
            return None

    async def settings(mode):
        return {"mode": mode}

    helper.__globals__["logger"] = Logger()
    helper.__globals__["get_pricing_settings"] = lambda _org_id: settings("shadow")
    helper.__globals__["is_pricing_enabled"] = lambda _org_id: True
    assert asyncio.run(helper("org-1")) == "shadow"

    async def unavailable(_org_id):
        raise RuntimeError("database down")

    helper.__globals__["get_pricing_settings"] = unavailable
    assert asyncio.run(helper("org-1")) == "review"
    helper.__globals__["is_pricing_enabled"] = lambda _org_id: False
    assert asyncio.run(helper("org-1")) == "off"

    webhook = (ROOT / "api/webhook_routes.py").read_text(encoding="utf-8")
    sender = (ROOT / "services/wa_service.py").read_text(encoding="utf-8")
    assert 'review_mode and is_price_signal(content)' in webhook
    assert webhook.count("pricing_failure_intercept(") >= 2
    pricing_routes = (ROOT / "api/pricing_routes.py").read_text(encoding="utf-8")
    assert "temp_id: str = Field(min_length=1, max_length=160)" in pricing_routes
    assert "temp_id=payload.temp_id" in pricing_routes
    assert "validate_ai_pricing_response" not in webhook
    assert 'pricing_audit_active = pricing_mode in {"shadow", "review"}' in sender
    assert 'pricing_guard_active = pricing_mode == "review"' in sender


if __name__ == "__main__":
    test_message_content_is_normalized_per_webhook_message()
    test_only_buyer_interception_sends_one_idempotent_holding_reply()
    test_webhook_fallback_blocks_only_review_mode()
    print("Pricing webhook contract: ok")
