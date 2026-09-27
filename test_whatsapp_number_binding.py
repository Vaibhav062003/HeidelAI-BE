import ast
import asyncio
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone, timedelta
from typing import Optional

for key in (
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_PROJECT",
    "AZURE_OPENAI_SUBSCRIPTION_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_DEPLOYMENT",
    "MONGODB_CLUSTER",
    "PINECONE_API_KEY",
    "PINECONE_INDEX_NAME",
):
    os.environ.setdefault(key, "test")

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")

# Keep this contract runnable in the repo's minimal local virtualenv.
database_stub = ModuleType("database")
database_stub.get_async_mongo_db = lambda: None
database_stub.get_async_mongo_client = lambda: None
sys.modules["database"] = database_stub
loguru_stub = ModuleType("loguru")
loguru_stub.logger = SimpleNamespace(
    info=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules["loguru"] = loguru_stub
utils_stub = ModuleType("services.utils")
utils_stub.serialize_mongo = lambda value: value
sys.modules["services.utils"] = utils_stub

from services import wa_bot_service


ROOT = Path(__file__).parent


def _matches(document: dict, query: dict) -> bool:
    for field, expected in query.items():
        if field == "$or":
            if not any(_matches(document, option) for option in expected):
                return False
            continue
        actual = document.get(field)
        if isinstance(expected, dict):
            if "$exists" in expected and (field in document) != bool(expected["$exists"]):
                return False
            if "$lt" in expected and (actual is None or actual >= expected["$lt"]):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class SyncCursor:
    def __init__(self, documents):
        self.documents = documents

    def limit(self, length):
        self.documents = self.documents[:length]
        return self

    def sort(self, fields):
        for field, direction in reversed(fields):
            self.documents.sort(key=lambda item: item.get(field) or 0, reverse=direction < 0)
        return self

    def __iter__(self):
        return iter(self.documents)


class SyncConnections:
    def __init__(self, documents):
        self.documents = documents

    def find_one(self, query):
        return next((document for document in self.documents if _matches(document, query)), None)

    def find(self, query):
        return SyncCursor([document for document in self.documents if _matches(document, query)])


class SyncDB:
    def __init__(self, documents):
        self.whatsapp_connections = SyncConnections(documents)


class FakeHTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class FakeRequestError(Exception):
    pass


class FakeTimeoutError(FakeRequestError):
    pass


def _load_route_helpers():
    tree = ast.parse((ROOT / "api/wa_routes.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_find_owned_whatsapp_connection", "_fetch_meta_details_resource", "_whatsapp_reconnect_identity",
        }
    ]
    namespace = {
        "HTTPException": FakeHTTPException,
        "httpx": SimpleNamespace(RequestError=FakeRequestError, TimeoutException=FakeTimeoutError),
        "logger": SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "route_helpers", "exec"), namespace)
    return namespace


