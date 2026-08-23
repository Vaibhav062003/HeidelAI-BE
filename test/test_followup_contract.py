import ast
from pathlib import Path


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _strings(node: ast.AST):
    return {
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_followup_routes_are_authenticated_and_lifecycle_is_atomic():
    routes = ast.parse(Path("api/ai_routes.py").read_text())
    for name in ["schedule_message", "get_followups", "update_followup_status"]:
        function = _function(routes, name)
        annotations = {argument.arg: argument.annotation for argument in function.args.args}
        assert isinstance(annotations.get("user"), ast.Name)
        assert annotations["user"].id == "CurrentUser"

    service = ast.parse(Path("services/followup_service.py").read_text())
    schedule = _function(service, "schedule_followup_message")
    assert {"org_id", "quote_id", "quote_value", "followup_text", "scheduled_time"} <= _strings(schedule)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(schedule)
    )

    transition = _function(service, "transition_scheduled_followup")
    assert {"org_id", "cancelled", "fulfilled", "pending"} <= _strings(transition)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(transition)
    )

    processor = _function(service, "process_scheduled_messages")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(processor)
    )

    webhook = ast.parse(Path("api/webhook_routes.py").read_text())
    followup_calls = [
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "handle_followup_customer_reply"
    ]
    assert len(followup_calls) >= 5

    background_upload = next(
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_task"
        and node.args
        and getattr(node.args[0], "id", None) == "upload_and_send_whatsapp_media_background"
    )
    assert any(call.lineno > background_upload.lineno for call in followup_calls)


if __name__ == "__main__":
    test_followup_routes_are_authenticated_and_lifecycle_is_atomic()
    print("Follow-up contract check passed.")
