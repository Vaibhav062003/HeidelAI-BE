"""Run with python3 tests/contracts/test_active_flow_completion.py; no services or network."""
import ast
import asyncio
import copy
import re
import sys
import types
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from test_uploaded_contacts_delete_contract import Collection, Database, Logger, ObjectId, Result, matches


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def load(path, namespace, names=None):
    tree = ast.parse(Path(path).read_text())
    tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))
                 and (names is None or getattr(node, "name", None) in names)]
    exec(compile(ast.fix_missing_locations(tree), path, "exec"), namespace)
    return namespace


class MemoryCollection(Collection):
    def update_one(self, query, update, **kwargs):
        result = super().update_one(query, copy.deepcopy(update), **kwargs)
        if not result.matched_count and kwargs.get("upsert"):
            self.insert_one({**query, **update.get("$set", {})})
            return Result(matched_count=1, modified_count=1)
        return result

    def insert_one(self, row, **kwargs):
        row = copy.deepcopy(row)
        row.setdefault("_id", ObjectId(uuid.uuid4().hex[:24]))
        self.rows.append(row)
        return types.SimpleNamespace(inserted_id=row["_id"])

    def find_one(self, query, *args, **kwargs):
        row = super().find_one(query)
        return copy.deepcopy(row)

    def find_one_and_update(self, query, update, **kwargs):
        if not self.update_one(query, update, **kwargs).matched_count:
            return None
        return self.find_one(query if "version" not in query else {k: v for k, v in query.items() if k != "version"})

    def update_many(self, query, update, **kwargs):
        for row in self.rows:
            if matches(row, query):
                row.update(copy.deepcopy(update.get("$set", {})))
        return Result(matched_count=1)


class Session:
    def __init__(self, database):
        self.database = database

    def __enter__(self): return self
    def __exit__(self, *args): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass

    def with_transaction(self, callback):
        before = copy.deepcopy({key: col.rows for key, col in self.database.collections.items()})
        try:
            return callback(self)
        except Exception:
            for key, rows in before.items():
                self.database[key].rows = rows
            raise


class Client:
    def __init__(self, database): self.database = database
    def start_session(self): return Session(self.database)


def base():
    return dict(HTTPException=HTTPException, deepcopy=copy.deepcopy, Optional=Optional,
                Dict=Dict, List=List, Any=Any, datetime=datetime, timezone=timezone,
                asyncio=asyncio, uuid=uuid, re=re, logger=Logger(),
                serialize_mongo=lambda value: value,
                ReturnDocument=types.SimpleNamespace(AFTER=True))