def test_details_helpers_are_tenant_scoped_and_map_meta_failures():
    namespace = _load_route_helpers()
    reconnect_identity = namespace["_whatsapp_reconnect_identity"]("new-phone", "same-waba")
    assert reconnect_identity == {"$or": [
        {"wa_id": "same-waba"}, {"waba_id": "same-waba"},
        {"phone_id": "new-phone"}, {"phone_number_id": "new-phone"},
    ]}
    namespace["db"] = SyncDB([
        {"org_id": "org-1", "phone_id": "phone", "is_active": True},
        {"org_id": "org-2", "phone_id": "other", "is_active": True},
        {"org_id": "org-1", "phone_id": "inactive", "is_active": False},
    ])
    find_owned = namespace["_find_owned_whatsapp_connection"]
    assert find_owned("org-1", connection_id="phone")["phone_id"] == "phone"
    assert find_owned("org-1", connection_id="other") is None
    assert find_owned("org-1", connection_id="inactive") is None

    fetch = namespace["_fetch_meta_details_resource"]

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.is_success = 200 <= status_code < 300
            self.payload = payload or {}
        def json(self):
            return self.payload

    class Client:
        def __init__(self, result): self.result = result
        async def get(self, *_args, **_kwargs):
            if isinstance(self.result, Exception): raise self.result
            return self.result

    async def call(result, *, required):
        warnings = []
        data = await fetch(Client(result), "url", {}, {}, "account", "org-1", warnings, required=required)
        return data, warnings

    try:
        asyncio.run(call(Response(400, {"error": {"code": 190}}), required=True))
        raise AssertionError("expired credentials must fail")
    except FakeHTTPException as exc:
        assert exc.status_code == 409
    try:
        asyncio.run(call(FakeTimeoutError(), required=True))
        raise AssertionError("a required Meta timeout must fail explicitly")
    except FakeHTTPException as exc:
        assert exc.status_code == 504
    try:
        asyncio.run(call(Response(503), required=True))
        raise AssertionError("a required Meta account failure must fail explicitly")
    except FakeHTTPException as exc:
        assert exc.status_code == 502
    assert asyncio.run(call(Response(503), required=False)) == ({}, ["account unavailable"])
    assert asyncio.run(call(Response(200, {"id": "waba"}), required=True)) == ({"id": "waba"}, [])


def _load_connection_resolver():
    tree = ast.parse((ROOT / "services/wa_service.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {
            "_identifier_values", "active_whatsapp_connection_candidates", "resolve_active_whatsapp_connection",
        }
    ]
    namespace = {"Optional": Optional, "MAX_WHATSAPP_CONNECTIONS": 50}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "resolver", "exec"), namespace)
    return namespace["resolve_active_whatsapp_connection"], namespace


def test_inbound_connection_resolution_is_exact_and_ambiguity_safe():
    resolver, namespace = _load_connection_resolver()
    documents = [
        {"org_id": "org", "wa_id": "waba", "phone_id": "phone-1", "is_active": True},
        {"org_id": "org", "wa_id": "waba", "phone_id": "phone-2", "is_active": True},
        {"org_id": "org", "wa_id": "other-waba", "phone_id": "phone-1", "is_active": True},
        {"org_id": "org", "wa_id": "waba", "phone_id": "inactive", "is_active": False},
    ]
    namespace["db"] = SyncDB(documents)

    assert resolver("phone-2", "waba")["phone_id"] == "phone-2"
    assert resolver("phone-1", "other-waba")["wa_id"] == "other-waba"
    assert resolver("phone-1", "wrong-waba") is None
    assert resolver("inactive", "waba") is None
    assert resolver(None, "waba") is None

    namespace["db"] = SyncDB([documents[0], dict(documents[0])])
    assert resolver("phone-1", "waba") is None

    namespace["db"] = SyncDB([documents[0]])
    assert resolver(None, "waba")["phone_id"] == "phone-1"
    assert resolver(None, "waba", allow_single_fallback=False) is None

    namespace["db"] = SyncDB([{"org_id": "org", "wa_id": 123, "phone_id": 456, "is_active": True}])
    assert resolver("456", "123")["phone_id"] == 456


def _load_disconnect_service():
    tree = ast.parse((ROOT / "services/platforms/whatsapp_service.py").read_text(encoding="utf-8"))
    service = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WhatsAppService"
    )
    disconnect = next(
        node for node in service.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "disconnect"
    )
    service.body = [disconnect]
    namespace = {"BasePlatformService": object}
    exec(compile(ast.Module(body=[service], type_ignores=[]), "disconnect", "exec"), namespace)
    return namespace["WhatsAppService"], namespace


def test_disconnect_uses_canonical_phone_id_and_keeps_sibling():
    service_class, namespace = _load_disconnect_service()
    calls = []

    async def deactivate(org_id, phone_id):
        calls.append(("deactivate", org_id, phone_id))
        return {"remaining_connections": 1, "provider_cleanup": "not_needed"}

    namespace["deactivate_whatsapp_connection"] = deactivate
    result = asyncio.run(service_class().disconnect("org", "phone-2"))

    assert result["remaining_connections"] == 1
    assert calls == [("deactivate", "org", "phone-2")]


