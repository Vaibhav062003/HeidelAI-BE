import ast
import base64
import hashlib
import hmac
from pathlib import Path


def test_fcm_payload_contains_customer_preview_without_internal_ids() -> None:
    source = Path("services/push_notification_service.py").read_text()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_fcm_message"
    )
    namespace: dict = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<payload>", "exec"), namespace)
    payload = namespace["build_fcm_message"](
        "device-token", "a" * 32, "Vinayaka Computers", "Need the RTX 3050 price"
    )["message"]

    assert payload["data"] == {"kind": "customer_message", "event_id": "a" * 32}
    assert payload["notification"] == {
        "title": "Vinayaka Computers",
        "body": "Need the RTX 3050 price",
    }
    serialized = str(payload).lower()
    for forbidden in ("conversation_id", "phone", "message_text", "org_id"):
        assert forbidden not in serialized


def test_customer_preview_is_read_from_the_durable_event() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_clean_notification_text", "notification_content"}
    ]
    namespace = {"ATTACHMENT_LABELS": {}}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<preview>", "exec"), namespace)
    title, body = namespace["notification_content"]({
        "notification_title": "Vinayaka Computers",
        "notification_body": "Need the RTX 3050 price",
    })
    assert (title, body) == ("Vinayaka Computers", "Need the RTX 3050 price")


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


def test_device_removal_is_user_scoped_across_organization_switches() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "deactivate_user_installation"
    )
    writes: list[dict] = []

    class Devices:
        @staticmethod
        def update_many(query: dict, _update: dict) -> None:
            writes.append(query)

    class Clock:
        @staticmethod
        def now(_timezone):
            return "now"

    namespace = {
        "devices": Devices(),
        "datetime": Clock,
        "timezone": type("Timezone", (), {"utc": object()})(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<push-handoff>", "exec"), namespace)
    namespace["deactivate_user_installation"](user_id="user-a", installation_id="device-a")
    assert writes == [{"user_id": "user-a", "installation_id": "device-a"}]


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
    assert any(
        isinstance(call.func, ast.Attribute) and call.func.attr == "put_nowait"
        for call in calls
    )


def test_immediate_push_delivery_is_bounded_and_lifecycle_managed() -> None:
    source = Path("services/push_notification_service.py").read_text()
    tree = ast.parse(source)
    start = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_push_notification_workers"
    )
    queue_calls = [
        node for node in ast.walk(start)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "Queue"
    ]
    assert len(queue_calls) == 1
    assert any(keyword.arg == "maxsize" for keyword in queue_calls[0].keywords)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_done_callback"
        for node in ast.walk(tree)
    )
    stop = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "stop_push_notification_workers"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        for node in ast.walk(stop)
    )
    lifecycle = Path("core/lifespan.py").read_text()
    assert "(\"push notification workers\", start_push_notification_workers)" in lifecycle
    assert "await stop_push_notification_workers()" in lifecycle


