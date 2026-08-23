import ast
from pathlib import Path


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _load_function(path: str, name: str, namespace: dict):
    tree = ast.parse(Path(path).read_text())
    node = _function(tree, name)
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), path, "exec"), namespace)
    return namespace[name]


def test_reference_ids_are_limited_and_unique():
    normalize = _load_function(
        "api/transfer_routes.py",
        "normalize_referenced_message_ids",
        {"List": list, "HTTPException": FakeHTTPException},
    )
    assert normalize(["m1", "m2", "m3", "m4", "m5"]) == ["m1", "m2", "m3", "m4", "m5"]
    for invalid in (["m1"] * 2, ["m1", "m2", "m3", "m4", "m5", "m6"], [""]):
        try:
            normalize(invalid)
        except FakeHTTPException as error:
            assert error.status_code == 422
        else:
            raise AssertionError(f"Expected invalid reference list to fail: {invalid}")


def test_references_are_conversation_scoped_and_persisted():
    source = Path("api/transfer_routes.py").read_text()
    tree = ast.parse(source)
    validator = _function(tree, "require_referenced_messages")
    validator_constants = {
        node.value for node in ast.walk(validator)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"conversation_id", "messages_", "Every referenced message must belong to this conversation"} <= validator_constants
    transfer = _function(tree, "transfer_conversation")
    transfer_constants = {
        node.value for node in ast.walk(transfer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"referenced_message_ids", "last_assignment_action", "transferred"} <= transfer_constants
    work_source = Path("services/work_team_service.py").read_text()
    assert '"referenced_message_ids"' in work_source
    assert '"active_work.$": embedded' in work_source
    assert '(entry.get("to_team") or {}).get("id")' in source


def test_transferred_queue_requires_current_owner_and_real_latest_transfer():
    transferred = _load_function(
        "services/common_service.py",
        "_transferred_condition",
        {"Optional": object, "Dict": dict},
    )
    condition = transferred("agent-1")
    assert condition["$and"][0]["$or"] == [
        {"assigned_agent.id": "agent-1"},
        {"assigned_agent_id": "agent-1"},
    ]
    assert {"last_assignment_action": "transferred"} in condition["$and"]
    assert {"last_transfer_to_agent_id": "agent-1"} in condition["$and"]
    assert {"last_transfer_from_agent_id": {"$nin": [None, "agent-1", "system"]}} in condition["$and"]


def test_notifications_are_recipient_scoped_and_expire():
    routes = Path("api/transfer_routes.py").read_text()
    assert '"org_id": user.org_id, "recipient_id": user.user_id' in routes
    assert '"recipient_id": recipient_id if recipient_id in notify_recipients else None' in routes
    assert 'now + timedelta(days=30)' in routes
    indexes = Path("services/transfer_notification_service.py").read_text()
    assert 'notifications.create_index("expires_at", expireAfterSeconds=0)' in indexes
    assert '("org_id", 1), ("recipient_id", 1)' in indexes


if __name__ == "__main__":
    test_reference_ids_are_limited_and_unique()
    test_references_are_conversation_scoped_and_persisted()
    test_transferred_queue_requires_current_owner_and_real_latest_transfer()
    test_notifications_are_recipient_scoped_and_expire()
    print("transfer handoff backend contract passed")