def _load_deactivate_connection():
    tree = ast.parse((ROOT / "services/wa_service.py").read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_deactivate_whatsapp_connection_sync", "deactivate_whatsapp_connection",
        }
    ]
    namespace = {
        "asyncio": asyncio, "datetime": datetime, "timezone": timezone,
        "HTTPException": RuntimeError,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "deactivate", "exec"), namespace)
    return namespace["deactivate_whatsapp_connection"], namespace


class MutableCollection(SyncConnections):
    def find_one(self, query):
        document = next((document for document in self.documents if _matches(document, query)), None)
        return deepcopy(document) if document else None

    def find(self, query, *_args, **_kwargs):
        return SyncCursor([document for document in self.documents if _matches(document, query)])

    def update_one(self, query, update, **_kwargs):
        document = next((document for document in self.documents if _matches(document, query)), None)
        if document is None:
            return SimpleNamespace(modified_count=0)
        document.update(deepcopy(update.get("$set", {})))
        for field, amount in update.get("$inc", {}).items():
            document[field] = document.get(field, 0) + amount
        for field in update.get("$unset", {}):
            document.pop(field, None)
        return SimpleNamespace(modified_count=1)

    def update_many(self, query, update, **_kwargs):
        modified = 0
        for document in self.documents:
            if _matches(document, query):
                document.update(deepcopy(update.get("$set", {})))
                modified += 1
        return SimpleNamespace(modified_count=modified)

    def delete_one(self, query, **_kwargs):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class MutableDB(SimpleNamespace):
    def __init__(self, collections, **kwargs):
        super().__init__(**kwargs)
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.setdefault(name, MutableCollection([]))


class Session:
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def start_transaction(self): return self


def test_disconnect_is_fast_soft_delete_and_keeps_history_owner():
    deactivate, namespace = _load_deactivate_connection()
    connections = MutableCollection([
        {"_id": "one", "org_id": "org", "wa_id": "waba", "phone_id": "phone-1", "slot": 1, "is_active": True, "access_token": "secret"},
        {"_id": "two", "org_id": "org", "wa_id": "other-waba", "phone_id": "phone-2", "slot": 2, "is_active": True, "access_token": "other"},
    ])
    organizations = MutableCollection([{"_id": "org", "org_id": "org", "wa_id": "waba"}])
    conversations = MutableCollection([{
        "conversation_id": "whatsapp_customer", "platform": "whatsapp", "customer_id": "customer",
    }])
    namespace["db"] = MutableDB(
        {"conversations_org": conversations},
        whatsapp_connections=connections,
        organizations=organizations,
    )
    namespace["mongo_client"] = SimpleNamespace(start_session=lambda: Session())

    result = asyncio.run(deactivate("org", "phone-1"))
    assert result == {"already_disconnected": False, "remaining_connections": 1, "provider_cleanup": "pending"}
    assert connections.documents[0]["is_active"] is False and "slot" not in connections.documents[0]
    assert connections.documents[1]["slot"] == 1
    assert organizations.documents[0]["wa_id"] == "other-waba"
    assert conversations.documents[0]["business_phone_id"] == "phone-1"
    assert asyncio.run(deactivate("org", "phone-1"))["already_disconnected"] is True