def test_new_event_reaches_an_immediate_worker() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    names = {
        "schedule_customer_message_push",
        "_immediate_push_worker",
        "_spawn_immediate_push_worker",
        "_immediate_push_worker_finished",
        "start_push_notification_workers",
        "stop_push_notification_workers",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name in names
    ]
    delivered: list[str] = []
    enqueued: dict = {}
    delivered_event = __import__("asyncio").Event()
    allow_delivery = __import__("asyncio").Event()

    async def process_push_event(event_id: str) -> None:
        delivered.append(event_id)
        delivered_event.set()
        await allow_delivery.wait()

    class Logger:
        @staticmethod
        def warning(_message: str) -> None:
            pass

    namespace = {
        "asyncio": __import__("asyncio"),
        "enqueue_customer_message_push": lambda **kwargs: enqueued.update(kwargs) or "event-test",
        "process_push_event": process_push_event,
        "logger": Logger(),
        "_clean_notification_text": lambda value, *, limit, fallback: str(value or fallback)[:limit],
        "ATTACHMENT_LABELS": {"image": "Sent a photo"},
        "PUSH_QUEUE_SIZE": 2,
        "PUSH_WORKER_COUNT": 1,
        "PUSH_SHUTDOWN_TIMEOUT_SECONDS": 1,
        "_immediate_queue": None,
        "_immediate_workers": set(),
        "_stopping_immediate_workers": False,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "<workers>", "exec"), namespace)

    async def run() -> None:
        await namespace["start_push_notification_workers"]()
        first_worker = next(iter(namespace["_immediate_workers"]))
        first_worker.cancel()
        await __import__("asyncio").gather(first_worker, return_exceptions=True)
        await __import__("asyncio").sleep(0)
        assert len(namespace["_immediate_workers"]) == 1
        assert first_worker not in namespace["_immediate_workers"]
        await namespace["schedule_customer_message_push"](
            org_id="org_test",
            platform="whatsapp",
            message_id="message_test",
            conversation_id="conversation_test",
            sender_name="Vinayaka Computers",
            content="Ryzen 5 5500 build",
            message_type="image",
        )
        await __import__("asyncio").wait_for(delivered_event.wait(), timeout=1)
        stop = __import__("asyncio").create_task(namespace["stop_push_notification_workers"]())
        await __import__("asyncio").sleep(0)
        assert not stop.done()
        allow_delivery.set()
        await stop

    __import__("asyncio").run(run())
    assert delivered == ["event-test"]
    assert enqueued["notification_title"] == "Vinayaka Computers"
    assert enqueued["notification_body"] == "Ryzen 5 5500 build"


def test_interrupted_claim_is_released_for_immediate_retry() -> None:
    tree = ast.parse(Path("services/push_notification_service.py").read_text())
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_release_push_claim"
    )
    captured: dict = {}

    class Events:
        def update_one(self, query, update):
            captured.update(query=query, update=update)

    class Logger:
        @staticmethod
        def warning(_message: str) -> None:
            pass

    namespace = {
        "asyncio": __import__("asyncio"),
        "events": Events(),
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "logger": Logger(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<claim-release>", "exec"), namespace)
    __import__("asyncio").run(namespace["_release_push_claim"]("event-test", "claim-test"))
    assert captured["query"] == {
        "event_id": "event-test",
        "claim_id": "claim-test",
        "status": "sending",
    }
    assert captured["update"]["$set"]["status"] == "pending"
    assert captured["update"]["$unset"] == {"claim_id": "", "claimed_at": ""}


def test_media_push_carries_preview_before_background_upload() -> None:
    tree = ast.parse(Path("api/webhook_routes.py").read_text())
    stores = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "store_whatsapp_message"
        and any(
            keyword.arg == "notify_push"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
    ]
    upload = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_task"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "upload_and_send_whatsapp_media_background"
    )
    assert any(store.lineno < upload.lineno and upload.lineno - store.lineno < 50 for store in stores)


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
        "_process_push_event",
        "process_push_event",
        "_release_push_claim",
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
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_push_event"
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
    test_fcm_payload_contains_customer_preview_without_internal_ids()
    test_customer_preview_is_read_from_the_durable_event()
    test_device_registration_cannot_choose_its_org_or_user()
    test_device_removal_is_user_scoped_across_organization_switches()
    test_only_explicit_token_errors_are_permanent()
    test_clerk_webhook_signature_verification()
    test_registration_checks_durable_membership_revocation_before_and_after_write()
    test_push_enqueue_is_durable_without_unbounded_tasks()
    test_immediate_push_delivery_is_bounded_and_lifecycle_managed()
    test_new_event_reaches_an_immediate_worker()
    test_interrupted_claim_is_released_for_immediate_retry()
    test_media_push_carries_preview_before_background_upload()
    test_async_push_jobs_do_not_call_pymongo_directly()
    test_one_http_client_is_reused_for_each_push_event()
    test_pending_push_batch_isolates_event_failures()
    print("Push notification payload contract is safe.")