def test_customers():
    database = Database({"contacts_org": MemoryCollection([]), "conversations_org": MemoryCollection([]),
                         "contacts_other": MemoryCollection([])})
    env = base()
    env.update(db=database, ObjectId=ObjectId, get_mongo_client=lambda: Client(database),
               get_org_default_country_code=lambda org: "91", _validate_email=lambda email: "@" in email,
               name_search_prefixes=lambda name: [name.lower()])
    load("services/utils.py", env, {"normalize_phone"})
    load("services/contacts_service.py", env, {"directory_contact", "save_directory_contact", "delete_directory_contact", "store_contact"})
    saved = env["save_directory_contact"]("org", {"full_name": "Ada", "phone_number": "9000000001"})
    contact_id = saved["contact_id"]
    assert saved["phone_number"] == "919000000001" and not saved.get("conversation_id")
    assert env["save_directory_contact"]("org", {"full_name": "Ada Lovelace"}, contact_id)["full_name"] == "Ada Lovelace"
    try:
        env["save_directory_contact"]("org", {"full_name": "Duplicate", "phone_number": "9000000001"})
        assert False, "duplicate phone must fail"
    except HTTPException as exc:
        assert exc.status_code == 409
    row = database["contacts_org"].rows[0]
    row.update(conversation_id="whatsapp_live", conversation_ids=["whatsapp_live", "whatsapp_other"], whatsapp_id="waba")
    database["conversations_org"].rows = [{"conversation_id": value, "customer_name": "Old"} for value in row["conversation_ids"]]
    env["save_directory_contact"]("org", {"full_name": "Saved Name"}, contact_id)
    assert all(c["customer_name"] == "Saved Name" for c in database["conversations_org"].rows)
    try:
        env["save_directory_contact"]("org", {"phone_number": "9000000002"}, contact_id)
        assert False, "linked identity must not change"
    except HTTPException as exc:
        assert exc.status_code == 409
    try:
        env["delete_directory_contact"]("other", contact_id)
        assert False, "cross-org deletion must fail"
    except HTTPException as exc:
        assert exc.status_code == 404
    env["delete_directory_contact"]("org", contact_id)
    env["logger"].success = lambda *args: None
    env["store_contact"]("org", "whatsapp", {"phone_number": saved["phone_number"], "conversation_id": "whatsapp_live", "full_name": "Provider Name"})
    assert database["contacts_org"].rows[0].get("deleted_at"), "a webhook must not restore a directory entry"
    assert len(database["conversations_org"].rows) == 2
    restored = env["save_directory_contact"]("org", {"full_name": "Restored", "phone_number": "9000000001"})
    assert restored["contact_id"] == contact_id and "deleted_at" not in restored
    assert len(database["contacts_org"].rows) == 1
    # A later Instagram webhook must preserve the saved name until deletion.
    database["contacts_org"].rows.append({"_id": ObjectId("64b000000000000000000099"),
        "instagram_id": "ig-user", "full_name": "Saved Instagram Name", "name_locked_by_user": True})
    database["conversations_org"].rows.append({"conversation_id": "instagram_ada", "next_action_at": None})
    async def noop(*args, **kwargs): pass
    env.update(services=types.SimpleNamespace(org_convo_viewers={}, instagram_connections_map={}),
               abort_scheduled_message=noop, reopen_conversation=noop, broadcast_main_ws=noop, timedelta=timedelta)
    load("services/ig_service.py", env, {"create_or_update_instagram_conversation"})
    asyncio.run(env["create_or_update_instagram_conversation"]("org", "account", "ig-user", "Provider Name", "ada", "Hi", "customer"))
    assert database["conversations_org"].rows[-1]["customer_name"] == "Saved Instagram Name"
    env["delete_directory_contact"]("org", "64b000000000000000000099")
    asyncio.run(env["create_or_update_instagram_conversation"]("org", "account", "ig-user", "Provider Name", "ada", "Hi", "customer"))
    assert database["conversations_org"].rows[-1]["customer_name"] == "Provider Name"


def graph(text="live", trigger="instagram_message"):
    return {"nodes": {"message": {"id": "message", "type": "message", "content": {"text": text}}},
            "connections": [], "triggers": [{"type": trigger, "config": {}, "start_node_id": "message"}]}