def test_provider_cleanup_is_claimed_and_idempotent():
    tree = ast.parse((ROOT / "services/wa_service.py").read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "finalize_whatsapp_revocation"
    )
    namespace = {
        "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
        "asyncio": asyncio,
        "WHATSAPP_PROVIDER_CLEANUP_MAX_ATTEMPTS": 5,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "finalize", "exec"), namespace)
    connections = MutableCollection([{
        "_id": "one", "org_id": "org", "wa_id": "waba", "phone_id": "phone-1",
        "is_active": False, "access_token": "secret", "provider_revoke_status": "pending",
    }])
    owners = MutableCollection([{"_id": "waba", "org_id": "org"}])
    namespace["db"] = SimpleNamespace(
        whatsapp_connections=connections,
        whatsapp_waba_owners=owners,
    )

    async def revoke(*_args):
        return True, ""

    namespace["_unsubscribe_whatsapp_waba"] = revoke
    finalize = namespace["finalize_whatsapp_revocation"]
    assert asyncio.run(finalize("org", "phone-1")) == {"provider_cleanup": "complete"}
    assert connections.documents[0]["provider_revoke_status"] == "complete"
    assert connections.documents[0]["provider_revoke_attempts"] == 1
    assert "access_token" not in connections.documents[0]
    assert owners.documents == []
    assert asyncio.run(finalize("org", "phone-1")) == {"provider_cleanup": "not_pending"}

    connections.documents.extend([
        {
            "_id": "fresh", "org_id": "org", "wa_id": "waba", "phone_id": "phone-2",
            "is_active": False, "access_token": "fresh", "provider_revoke_status": "processing",
            "provider_revoke_updated_at": datetime.now(timezone.utc),
        },
        {
            "_id": "stale", "org_id": "org", "wa_id": "waba", "phone_id": "phone-3",
            "is_active": False, "access_token": "stale", "provider_revoke_status": "processing",
            "provider_revoke_updated_at": datetime.now(timezone.utc) - timedelta(minutes=10),
        },
    ])
    assert asyncio.run(finalize("org", "phone-2")) == {"provider_cleanup": "not_pending"}
    assert asyncio.run(finalize("org", "phone-3")) == {"provider_cleanup": "complete"}


def test_provider_cleanup_never_unsubscribes_a_waba_with_any_active_line():
    tree = ast.parse((ROOT / "services/wa_service.py").read_text(encoding="utf-8"))
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "finalize_whatsapp_revocation"
    )
    namespace = {
        "datetime": datetime, "timezone": timezone, "timedelta": timedelta,
        "asyncio": asyncio,
        "WHATSAPP_PROVIDER_CLEANUP_MAX_ATTEMPTS": 5,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "finalize", "exec"), namespace)
    connections = MutableCollection([
        {
            "_id": "removed", "org_id": "org", "wa_id": "shared", "phone_id": "phone-1",
            "is_active": False, "access_token": "secret", "provider_revoke_status": "pending",
        },
        {
            "_id": "active", "org_id": "other-org", "wa_id": "shared", "phone_id": "phone-2",
            "is_active": True, "access_token": "other",
        },
    ])
    owners = MutableCollection([{"_id": "shared", "org_id": "org"}])
    namespace["db"] = SimpleNamespace(
        whatsapp_connections=connections,
        whatsapp_waba_owners=owners,
    )
    calls = []

    async def unsubscribe(*args):
        calls.append(args)
        return True, ""

    namespace["_unsubscribe_whatsapp_waba"] = unsubscribe
    assert asyncio.run(namespace["finalize_whatsapp_revocation"]("org", "phone-1")) == {
        "provider_cleanup": "not_needed"
    }
    assert calls == []
    assert owners.documents == [{"_id": "shared", "org_id": "org"}]


class AsyncCursor:
    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return deepcopy(self.documents if length is None else self.documents[:length])


class AsyncCollection:
    def __init__(self, documents=None):
        self.documents = [deepcopy(document) for document in (documents or [])]

    def find(self, query, _projection=None, **_kwargs):
        return AsyncCursor([document for document in self.documents if _matches(document, query)])

    async def find_one(self, query, **_kwargs):
        return next((deepcopy(document) for document in self.documents if _matches(document, query)), None)

    async def find_one_and_update(self, query, update, return_document=None, **_kwargs):
        for document in self.documents:
            if _matches(document, query):
                document.update(deepcopy(update.get("$set", {})))
                for field in update.get("$unset", {}):
                    document.pop(field, None)
                return deepcopy(document)
        return None

    async def update_many(self, query, update, **_kwargs):
        matched = 0
        for document in self.documents:
            if not _matches(document, query):
                continue
            matched += 1
            document.update(deepcopy(update.get("$set", {})))
            for field in update.get("$unset", {}):
                document.pop(field, None)
        return SimpleNamespace(matched_count=matched, modified_count=matched)

    async def delete_one(self, query):
        for index, document in enumerate(self.documents):
            if _matches(document, query):
                self.documents.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def insert_one(self, document):
        self.documents.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("flow_id"))


