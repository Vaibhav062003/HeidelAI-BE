import ast
from pathlib import Path


wa_routes = Path("api/wa_routes.py").read_text()
webhook_routes = Path("api/webhook_routes.py").read_text()
wa_service = Path("services/wa_service.py").read_text()
lifespan = Path("core/lifespan.py").read_text()


def call_event_payload_keys(source: str) -> list[set[str]]:
    tree = ast.parse(source)
    named_payloads = {
        node.targets[0].id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Dict)
    }
    payloads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "broadcast_main_ws":
            continue
        if len(node.args) < 4 or not isinstance(node.args[2], ast.Constant) or node.args[2].value != "whatsapp_call":
            continue
        payload = named_payloads.get(node.args[3].id) if isinstance(node.args[3], ast.Name) else node.args[3]
        assert isinstance(payload, ast.Dict)
        payloads.append({key.value for key in payload.keys if isinstance(key, ast.Constant)})
    return payloads


assert '{"org_id": user.org_id, "call_id": call_id}' in wa_routes
payloads = call_event_payload_keys(wa_routes) + call_event_payload_keys(webhook_routes)
assert len(payloads) == 4 and all({"id", "call_id"} <= keys for keys in payloads)
assert "len(sdp) > 100_000" in webhook_routes
assert '**safe_call' in webhook_routes
assert '[("org_id", 1), ("call_id", 1)]' in wa_service
assert "unique=True" in wa_service
assert "ensure_whatsapp_call_indexes" in lifespan

print("WhatsApp call backend contract check passed.")
