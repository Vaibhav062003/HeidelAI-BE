"""Regression check for bounded, organization-scoped AI handover."""

import ast
from pathlib import Path


ROOT = Path(__file__).parent


def function_node(path: str, name: str) -> ast.AST:
    tree = ast.parse((ROOT / path).read_text())
    return next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def strings(node: ast.AST) -> set[str]:
    return {item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)}


def test_ai_policy_contract() -> None:
    policy = (ROOT / "services/ai_policy_service.py").read_text()
    assert "MIN_PAUSE_MINUTES = 5" in policy
    assert "MAX_PAUSE_MINUTES = 24 * 60" in policy
    assert 'conversation_id.startswith(("whatsapp_", "instagram_"))' in policy
    assert 'db[f"conversations_{org_id}"]' in policy
    assert 'action == "pause_indefinitely"' in policy
    assert '"$type": "date"' in policy
    assert '"number_disabled"' in policy
    assert '"ai_disabled_whatsapp_phone_ids"' in policy
    assert '"whatsapp_enabled_phone_ids"' in policy

    put_policy = function_node("api/ai_routes.py", "put_ai_policy")
    assert any(isinstance(node, ast.Name) and node.id == "admin_required" for node in ast.walk(put_policy))
    assert "whatsapp_enabled_phone_ids" in (ROOT / "api/ai_routes.py").read_text()
    assert '"pause_indefinitely"' in (ROOT / "api/ai_routes.py").read_text()

    scheduler = function_node("core/scheduler.py", "update_ai_status")
    names = {node.id for node in ast.walk(scheduler) if isinstance(node, ast.Name)}
    assert "resume_expired_ai_pauses" in names
    assert "send_to_agent" not in names
    assert "send_whatsapp_message" not in names

    for path, name in (
        ("api/wa_routes.py", "toggle_whatsapp_ai_status"),
        ("api/ig_routes.py", "toggle_instagram_ai_status"),
    ):
        assert "enabled is required." in strings(function_node(path, name))

    instagram_status = strings(function_node("api/ig_routes.py", "get_instagram_ai_status"))
    assert "instagram_id" not in instagram_status


if __name__ == "__main__":
    test_ai_policy_contract()
    print("AI policy contract: ok")
