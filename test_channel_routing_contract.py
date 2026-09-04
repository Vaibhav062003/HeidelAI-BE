"""Small regression check for organization-scoped outbound channel routing."""

import ast
from pathlib import Path


ROOT = Path(__file__).parent


def function_node(path: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / path).read_text())
    return next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def strings(node: ast.AST) -> set[str]:
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)}


def calls(node: ast.AST) -> set[str]:
    return {
        item.func.id for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }


def test_channel_routing_is_tenant_scoped() -> None:
    router = strings(function_node("services/common_service.py", "send_message"))
    assert {"whatsapp_id", "instagram_id", "org_id", "is_active"} <= router
    assert "Conversation not found for this organization and channel" in router

    resolver = strings(function_node("services/wa_service.py", "resolve_outbound_whatsapp_connection"))
    assert {"org_id", "wa_id", "phone_id", "is_active"} <= resolver
    for name in ("send_whatsapp_message", "send_whatsapp_template_message", "send_wa_media_message"):
        assert "resolve_outbound_whatsapp_connection" in calls(
            function_node("services/wa_service.py", name)
        ), name

    auth = strings(function_node("api/wa_routes.py", "whatsapp_auth_exchange"))
    assert "is_active" in auth


if __name__ == "__main__":
    test_channel_routing_is_tenant_scoped()
    print("channel routing contract: ok")
