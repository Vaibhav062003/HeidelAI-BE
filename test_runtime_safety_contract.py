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


def test_database_bootstrap_gates_lifespan_startup() -> None:
    source = _source("core/lifespan.py")
    lifespan = ast.get_source_segment(source, _function("core/lifespan.py", "lifespan"))
    startup = ast.get_source_segment(source, _function("core/lifespan.py", "_run_startup"))
    assert lifespan.index("await _wait_for_mongo()") < lifespan.index("await _run_startup()") < lifespan.index("yield")
    assert lifespan.index("monitor_task = asyncio.create_task(_monitor_mongo())") < lifespan.index("yield")
    for operation in (
        "ensure_bulk_job_indexes", "ensure_shopify_indexes", "startup_intent_worker",
        "recover_stale_bulk_jobs", "load_prompts_into_cache", "repair_legacy_shopify_connections",
    ):
        assert operation in startup
        assert operation not in lifespan


def test_mongo_release_boundaries() -> None:
    client_source = (ROOT / "database/__init__.py").read_text()
    assert "MONGO_CLIENT_OPTIONS" not in client_source
    assert "AsyncIOMotorClient(MONGODB_URI)" in client_source
    assert "MongoClient(MONGODB_URI)" in client_source
    assert "serverSelectionTimeoutMS=MONGO_PROBE_TIMEOUT_MS" in client_source
    assert "connectTimeoutMS=MONGO_PROBE_TIMEOUT_MS" in client_source
    assert "maxPoolSize=1" in client_source

    main_source = _source("main.py")
    handler = ast.get_source_segment(main_source, _function("main.py", "mongo_unavailable_handler"))
    response = ast.get_source_segment(main_source, _function("main.py", "_mongo_unavailable_response"))
    circuit = ast.get_source_segment(
        main_source,
        _function("main.py", "reject_database_requests_while_mongo_recovers"),
    )
    assert "mark_mongo_unavailable()" in handler
    assert 'status_code=503' in response
    assert '"Retry-After": "10"' in response
    assert '"database_unavailable"' in response
    assert "str(exc)" not in handler
    assert 'path == "/webhook" or path.startswith("/api/")' in circuit
    assert 'request.method != "OPTIONS"' in circuit
    assert "not mongo_retry_allowed()" in circuit


def test_mongo_outage_state_recovers_only_when_marked_available() -> None:
    tree = ast.parse(_source("database/__init__.py"))
    wanted_assignments = {"_mongo_available"}
    nodes = [
        node for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in wanted_assignments for target in node.targets)
        )
        or isinstance(node, ast.FunctionDef)
        and node.name in {"mark_mongo_unavailable", "mark_mongo_available", "mongo_retry_allowed"}
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "database", "exec"), namespace)

    assert not namespace["mongo_retry_allowed"]()
    namespace["mark_mongo_available"]()
    assert namespace["mongo_retry_allowed"]()
    namespace["mark_mongo_unavailable"]()
    assert not namespace["mongo_retry_allowed"]()
    namespace["mark_mongo_available"]()
    assert namespace["mongo_retry_allowed"]()


def test_mongo_startup_uses_one_short_probe_with_backoff() -> None:
    node = copy.deepcopy(_function("core/lifespan.py", "_wait_for_mongo"))
    node.returns = None
    clock = SimpleNamespace(now=0.0)
    delays = []
    attempts = []
    state = []

    class MongoError(Exception):
        pass

    async def to_thread(function):
        return function()

    async def sleep(delay):
        delays.append(delay)
        clock.now += delay

    def probe():
        attempts.append(True)
        if len(attempts) < 3:
            raise MongoError

    namespace = {
        "asyncio": SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now),
            to_thread=to_thread,
            sleep=sleep,
        ),
        "MONGO_BOOT_TIMEOUT_SECONDS": 90,
        "PyMongoError": MongoError,
        "probe_mongo": probe,
        "mark_mongo_available": lambda: state.append("ready"),
        "mark_mongo_unavailable": lambda: state.append("unavailable"),
        "logger": SimpleNamespace(warning=lambda *_: None),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "lifespan", "exec"), namespace)
    asyncio.run(namespace["_wait_for_mongo"]())
    assert len(attempts) == 3
    assert delays == [2, 4]
    assert state == ["unavailable", "unavailable", "ready"]


def test_scheduler_defers_all_jobs_while_mongo_circuit_is_open() -> None:
    source = _source("core/scheduler.py")
    run_job = ast.get_source_segment(source, _function("core/scheduler.py", "_run_mongo_job"))
    add_job = ast.get_source_segment(source, _function("core/scheduler.py", "_add_mongo_job"))
    start = ast.get_source_segment(source, _function("core/scheduler.py", "start_scheduler"))
    assert "if not mongo_retry_allowed()" in run_job
    assert "except PyMongoError" in run_job
    assert "mark_mongo_unavailable()" in run_job
    assert "scheduler.add_job(" in add_job
    assert "scheduler.add_job(" not in start
    assert start.count("_add_mongo_job(") == 18


def test_readiness_fails_closed_on_mongo_timeout() -> None:
    node = copy.deepcopy(_function("api/common_routes.py", "readiness_check"))
    node.decorator_list = []
    node.returns = None
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        arg.annotation = None

    class Response:
        def __init__(self, *, status_code, content):
            self.status_code = status_code
            self.content = content

    namespace = {
        "os": SimpleNamespace(getenv=lambda *_: "revision"),
        "mongo_retry_allowed": lambda: False,
        "JSONResponse": Response,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "ready", "exec"), namespace)
    response = asyncio.run(namespace["readiness_check"](None))
    assert response.status_code == 503
    assert response.content == {
        "status": "not_ready",
        "revision": "revision",
        "dependency": "mongodb",
    }


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
    test_database_bootstrap_gates_lifespan_startup()
    test_mongo_release_boundaries()
    test_mongo_outage_state_recovers_only_when_marked_available()
    test_mongo_startup_uses_one_short_probe_with_backoff()
    test_scheduler_defers_all_jobs_while_mongo_circuit_is_open()
    test_readiness_fails_closed_on_mongo_timeout()
    test_whatsapp_critical_sync_operations_leave_the_event_loop()
    test_webhook_sync_database_operations_leave_the_event_loop()
    test_duplicate_whatsapp_status_returns_the_existing_message()
    print("Runtime safety contracts passed.")
