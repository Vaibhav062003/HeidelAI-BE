"""Dependency-free behavioral checks for runtime safety boundaries."""

import ast
import asyncio
import copy
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Dict


ROOT = Path(__file__).parent


def _source(path: str) -> str:
    value = (ROOT / path).read_text(encoding="utf-8")
    ast.parse(value, filename=path)
    return value


def _function(path: str, name: str):
    return next(
        node for node in ast.walk(ast.parse(_source(path)))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def test_rate_keys_separate_bearer_identities_behind_one_proxy() -> None:
    namespace = {
        "hashlib": hashlib,
        "get_remote_address": lambda request: request.remote,
    }
    exec(compile(ast.Module(body=[_function("core/limiter.py", "rate_limit_key")], type_ignores=[]), "limiter", "exec"), namespace)
    key = namespace["rate_limit_key"]

    def request(token: str | None):
        headers = {"authorization": f"Bearer {token}"} if token else {"x-forwarded-for": "203.0.113.8"}
        return SimpleNamespace(state=SimpleNamespace(), headers=headers, remote="10.0.0.5")

    first = key(request("token-one"))
    second = key(request("token-two"))
    assert first != second
    assert "token-one" not in first and "token-two" not in second
    assert key(request(None)) == "ip:10.0.0.5"

    verified = request("token-one")
    verified.state.rate_limit_identity = "auth:org-1:user-1"
    assert key(verified) == "auth:org-1:user-1"

    limiter_source = _source("core/limiter.py")
    auth_source = _source("auth/dependencies.py")
    assert 'default_limits=["100/minute"]' in limiter_source
    assert 'request.state.rate_limit_identity = f"auth:{user.org_id}:{user.user_id}"' in auth_source


def test_control_frames_are_bounded_and_typed() -> None:
    tree = ast.parse(_source("services/websocket_service.py"))
    nodes = [
        node for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id.startswith("MAX_WEBSOCKET_") for target in node.targets)
        )
        or isinstance(node, ast.ClassDef) and node.name == "InvalidWebSocketControlFrame"
        or isinstance(node, ast.FunctionDef) and node.name == "decode_websocket_control_frame"
    ]
    namespace = {"json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "websocket", "exec"), namespace)
    decode = namespace["decode_websocket_control_frame"]
    error_type = namespace["InvalidWebSocketControlFrame"]

    assert decode('{"type":"ping"}') == {"type": "ping"}
    for raw in ('[]', '{"type":1}', "not-json"):
        try:
            decode(raw)
        except error_type as error:
            assert error.close_code == 1003
        else:
            raise AssertionError(f"unsafe control frame accepted: {raw}")
    try:
        decode("x" * (namespace["MAX_WEBSOCKET_CONTROL_FRAME_BYTES"] + 1))
    except error_type as error:
        assert error.close_code == 1009
    else:
        raise AssertionError("oversized control frame accepted")


def test_websocket_identifiers_and_legacy_auth_are_bounded() -> None:
    tree = ast.parse(_source("services/websocket_service.py"))
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "MAX_WEBSOCKET_IDENTIFIER_LENGTH" for target in node.targets)
        or isinstance(node, ast.FunctionDef) and node.name == "validate_websocket_identifier"
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "identifiers", "exec"), namespace)
    validate = namespace["validate_websocket_identifier"]
    assert validate("client_98c0e865-2407-46f2-a4d3") == "client_98c0e865-2407-46f2-a4d3"
    for value in ("", "x" * 129, "line\nbreak", "../../client"):
        try:
            validate(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe identifier accepted: {value!r}")

    routes = _source("api/websockets_routes.py")
    legacy = ast.get_source_segment(routes, _function("api/websockets_routes.py", "websocket_endpoint_whatsapp"))
    summary = ast.get_source_segment(routes, _function("api/websockets_routes.py", "websocket_endpoint"))
    assert legacy.index("await authenticate_credentials(credentials)") < legacy.index("await manager.connect")
    assert '{"user_id": whatsapp_id, "org_id": user.org_id, "is_active": {"$ne": False}}' in legacy
    assert "validate_websocket_identifier(client_id)" in legacy
    assert "validate_websocket_identifier(whatsapp_id)" in legacy
    assert summary.index("await authenticate_credentials(credentials)") < summary.index("await manager2.connect")

    managers = _source("core/managers.py")
    assert managers.count("list(self.active_connections.items())") == 2
    assert managers.count('reason="Client ID already connected"') == 2

    manager_tree = ast.parse(managers)
    manager_nodes = [node for node in manager_tree.body if isinstance(node, ast.ClassDef)]

    class Logger:
        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    class Socket:
        def __init__(self):
            self.closed = None

        async def accept(self, **_):
            return None

        async def close(self, **kwargs):
            self.closed = kwargs

    manager_namespace = {"Dict": Dict, "WebSocket": object, "logger": Logger()}
    exec(compile(ast.Module(body=manager_nodes, type_ignores=[]), "managers", "exec"), manager_namespace)
    manager = manager_namespace["ConnectionManager"]()
    first, collision = Socket(), Socket()
    assert asyncio.run(manager.connect("same-client", first, "protocol")) is True
    assert asyncio.run(manager.connect("same-client", collision, "protocol")) is False
    assert collision.closed == {"code": 1008, "reason": "Client ID already connected"}
    assert manager.active_connections == {"same-client": first}


def test_cleanup_is_idempotent_across_every_client_registry() -> None:
    tree = ast.parse(_source("services/websocket_service.py"))
    wanted = {"_discard_client", "cleanup_client", "leave_conversation_presence"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]

    client_id = "client-1"
    indexed = [defaultdict(set, {"key": {client_id}}) for _ in range(6)]
    services = SimpleNamespace(
        agent_clients=indexed[0],
        org_clients=indexed[1],
        instagram_clients=indexed[2],
        whatsapp_clients=indexed[3],
        instagram_connections_map=indexed[4],
        whatsapp_connections_map=indexed[5],
        org_convo_viewers=defaultdict(lambda: defaultdict(set), {"org-1": defaultdict(set, {"convo-1": {client_id}})}),
        client_convo_map={client_id: ("org-1", "convo-1")},
        active_clients={client_id: object()},
        client_metadata={client_id: {"org_id": "org-1", "user_id": "user-1"}},
        connected_clients={client_id: object()},
    )

    class Manager:
        def __init__(self):
            self.active_connections = {client_id: object()}

        def disconnect(self, value):
            self.active_connections.pop(value, None)

    class Logger:
        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    async def broadcast_org_ws(**_):
        return None

    manager = Manager()
    manager2 = Manager()
    namespace = {
        "services": services,
        "manager": manager,
        "manager2": manager2,
        "logger": Logger(),
        "broadcast_org_ws": broadcast_org_ws,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "cleanup", "exec"), namespace)
    cleanup = namespace["cleanup_client"]
    asyncio.run(cleanup(client_id))
    asyncio.run(cleanup(client_id))

    assert all(not index for index in indexed)
    assert not services.org_convo_viewers
    assert not services.client_convo_map
    assert not services.active_clients
    assert not services.client_metadata
    assert not services.connected_clients
    assert not manager.active_connections and not manager2.active_connections


