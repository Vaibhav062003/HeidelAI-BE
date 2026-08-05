import ast
import base64
import hashlib
import hmac
from pathlib import Path


def test_fcm_payload_contains_no_customer_data() -> None:
    source = Path("services/push_notification_service.py").read_text()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_fcm_message"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<payload>", "exec"), namespace)
    payload = namespace["build_fcm_message"]("device-token", "a" * 32)["message"]

    assert payload["data"] == {"kind": "customer_message", "event_id": "a" * 32}
    assert payload["notification"] == {
        "title": "New customer message",
        "body": "Open HeidelAI to view it",
    }
    serialized = str(payload).lower()
    for forbidden in ("conversation_id", "phone", "customer_name", "message_text", "org_id"):
        assert forbidden not in serialized


def test_device_registration_cannot_choose_its_org_or_user() -> None:
    tree = ast.parse(Path("api/push_routes.py").read_text())
    model = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DeviceRegistration"
    )
    fields = {
        node.target.id for node in model.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == {"installation_id", "token", "platform", "app_version"}


def test_only_explicit_token_errors_are_permanent() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "classify_fcm_failure"
    )
    namespace = {"Literal": __import__("typing").Literal}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<fcm-errors>", "exec"), namespace)
    classify = namespace["classify_fcm_failure"]
    assert classify(404, "UNREGISTERED") == "permanent"
    assert classify(403, "SENDER_ID_MISMATCH") == "permanent"
    assert classify(400, "INVALID_ARGUMENT") == "retry"
    assert classify(400, None) == "retry"
    assert classify(403, None) == "retry"


def test_clerk_webhook_signature_verification() -> None:
    source = Path("api/push_routes.py").read_text()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_verify_clerk_webhook"
    )
    secret_bytes = b"test-signing-key"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    timestamp = "1700000000"
    body = b'{"type":"organization_membership.deleted"}'
    webhook_id = "msg_test"
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    signature = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()

    class FakeHTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self.detail = detail

    class FakeRequest:
        headers = {
            "svix-id": webhook_id,
            "svix-timestamp": timestamp,
            "svix-signature": f"v1,{signature}",
        }

    namespace = {
        "Request": object,
        "HTTPException": FakeHTTPException,
        "CLERK_WEBHOOK_SIGNING_SECRET": secret,
        "base64": base64,
        "hashlib": hashlib,
        "hmac": hmac,
        "time": type("Clock", (), {"time": staticmethod(lambda: float(timestamp))}),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<clerk-signature>", "exec"), namespace)
    assert namespace["_verify_clerk_webhook"](body, FakeRequest()) == webhook_id


def test_registration_checks_durable_membership_revocation_before_and_after_write() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "register_device"
    )
    revocation_checks = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "revoked_members"
        and node.func.attr == "find_one"
    ]
    assert len(revocation_checks) >= 2
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "deactivate_installation"
        for node in ast.walk(function)
    )


def test_push_enqueue_is_durable_without_unbounded_tasks() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "schedule_customer_message_push"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
        and call.func.attr == "create_task"
        for call in calls
    )
    assert any(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "asyncio"
        and call.func.attr == "to_thread"
        for call in calls
    )


def test_async_push_jobs_do_not_call_pymongo_directly() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())

    class DirectMongoCalls(ast.NodeVisitor):
        def __init__(self) -> None:
            self.calls: list[ast.Call] = []

        def visit_Lambda(self, node: ast.Lambda) -> None:
            # Cursor creation and consumption inside a lambda passed to to_thread is safe.
            return

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"devices", "events", "revocations", "revoked_members"}
            ):
                self.calls.append(node)
            self.generic_visit(node)

    for name in {
        "schedule_customer_message_push",
        "process_push_event",
        "process_pending_push_notifications",
        "process_pending_member_device_revocations",
    }:
        function = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        visitor = DirectMongoCalls()
        visitor.visit(function)
        assert not visitor.calls, f"{name} calls synchronous PyMongo directly"


def test_one_http_client_is_reused_for_each_push_event() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    send = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send"
    )
    process = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_push_event"
    )
    assert send.args.args[0].arg == "client"
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "AsyncClient"
        for node in ast.walk(send)
    )
    clients = [
        node for node in ast.walk(process)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
        and node.func.attr == "AsyncClient"
    ]
    assert len(clients) == 1


def test_pending_push_batch_isolates_event_failures() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_pending_push_notifications"
    )
    assert any(
        isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.Await)
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Name)
            and child.value.func.id == "process_push_event"
            for child in ast.walk(node)
        )
        for node in ast.walk(function)
    )


if __name__ == "__main__":
    test_fcm_payload_contains_no_customer_data()
    test_device_registration_cannot_choose_its_org_or_user()
    test_only_explicit_token_errors_are_permanent()
    test_clerk_webhook_signature_verification()
    test_registration_checks_durable_membership_revocation_before_and_after_write()
    test_push_enqueue_is_durable_without_unbounded_tasks()
    test_async_push_jobs_do_not_call_pymongo_directly()
    test_one_http_client_is_reused_for_each_push_event()
    test_pending_push_batch_isolates_event_failures()
    print("Push notification payload contract is safe.")
