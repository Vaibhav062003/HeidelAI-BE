import ast
from pathlib import Path


def _load_filter_helpers():
    tree = ast.parse(Path("services/common_service.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_escalated_condition", "_queue_filter_conditions", "_priority_condition"}
    ]
    namespace = {}
    module = ast.Module(
        body=[ast.ImportFrom(module="typing", names=[ast.alias("Dict"), ast.alias("List"), ast.alias("Optional")], level=0), *functions],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "queue-filter-helpers", "exec"), namespace)
    return namespace["_queue_filter_conditions"], namespace["_priority_condition"]


def test_queue_filters_are_server_scoped():
    filters, priority = _load_filter_helpers()
    assert filters("mine", "agent-1", None, None, None, None) == [
        {"$or": [{"assigned_agent.id": "agent-1"}, {"assigned_agent_id": "agent-1"}]}
    ]
    assert filters("all", None, "unread", "ai", "closed", None) == [
        {"unread_count": {"$gt": 0}},
        {"is_ai_enabled": True},
        {"status": "closed"},
    ]
    assert filters("unassigned", None, None, None, None, None)[0]["$and"][0]["$or"][1] == {"assigned_agent": {}}
    assert priority("medium") == {"$or": [{"priority": {"$in": ["medium"]}}, {"priority": None}]}
    assert priority("high,low") == {"priority": {"$in": ["high", "low"]}}

    route = ast.parse(Path("api/common_routes.py").read_text())
    forwarded = {
        keyword.arg
        for node in ast.walk(route)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "fetch_conversations_new"
        for keyword in node.keywords
    }
    assert {"attention", "handling", "status_filter", "assigned_agent_id"} <= forwarded


if __name__ == "__main__":
    test_queue_filters_are_server_scoped()
    print("Inbox queue filter checks passed.")