def test_database_bootstrap_has_no_global_mongo_gate() -> None:
    source = _source("core/lifespan.py")
    lifespan = ast.get_source_segment(source, _function("core/lifespan.py", "lifespan"))
    startup = ast.get_source_segment(source, _function("core/lifespan.py", "_run_startup"))
    assert lifespan.index("await _run_startup()") < lifespan.index("yield")
    assert "probe_mongo" not in source
    assert "_monitor_mongo" not in source
    assert "_wait_for_mongo" not in source
    for operation in (
        "ensure_bulk_job_indexes", "ensure_shopify_indexes", "startup_intent_worker",
        "recover_stale_bulk_jobs", "load_prompts_into_cache", "repair_legacy_shopify_connections",
    ):
        assert operation in startup
        assert operation not in lifespan


def test_mongo_runtime_uses_driver_defaults_without_a_circuit() -> None:
    client_source = (ROOT / "database/__init__.py").read_text()
    assert "AsyncIOMotorClient(MONGODB_URI)" in client_source
    assert "MongoClient(MONGODB_URI)" in client_source
    runtime_paths = [ROOT / "main.py"]
    for directory in ("api", "core", "database", "services"):
        runtime_paths.extend((ROOT / directory).rglob("*.py"))
    runtime_source = "\n".join(path.read_text(encoding="utf-8") for path in runtime_paths)
    for removed in (
        "serverSelectionTimeoutMS", "connectTimeoutMS", "socketTimeoutMS", "maxTimeMS",
        "probe_mongo", "mongo_retry_allowed", "wait_for_mongo_available",
        "mark_mongo_available", "mark_mongo_unavailable",
    ):
        assert removed not in runtime_source


