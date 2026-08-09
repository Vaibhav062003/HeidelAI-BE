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

    service = ast.parse(Path("services/scheduler_service.py").read_text())
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

    scheduler = ast.parse(Path("core/scheduler.py").read_text())
    processor = _function(scheduler, "process_scheduled_messages")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(processor)
    )


if __name__ == "__main__":
    test_followup_routes_are_authenticated_and_lifecycle_is_atomic()
    print("Follow-up contract check passed.")