class AsyncTransaction:
    def __init__(self, database):
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def with_transaction(self, callback):
        collections = [self.database.whatsapp_connections, *self.database.collections.values()]
        before = [deepcopy(collection.documents) for collection in collections]
        try:
            return await callback(self)
        except Exception:
            for collection, documents in zip(collections, before):
                collection.documents = documents
            raise


class AsyncDB:
    def __init__(self):
        self.whatsapp_connections = AsyncCollection([
            {"org_id": "org", "wa_id": "waba", "phone_id": "phone-1", "is_active": True},
            {"org_id": "org", "wa_id": "waba", "phone_id": "phone-2", "is_active": True},
            {"org_id": "other", "wa_id": "waba", "phone_id": "phone-3", "is_active": True},
        ])
        self.collections = {
            "wa_bot_flows_org": AsyncCollection([
                {
                    "org_id": "org",
                    "flow_id": "flow-1",
                    "name": "Bound flow",
                    "status": "draft",
                    "trigger_messages": "hello",
                    "start_node_id": "start",
                    "nodes": {"start": {"type": "text", "content": "Hello"}},
                },
            ])
        }

    def __getitem__(self, name):
        return self.collections[name]

    async def start_session(self):
        return AsyncTransaction(self)


def test_publish_binds_connections_without_adding_phone_ids_to_flow(monkeypatch):
    database = AsyncDB()
    monkeypatch.setattr(wa_bot_service, "db", database)
    monkeypatch.setattr(wa_bot_service, "get_async_mongo_client", lambda: database)

    published = asyncio.run(wa_bot_service.publish_wa_bot_flow(
        "org", "flow-1", ["phone-2", "phone-2", "phone-1"], updated_by="user",
    ))
    assert published["status"] == "active"
    assert "phone_ids" not in published and "wa_bot_flow_id" not in published
    assert {
        connection["phone_id"] for connection in database.whatsapp_connections.documents
        if connection.get("wa_bot_flow_id") == "flow-1"
    } == {"phone-1", "phone-2"}

    asyncio.run(wa_bot_service.unpublish_wa_bot_flow("org", "flow-1", updated_by="user"))
    assert not any(
        connection.get("wa_bot_flow_id") == "flow-1"
        for connection in database.whatsapp_connections.documents
    )


def test_publish_rejects_unknown_phone_and_delete_or_duplicate_stays_unbound(monkeypatch):
    database = AsyncDB()
    monkeypatch.setattr(wa_bot_service, "db", database)
    monkeypatch.setattr(wa_bot_service, "get_async_mongo_client", lambda: database)

    try:
        asyncio.run(wa_bot_service.publish_wa_bot_flow("org", "flow-1", ["missing"]))
    except ValueError as exc:
        assert "Unknown or inactive" in str(exc)
    else:
        raise AssertionError("Unknown phone ID must not be published")
    assert database.collections["wa_bot_flows_org"].documents[0]["status"] == "draft"

    source = database.collections["wa_bot_flows_org"].documents[0]
    source["phone_ids"] = ["legacy-leak"]
    source["wa_bot_flow_id"] = "legacy-leak"
    duplicate = asyncio.run(wa_bot_service.duplicate_wa_bot_flow("org", "flow-1"))
    assert duplicate["status"] == "draft"
    assert "phone_ids" not in duplicate and "wa_bot_flow_id" not in duplicate

    database.whatsapp_connections.documents[0]["wa_bot_flow_id"] = "flow-1"
    assert asyncio.run(wa_bot_service.delete_wa_bot_flow("org", "flow-1")) is True
    assert "wa_bot_flow_id" not in database.whatsapp_connections.documents[0]