async def test_automation():
    database = Database({"automation_flows_org": MemoryCollection([{"org_id": "org", "flow_id": "one", "status": "published", "version": 1, "name": "Live", "flow_data": graph()}]),
                         "automation_triggers": MemoryCollection([])})
    env = base()
    load("services/automation_state.py", env)
    load("services/automation_capabilities.py", env)
    env.update(db=database, get_mongo_client=lambda: Client(database))
    load("services/automation_service.py", env, {"_automation_flows_collection", "_automation_triggers_collection", "update_automation_flow", "publish_automation_flow", "unpublish_automation_flow", "_deactivate_flow_triggers", "_register_single_trigger", "validate_flow_structure"})
    scheduler = types.ModuleType("services.automation_scheduler")
    scheduler.cancel_flow_followups = lambda *args, **kwargs: None
    scheduler.schedule_followup = lambda *args, **kwargs: None
    sys.modules["services.automation_scheduler"] = scheduler
    saved = await env["update_automation_flow"]("org", "one", {"flow_data": graph("draft"), "expected_draft_version": 1, "status": "draft"})
    stored = database["automation_flows_org"].rows[0]
    assert saved["draft_version"] == 2 and saved["published_version"] == 1
    assert env["published_snapshot"](stored)["flow_data"] == graph("live")
    assert stored["status"] == "published"
    try:
        await env["update_automation_flow"]("org", "one", {"flow_data": graph("stale"), "expected_draft_version": 1})
        assert False, "stale editor must fail"
    except HTTPException as exc:
        assert exc.status_code == 409
    register = env["_register_single_trigger"]
    def fail(*args, **kwargs): raise RuntimeError("registration failed")
    env["_register_single_trigger"] = fail
    try:
        await env["publish_automation_flow"]("org", "one", 2)
        assert False, "publication must fail if registration fails"
    except RuntimeError: pass
    assert database["automation_flows_org"].rows[0]["published_version"] == 1
    assert env["published_snapshot"](database["automation_flows_org"].rows[0])["flow_data"] == graph("live")
    env["_register_single_trigger"] = register
    published = await env["publish_automation_flow"]("org", "one", 2)
    assert published["published_version"] == 2
    assert database["automation_triggers"].rows[0]["trigger_type"] == "instagram_dm_received"
    assert database["automation_triggers"].rows[0]["published_version"] == 2
    await env["unpublish_automation_flow"]("org", "one")
    assert database["automation_triggers"].rows[0]["status"] == "inactive"

    caps = env["automation_capabilities"]()
    assert "trigger_email_received" not in caps["node_types"] and not caps["features"]["live_test"]
    assert "action_send_dm" not in caps["nodes_by_trigger"]["trigger_instagram_message"]
    for bad in [graph(trigger="email_received"), {"nodes": {"x": {"type": "email"}}, "triggers": [{"type": "instagram_message", "start_node_id": "x"}]}]:
        try:
            env["validate_executable_flow"](bad)
            assert False, "unsupported automation must fail"
        except ValueError: pass

    # The real traversal must stop at failures and Stop nodes; text export is read.
    env["logger"].info = lambda *args: None
    env["logger"].error = lambda *args: None
    load("services/automation_execution.py", env, {"FlowExecutionContext", "_execute_node", "_get_next_nodes", "_get_conditional_next_nodes", "_execute_condition_node"})
    env["TRIGGER_NODE_TYPES"] = {"trigger"}
    calls = []
    async def action(context, node_id, kind, config):
        calls.append(config)
        if node_id == "bad": context.log(node_id, kind, "error", "Provider failed", success=False)
    env["execute_action_node"] = action
    context = env["FlowExecutionContext"]("org", "one", "message_received", {"message_text": "Hello"})
    assert await env["_execute_node"](context, "message", graph()["nodes"], [])
    assert calls[-1]["content"]["text"] == "live"
    nodes = {"bad": {"type": "action"}, "later": {"type": "message"}}
    assert not await env["_execute_node"](context, "bad", nodes, [{"source": "bad", "target": "later"}])
    assert context.status == "failed" and len(calls) == 2
    context = env["FlowExecutionContext"]("org", "one", "message_received", {})
    assert await env["_execute_node"](context, "stop", {"stop": {"type": "stop"}}, [])
    assert len(calls) == 2
    branches = [{"source": "condition", "target": "yes", "source_handle": "true"},
                {"source": "condition", "target": "no", "sourceHandle": "false"}]
    assert env["_get_conditional_next_nodes"]("condition", branches, True) == ["yes"]
    assert env["_get_conditional_next_nodes"]("condition", branches, False) == ["no"]
    context.set_variable("message", "hello")
    assert await env["_execute_condition_node"](context, "condition", {"variable": "message", "operator": "includes", "value": "ell"})


    # Top-level execution must preserve a failed run in its stored record.
    load("services/automation_execution.py", env, {"execute_automation_flow"})
    entitlement = types.ModuleType("services.entitlement_service")
    async def quota(*args): pass
    entitlement.consume_quota_async = quota
    sys.modules["services.entitlement_service"] = entitlement
    records = []
    async def save_record(context): records.append(context.to_dict())
    env["_save_execution_record"] = save_record
    import hashlib
    import json
    env.update(hashlib=hashlib, json=json)
    stored = database["automation_flows_org"].rows[0]
    failure_graph = {"nodes": {"bad": {"type": "action", "config": {"action_type": "api"}}},
                     "connections": [], "triggers": [{"type": "instagram_message", "start_node_id": "bad"}]}
    stored.update(status="published", published_snapshot={"flow_data": failure_graph})
    result = await env["execute_automation_flow"]("org", "one", "message_received", {"message_id": "source"})
    assert result["status"] == records[-1]["status"] == "failed"

    # Exercise the actual HTTP action without an external request.
    class HttpClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            return types.SimpleNamespace(status_code=500, json=lambda: {"error": "upstream"})
    sys.modules["httpx"] = types.SimpleNamespace(AsyncClient=HttpClient)
    env.update(json_lib=json, replace_variables=lambda text, variables: text)
    load("services/automation_actions.py", env, {"execute_http_request"})
    context = env["FlowExecutionContext"]("org", "one", "message_received", {})
    await env["execute_http_request"](context, "http", {"api_url": "https://example.invalid", "api_method": "GET"}, {})
    assert context.status == "failed" and context.variables["http_status_code"] == 500
    context = env["FlowExecutionContext"]("org", "one", "message_received", {})
    await env["execute_http_request"](context, "http", {"api_url": "https://example.invalid", "api_body": "{"}, {})
    assert context.status == "failed" and "http_status_code" not in context.variables


