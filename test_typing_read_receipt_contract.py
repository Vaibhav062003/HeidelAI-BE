import ast
import asyncio
import re
import sys
import time
import types
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


source_path = Path(__file__).parent / "services/websocket_service.py"
tree = ast.parse(source_path.read_text())
tested_names = {
    "latest_unread_whatsapp_message_id",
    "handle_typing_event",
    "join_conversation_presence",
    "leave_conversation_presence",
}
tested_nodes = [
    node for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in tested_names
]


class FakeMessages:
    latest_message_id = "visible-before-leave"

    async def find_one(self, *_args, **_kwargs):
        return {"message_id": self.latest_message_id}


class FakeDb:
    messages = FakeMessages()

    def __getitem__(self, _name):
        return self.messages


class FakeLogger:
    def info(self, *_args, **_kwargs):
        pass


async def check_receipt_is_bound_to_visible_message():
    client_id = "test-client"
    conversation_id = "whatsapp_15551234567"
    fake_db = FakeDb()
    sent = []
    tasks = []
    controls = {"hide_during_broadcast": False}
    state = types.SimpleNamespace(
        client_metadata={client_id: {"org_id": "org-1", "wa_id": "wa-1"}},
        client_convo_map={client_id: ("org-1", conversation_id)},
        org_convo_viewers=defaultdict(lambda: defaultdict(set)),
    )

    async def hide_chat_and_receive_new_message(**_kwargs):
        if controls["hide_during_broadcast"]:
            state.client_convo_map.pop(client_id, None)
            fake_db.messages.latest_message_id = "arrived-during-broadcast"
        return None

    async def record_receipt(**kwargs):
        sent.append(kwargs)

    fake_asyncio = types.SimpleNamespace(create_task=lambda coro: tasks.append(coro))
    fake_wa_service = types.ModuleType("services.wa_service")
    fake_wa_service.read_message_and_typing_indicator = record_receipt
    fake_services_package = types.ModuleType("services")
    fake_services_package.__path__ = []
    namespace = {
        "asyncio": fake_asyncio,
        "broadcast_main_ws": hide_chat_and_receive_new_message,
        "get_async_mongo_db": lambda: fake_db,
        "logger": FakeLogger(),
        "services": state,
    }

    previous_services = sys.modules.get("services")
    previous_wa_service = sys.modules.get("services.wa_service")
    try:
        sys.modules["services"] = fake_services_package
        sys.modules["services.wa_service"] = fake_wa_service
        exec(compile(ast.Module(tested_nodes, type_ignores=[]), source_path, "exec"), namespace)

        await namespace["handle_typing_event"](client_id, {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "is_typing": True,
        })

        await asyncio.gather(*tasks)

        assert sent == [{
            "whatsapp_id": "wa-1",
            "message_id": "visible-before-leave",
        }]

        sent.clear()
        tasks.clear()
        controls["hide_during_broadcast"] = True
        fake_db.messages.latest_message_id = "visible-before-leave"
        state.client_convo_map[client_id] = ("org-1", conversation_id)
        await namespace["handle_typing_event"](client_id, {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "is_typing": True,
        })
        await asyncio.gather(*tasks)
        assert sent == []

        controls["hide_during_broadcast"] = False
        tasks.clear()
        state.client_convo_map[client_id] = ("org-1", conversation_id)
        await namespace["handle_typing_event"](client_id, {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "is_typing": False,
        })
        assert tasks == []

        state.client_convo_map.clear()
        await namespace["handle_typing_event"](client_id, {
            "platform": "whatsapp",
            "conversation_id": conversation_id,
            "is_typing": True,
        })
        assert tasks == []

        namespace["join_conversation_presence"]("org-1", "chat-a", client_id)
        namespace["join_conversation_presence"]("org-1", "chat-b", client_id)
        assert client_id not in state.org_convo_viewers.get("org-1", {}).get("chat-a", set())
        assert state.client_convo_map[client_id] == ("org-1", "chat-b")
        namespace["leave_conversation_presence"]("org-1", "chat-a", client_id)
        assert state.client_convo_map[client_id] == ("org-1", "chat-b")
    finally:
        if previous_services is None:
            sys.modules.pop("services", None)
        else:
            sys.modules["services"] = previous_services
        if previous_wa_service is None:
            sys.modules.pop("services.wa_service", None)
        else:
            sys.modules["services.wa_service"] = previous_wa_service