def test_webhook_uses_bound_flow_and_number_aware_conversations():
    source = (ROOT / "api/webhook_routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_or_update_whatsapp_conversation"
    ]
    assert len(calls) >= 3
    assert all(
        {keyword.arg for keyword in call.keywords} >= {"business_phone_id", "business_phone"}
        for call in calls
    )
    assert '(value.get("metadata") or {}).get("phone_number_id")' in source
    assert 'connection.get("wa_bot_flow_id")' in source
    assert '"flow_id": bound_flow_id' in source


def test_all_outbound_paths_use_bound_sender_and_customer_fields():
    scheduler = (ROOT / "services/automation_scheduler.py").read_text(encoding="utf-8")
    google_sheet = (ROOT / "services/google_sheet_automation.py").read_text(encoding="utf-8")
    oos = (ROOT / "services/oos_service.py").read_text(encoding="utf-8")
    wa_service = (ROOT / "services/wa_service.py").read_text(encoding="utf-8")
    wa_routes = (ROOT / "api/wa_routes.py").read_text(encoding="utf-8")

    assert 'recipient_id=conversation_id.split("_")[-1]' not in scheduler
    assert 'recipient_id=recipient_id' in scheduler
    assert 'conversation_id=conversation_id' in scheduler

    assert "conversation_id = await create_or_update_whatsapp_conversation(" in google_sheet
    assert "business_phone_id=phone_id" in google_sheet
    assert "resolve_outbound_whatsapp_connection(" in google_sheet

    assert 'db.whatsapp_connections.find_one, {"org_id": org_id}' not in oos
    assert "dealer_conversation_id = await create_or_update_whatsapp_conversation(" in oos
    assert '"conversation_id": dealer_conversation_id' in oos

    reaction_start = wa_service.index("async def send_whatsapp_reaction")
    reaction_end = wa_service.index("async def send_message_reaction", reaction_start)
    reaction_source = wa_service[reaction_start:reaction_end]
    assert "resolve_outbound_whatsapp_connection(" in reaction_source
    assert 'conversation.get("customer_id")' in reaction_source
    assert "whatsapp_connections.find_one" not in reaction_source

    bulk_start = wa_routes.index("async def bulk_send_whatsapp_message")
    bulk_source = wa_routes[bulk_start:]
    assert "await asyncio.to_thread(resolve_outbound_whatsapp_connection," in bulk_source
    assert "request.recipients" not in bulk_source
    assert "whatsapp_conversation_id(phone, phone_id)" in wa_service
    webhook_tree = ast.parse((ROOT / "api/webhook_routes.py").read_text(encoding="utf-8"))
    typing_calls = [
        node for node in ast.walk(webhook_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "read_message_and_typing_indicator"
    ]
    assert typing_calls
    assert all("conversation_id" in {keyword.arg for keyword in call.keywords} for call in typing_calls)


if __name__ == "__main__":
    monkeypatch = SimpleNamespace(setattr=setattr)
    test_details_helpers_are_tenant_scoped_and_map_meta_failures()
    test_inbound_connection_resolution_is_exact_and_ambiguity_safe()
    test_disconnect_uses_canonical_phone_id_and_keeps_sibling()
    test_disconnect_is_fast_soft_delete_and_keeps_history_owner()
    test_provider_cleanup_is_claimed_and_idempotent()
    test_provider_cleanup_never_unsubscribes_a_waba_with_any_active_line()
    test_publish_binds_connections_without_adding_phone_ids_to_flow(monkeypatch)
    test_publish_rejects_unknown_phone_and_delete_or_duplicate_stays_unbound(monkeypatch)
    test_webhook_uses_bound_flow_and_number_aware_conversations()
    test_all_outbound_paths_use_bound_sender_and_customer_fields()
    print("WhatsApp number binding contracts passed")