class AsyncCollection:
    def __init__(self, collection): self.collection = collection
    @property
    def rows(self): return self.collection.rows
    @rows.setter
    def rows(self, value): self.collection.rows = value
    async def find_one(self, *args, **kwargs): return self.collection.find_one(*args, **kwargs)
    async def find_one_and_update(self, *args, **kwargs): return self.collection.find_one_and_update(*args, **kwargs)


class AsyncSession(Session):
    async def with_transaction(self, callback):
        before = copy.deepcopy({key: col.rows for key, col in self.database.collections.items()})
        try:
            return await callback(self)
        except Exception:
            for key, rows in before.items(): self.database[key].rows = rows
            raise


class AsyncClient(Client):
    async def start_session(self): return AsyncSession(self.database)


async def test_wabot():
    database = Database({
        "wa_bot_flows_org": AsyncCollection(MemoryCollection([{
            "org_id": "org", "flow_id": "bot", "flow_type": "wabot", "status": "active",
            "name": "Live bot", "version": 1, "trigger_messages": "hello", "start_node_id": "start",
            "nodes": {"start": {"type": "text", "content": "live"}}
        }])),
        "whatsapp_connections": MemoryCollection([{"phone_id": "phone", "wa_bot_flow_id": "old"}]),
    })
    env = base()
    load("services/automation_state.py", env)
    env.update(db=database, get_async_mongo_client=lambda: AsyncClient(database))
    load("services/wa_bot_service.py", env, {"_col", "update_wa_bot_flow", "publish_wa_bot_flow", "_validate_wa_bot_draft"})
    draft = await env["update_wa_bot_flow"]("org", "bot", {
        "nodes": {"start": {"type": "text", "content": "draft"}},
        "expected_draft_version": 1, "status": "draft", "org_id": "other",
    })
    stored = database["wa_bot_flows_org"].rows[0]
    assert draft["status"] == "active" and draft["draft_version"] == 2
    assert stored["org_id"] == "org" and env["published_snapshot"](stored)["nodes"]["start"]["content"] == "live"
    try:
        await env["update_wa_bot_flow"]("org", "bot", {"name": "Stale", "expected_draft_version": 1})
        assert False, "stale WhatsApp edits must fail"
    except HTTPException as exc:
        assert exc.status_code == 409
    async def phones(*args, **kwargs): return ["phone"]
    async def fail_bind(*args, **kwargs):
        database["whatsapp_connections"].rows[0]["wa_bot_flow_id"] = "bot"
        raise RuntimeError("binding failed")
    env["_validate_publish_phone_ids"] = phones
    env["_bind_flow_to_phone_ids"] = fail_bind
    try:
        await env["publish_wa_bot_flow"]("org", "bot", ["phone"], expected_draft_version=2)
        assert False, "failed binding must roll back publication"
    except RuntimeError: pass
    assert database["wa_bot_flows_org"].rows[0]["published_version"] == 1
    assert database["whatsapp_connections"].rows[0]["wa_bot_flow_id"] == "old"
    async def bind(*args, **kwargs):
        database["whatsapp_connections"].rows[0]["wa_bot_flow_id"] = "bot"
    env["_bind_flow_to_phone_ids"] = bind
    published = await env["publish_wa_bot_flow"]("org", "bot", ["phone"], expected_draft_version=2)
    assert published["published_version"] == 2
    assert env["published_snapshot"](database["wa_bot_flows_org"].rows[0])["nodes"]["start"]["content"] == "draft"
    try:
        env["_validate_wa_bot_draft"]({"trigger_messages": "hello", "nodes": {}})
        assert False, "empty bot cannot publish"
    except ValueError: pass


if __name__ == "__main__":
    test_customers()
    asyncio.run(test_automation())
    asyncio.run(test_wabot())
    print("Active flow completion checks passed.")