asyncio.run(check_receipt_is_bound_to_visible_message())

common_source_path = Path(__file__).parent / "services/common_service.py"
common_tree = ast.parse(common_source_path.read_text())
fetch_conversations_new = next(
    node for node in common_tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch_conversations_new"
)
fetch_messages = next(
    node for node in common_tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch_messages"
)
mutating_calls = {
    node.func.attr for node in ast.walk(fetch_messages)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    and node.func.attr == "update_one"
}
assert not mutating_calls, "fetch_messages must not clear unread state"

acknowledge_opened = next(
    node for node in common_tree.body
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "acknowledge_conversation_opened"
)
assert any(
    isinstance(node, ast.Call)
    and isinstance(node.func, ast.Attribute)
    and node.func.attr == "update_one"
    for node in ast.walk(acknowledge_opened)
), "explicit open acknowledgment must persist the internal unread state"


async def check_stale_acknowledgement_is_ignored():
    class FakeConversations:
        counter = 2
        unread = 1

        async def update_one(self, query, _update):
            matched = (
                query.get("conversation_id") == "chat-a"
                and query.get("$expr", {}).get("$eq", [None, None])[1] == self.counter
            )
            if matched:
                self.unread = 0
            return types.SimpleNamespace(matched_count=int(matched))

    collection = FakeConversations()
    fake_db = FakeDb()
    fake_db.messages = collection
    namespace = {"get_async_mongo_db": lambda: fake_db}
    exec(compile(ast.Module([acknowledge_opened], type_ignores=[]), source_path, "exec"), namespace)

    stale = await namespace["acknowledge_conversation_opened"]("org-1", "chat-a", 1)
    assert stale["acknowledged"] is False and collection.unread == 1
    current = await namespace["acknowledge_conversation_opened"]("org-1", "chat-a", 2)
    assert current["acknowledged"] is True and collection.unread == 0


asyncio.run(check_stale_acknowledgement_is_ignored())


async def check_queue_exposes_message_counter():
    class FakeCursor:
        def sort(self, *_args): return self
        def skip(self, *_args): return self
        def limit(self, *_args): return self

        async def to_list(self, **_kwargs):
            return [{
                "conversation_id": "chat-a",
                "platform": "whatsapp",
                "message_counter": 7,
            }]

    class FakeCollection:
        def find(self, _query, projection):
            assert projection.get("message_counter") != 0
            return FakeCursor()

    fake_db = FakeDb()
    fake_db.messages = FakeCollection()
    namespace = {
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "datetime": datetime,
        "get_async_mongo_db": lambda: fake_db,
        "logger": FakeLogger(),
        "re": re,
        "time": time,
    }
    exec(compile(ast.Module([fetch_conversations_new], type_ignores=[]), common_source_path, "exec"), namespace)
    result = await namespace["fetch_conversations_new"]("org-1")
    assert result[0]["message_counter"] == 7


asyncio.run(check_queue_exposes_message_counter())

webhook_tree = ast.parse((Path(__file__).parent / "api/webhook_routes.py").read_text())
non_heidelai_branch = next(
    node for node in ast.walk(webhook_tree)
    if isinstance(node, ast.If)
    and isinstance(node.test, ast.UnaryOp)
    and isinstance(node.test.op, ast.Not)
    and isinstance(node.test.operand, ast.Name)
    and node.test.operand.id == "is_heidelai"
)
unsafe_reads = [
    statement for statement in non_heidelai_branch.body
    if isinstance(statement, ast.Expr)
    and isinstance(statement.value, ast.Await)
    and isinstance(statement.value.value, ast.Call)
    and isinstance(statement.value.value.func, ast.Name)
    and statement.value.value.func.id == "read_message_and_typing_indicator"
]
assert not unsafe_reads, "Receiving a message must not immediately mark it read"