def test_scheduler_runs_jobs_without_a_mongo_circuit() -> None:
    source = _source("core/scheduler.py")
    start = ast.get_source_segment(source, _function("core/scheduler.py", "start_scheduler"))
    assert "_add_mongo_job" not in source
    assert start.count("scheduler.add_job(") == 18


def test_deployments_verify_liveness_and_exact_revision() -> None:
    for workflow in ("deploy-dev.yml", "deploy-main.yml"):
        source = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
        assert "/health" in source
        assert ".status == \"ok\" and .revision == $revision" in source
        assert "/ready" not in source


def test_whatsapp_critical_sync_operations_leave_the_event_loop() -> None:
    node = _function("services/wa_service.py", "send_whatsapp_message")
    threaded = {
        ast.get_source_segment(_source("services/wa_service.py"), call.args[0])
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and ast.get_source_segment(_source("services/wa_service.py"), call.func) == "asyncio.to_thread"
        and call.args
    }
    assert {
        "require_feature",
        "resolve_outbound_whatsapp_connection",
        "claim_reply_manifest",
        "claim_reply",
        "start_provider",
        "record_acceptance",
        "complete_reply",
    } <= threaded


def test_webhook_sync_database_operations_leave_the_event_loop() -> None:
    source = _source("api/webhook_routes.py")
    node = _function("api/webhook_routes.py", "handle_webhook")
    sync_methods = {
        "aggregate", "delete_one", "find", "find_one", "find_one_and_update",
        "insert_one", "list_collection_names", "replace_one", "update_many", "update_one",
    }
    direct = [
        call for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in sync_methods
    ]
    assert not direct

    threaded = {
        ast.get_source_segment(source, call.args[0])
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and ast.get_source_segment(source, call.func) == "asyncio.to_thread"
        and call.args
    }
    assert {
        "resolve_active_whatsapp_connection",
        "claim_instagram_webhook_message",
        "claim_instagram_webhook_downstream",
        "complete_instagram_webhook_message",
        "release_instagram_webhook_downstream",
    } <= threaded


def test_duplicate_whatsapp_status_returns_the_existing_message() -> None:
    node = copy.deepcopy(_function("services/wa_service.py", "update_whatsapp_message_status"))
    node.decorator_list = []
    node.returns = None
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        arg.annotation = None

    existing = {"message_id": "mid", "platform": "whatsapp", "status": "read"}

    class Collection:
        def find_one_and_update(self, *_args, **_kwargs):
            return None

        def find_one(self, *_args, **_kwargs):
            return existing

    class Logger:
        def __getattr__(self, _):
            return lambda *args, **kwargs: None

    namespace = {
        "asyncio": asyncio,
        "db": {"messages_org": Collection()},
        "ReturnDocument": SimpleNamespace(AFTER="after"),
        "ConnectionFailure": OSError,
        "logger": Logger(),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "status", "exec"), namespace)
    result = asyncio.run(namespace["update_whatsapp_message_status"]("org", "mid", "read"))
    assert result == existing


if __name__ == "__main__":
    test_rate_keys_separate_bearer_identities_behind_one_proxy()
    test_control_frames_are_bounded_and_typed()
    test_websocket_identifiers_and_legacy_auth_are_bounded()
    test_cleanup_is_idempotent_across_every_client_registry()
    test_database_bootstrap_has_no_global_mongo_gate()
    test_mongo_runtime_uses_driver_defaults_without_a_circuit()
    test_scheduler_runs_jobs_without_a_mongo_circuit()
    test_deployments_verify_liveness_and_exact_revision()
    test_whatsapp_critical_sync_operations_leave_the_event_loop()
    test_webhook_sync_database_operations_leave_the_event_loop()
    test_duplicate_whatsapp_status_returns_the_existing_message()
    print("Runtime safety contracts passed.")
