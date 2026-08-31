import ast
import asyncio
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
        actual = document.get(field)
        if isinstance(expected, dict):
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


def _load_connection_resolver():
    tree = ast.parse((ROOT / "services/wa_service.py").read_text(encoding="utf-8"))
    node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_active_whatsapp_connection"
    )
    namespace = {"Optional": Optional}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "resolver", "exec"), namespace)
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


class AsyncCursor:
    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return deepcopy(self.documents if length is None else self.documents[:length])


class AsyncCollection:
    def __init__(self, documents=None):
        self.documents = [deepcopy(document) for document in (documents or [])]

    def find(self, query, _projection=None):
        return AsyncCursor([document for document in self.documents if _matches(document, query)])

    async def find_one(self, query):
        return next((deepcopy(document) for document in self.documents if _matches(document, query)), None)

    async def find_one_and_update(self, query, update, return_document=None):
        for document in self.documents:
            if _matches(document, query):
                document.update(deepcopy(update.get("$set", {})))
                for field in update.get("$unset", {}):
                    document.pop(field, None)
                return deepcopy(document)
        return None

    async def update_many(self, query, update):
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
                    "nodes": {"start": {"type": "text"}},
                },
            ])
        }

    def __getitem__(self, name):
        return self.collections[name]


def test_publish_binds_connections_without_adding_phone_ids_to_flow(monkeypatch):
    database = AsyncDB()
    monkeypatch.setattr(wa_bot_service, "db", database)

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
    assert "resolve_outbound_whatsapp_connection(" in bulk_source
    assert "request.recipients" not in bulk_source
    assert "whatsapp_conversation_id(phone_number, phone_id)" in wa_service
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
    test_inbound_connection_resolution_is_exact_and_ambiguity_safe()
    test_publish_binds_connections_without_adding_phone_ids_to_flow(monkeypatch)
    test_publish_rejects_unknown_phone_and_delete_or_duplicate_stays_unbound(monkeypatch)
    test_webhook_uses_bound_flow_and_number_aware_conversations()
    test_all_outbound_paths_use_bound_sender_and_customer_fields()
    print("WhatsApp number binding contracts passed")
