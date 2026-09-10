"""Run with python3 test_google_sheet_inbox.py; no provider or database access."""
import ast
import asyncio
import copy
import json
import sys
import types
import uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class Collection:
    def __init__(self):
        self.rows = []

    def matches(self, row, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self.matches(row, branch) for branch in value):
                    return False
            elif isinstance(value, dict):
                current = row.get(key)
                for op, expected in value.items():
                    if op == "$exists" and (key in row) != expected:
                        return False
                    if op in {"$lt", "$lte"} and (current is None or (current >= expected if op == "$lt" else current > expected)):
                        return False
            elif row.get(key) != value:
                return False
        return True

    def find_one(self, query, projection=None):
        return next((copy.deepcopy(row) for row in self.rows if self.matches(row, query)), None)

    def update_one(self, query, update, upsert=False):
        row = next((row for row in self.rows if self.matches(row, query)), None)
        if row is None and upsert:
            row = {**query, **copy.deepcopy(update.get("$setOnInsert", {}))}
            self.rows.append(row)
        if row is not None:
            row.update(copy.deepcopy(update.get("$set", {})))
        return types.SimpleNamespace(modified_count=int(row is not None))

    def find_one_and_update(self, query, update, **kwargs):
        self.update_one(query, update)
        return self.find_one({"conversation_id": query["conversation_id"]}) if any(self.matches(row, query) for row in self.rows) else None

    def replace_one(self, query, row, upsert=False):
        self.rows = [item for item in self.rows if not self.matches(item, query)]
        self.rows.append(copy.deepcopy(row))

    def insert_one(self, row):
        if self.find_one({key: row[key] for key in ("org_id", "flow_id", "phone")}):
            raise ValueError("duplicate")
        self.rows.append(copy.deepcopy(row))


class Db(dict):
    def __missing__(self, key):
        self[key] = Collection()
        return self[key]


def load(path, names, context):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), context)


async def check():
    db, events = Db(), []
    old_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    conversation = {
        "conversation_id": "whatsapp_customer_sender", "platform": "whatsapp",
        "customer_id": "customer", "business_phone_id": "sender", "whatsapp_id": "waba",
        "customer_name": "Original name", "last_message": "Customer question",
        "last_message_timestamp": old_at, "last_sender": "customer", "needs_reply": True,
        "status": "follow-up", "unread_count": 3, "message_counter": 8,
    }
    db["conversations_org"].rows = [copy.deepcopy(conversation)]
    logger = types.SimpleNamespace(**{name: lambda *a, **kw: None for name in ("error", "warning", "info", "success")})

    async def broadcast(**kwargs):
        events.append(kwargs["event_type"])

    async def store(**kwargs):
        assert kwargs["conversation_id"] == conversation["conversation_id"]
        assert kwargs["business_phone_id"] == "sender"
        db["messages_org"].rows.append({**kwargs, "id": kwargs["message_id"], "timestamp": datetime.now(timezone.utc)})
        events.append("new_message")

    modules = {
        "pymongo": types.SimpleNamespace(ReturnDocument=types.SimpleNamespace(AFTER=True)),
        "schemas.models": types.SimpleNamespace(MessageRole=types.SimpleNamespace(AGENT="agent")),
        "services.wa_service": types.SimpleNamespace(store_whatsapp_message=store),
        "services.websocket_service": types.SimpleNamespace(broadcast_main_ws=broadcast),
    }
    previous = {key: sys.modules.get(key) for key in modules}
    sys.modules.update(modules)
    context = {
        "asyncio": asyncio, "Optional": Optional, "datetime": datetime, "timezone": timezone,
        "timedelta": timedelta, "db": db, "json": json, "_uuid": uuid, "logger": logger,
        "HTTPException": HTTPException, "DuplicateKeyError": ValueError,
        "services": types.SimpleNamespace(org_convo_viewers={}),
        "normalize_phone": str, "phone_search_parts": lambda phone: (phone, [phone]),
        "name_search_prefixes": lambda name: [name], "serialize_mongo": lambda value: value,
        "whatsapp_conversation_id": lambda phone, sender: f"whatsapp_{phone}_{sender}",
        "sem": asyncio.Semaphore(20), "org_id": "org", "flow_id": "flow", "sheet_id": "sheet",
        "wa_id": "waba", "phone_id": "sender", "wa_connection": {"phone": "business"},
        "is_broadcast_suppressed": lambda *args: False, "SENT_COLLECTION": "results",
    }
    load("services/wa_service.py", {"create_or_update_whatsapp_conversation", "_is_definitive_meta_rejection"}, context)
    load("services/google_sheet_automation.py", {"_send_one", "_record_failed_sheet_send", "_mark_failed", "_try_claim_send"}, context)
    rejected = HTTPException(409, {"code": "whatsapp_reply_window_closed", "message": "Use an approved template."})
    outcome = rejected

    async def send(**kwargs):
        # Recipient preparation must not touch customer activity, assignment or follow-ups.
        current = db["conversations_org"].rows[0]
        assert current["needs_reply"] is True and current["status"] == "follow-up"
        assert current["unread_count"] == 3 and current["message_counter"] == 8
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    context.update(send_whatsapp_message=send, send_whatsapp_template_message=send)
    try:
        for template in (False, True):
            db["messages_org"].rows.clear()
            db["conversations_org"].rows = [copy.deepcopy(conversation)]
            events.clear()
            recipient = {"phone_number": "customer", "customer_name": "Sheet name", "resolved_text": "Hospital reminder", "is_template": template, "template_name": "reminder", "components": [{"type": "body", "parameters": [{"type": "text", "text": "Vaibhav"}]}]}
            result = await context["_send_one"](recipient)
            assert result == {"sent": 0, "errors": 1}
            assert events == ["new_message", "conversation_updated"]
            failed = copy.deepcopy(db["messages_org"].rows[0])
            assert failed["status"] == "failed" and failed["sender_name"] == "Automation"
            assert failed["delivery_error"]["code"] == "whatsapp_reply_window_closed"
            assert failed["type"] == ("template" if template else "text")
            if template:
                assert failed["payload"]["components"] == recipient["components"]
            current = db["conversations_org"].rows[0]
            assert current["last_message"].startswith("Not sent: ") and current["last_sender"] == "customer"
            assert current["needs_reply"] is True
            await context["_send_one"](recipient)
            assert db["messages_org"].rows == [failed], "retries must preserve one historical failed attempt"
            # A subsequent accepted send keeps the failure as history and records the provider ID.
            outcome = {"message_id": "wamid.accepted"}
            assert await context["_send_one"](recipient) == {"sent": 1, "errors": 0}
            assert db["results"].rows[0]["message_id"] == "wamid.accepted"
            assert db["messages_org"].rows == [failed]
            # Unknown outcomes and malformed successes neither fabricate failure bubbles nor retry.
            for outcome in (RuntimeError("timeout"), HTTPException(502, "unknown"), {}):
                count = len(db["messages_org"].rows)
                assert await context["_send_one"](recipient) == {"sent": 0, "errors": 1}
                assert len(db["messages_org"].rows) == count
                assert db["results"].rows[0]["status"] == "delivery_unknown"
                assert await context["_try_claim_send"]("org", "flow", "customer") is False
            # A confirmed failure before provider submission remains retryable.
            outcome = HTTPException(503, "Temporary backend failure")
            outcome.delivery_not_attempted = True
            assert await context["_send_one"](recipient) == {"sent": 0, "errors": 1}
            assert db["results"].rows[0]["status"] == "failed"
            assert await context["_try_claim_send"]("org", "flow", "customer") is True
            outcome = rejected

        # An existing recipient's preparation is strictly activity-free.
        db["conversations_org"].rows = [copy.deepcopy(conversation)]
        events.clear()
        await context["create_or_update_whatsapp_conversation"](
            org_id="org", recipient_id="waba", customer_phone_no="customer", customer_name="Sheet name",
            last_message="Unsent", last_sender="agent", business_phone_id="sender", record_activity=False,
        )
        assert db["conversations_org"].rows == [conversation] and not events
        context["resolve_active_whatsapp_connection"] = lambda *args, **kwargs: {"phone_id": "other", "slot": 2}
        new_id = await context["create_or_update_whatsapp_conversation"](
            org_id="org", recipient_id="waba", customer_phone_no="customer", customer_name="Sheet name",
            last_message="Unsent", last_sender="agent", business_phone_id="other", record_activity=False,
        )
        new = db["conversations_org"].find_one({"conversation_id": new_id})
        assert new_id != conversation["conversation_id"] and "last_message" not in new
        assert new["message_counter"] == 0 and new["unread_count"] == 0
        # Slow failure handling cannot replace newer customer activity.
        db["messages_org"].rows.clear()
        db["conversations_org"].rows[0]["last_message_timestamp"] = datetime.now(timezone.utc) + timedelta(days=1)
        snapshot = copy.deepcopy(db["conversations_org"].rows[0])
        await context["_record_failed_sheet_send"]("org", "flow", conversation["conversation_id"], recipient, "waba", "sender", {"details": "rejected"})
        assert db["conversations_org"].rows[0] == snapshot
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    # Accepted activity must follow history persistence in both send paths.
    source = (ROOT / "services/wa_service.py").read_text()
    for name, end in (("send_whatsapp_message", "send_whatsapp_template_message"), ("send_whatsapp_template_message", "send_wa_media_message")):
        start = source.index(f"async def {name}(")
        finish = source.find(f"async def {end}(", start + 1)
        block = source[start:finish if finish != -1 else None]
        assert block.index("await store_whatsapp_message(") < block.index("await broadcast_main_ws(")
    print("Google Sheets inbox failure checks passed.")


async def check_senders():
    """Exercise the actual shared senders, including their exception boundaries."""
    class Unavailable(RuntimeError):
        def __init__(self, category, **kwargs):
            super().__init__(category)
            self.category = category

    class ConnectionFailure(RuntimeError):
        pass

    class RequestError(RuntimeError):
        pass

    failure = None
    events, stored = [], []
    db = Db()
    db.whatsapp_outbound_sends = Collection()
    conversation = {"conversation_id": "whatsapp_customer_sender", "platform": "whatsapp", "customer_id": "customer", "whatsapp_id": "waba", "business_phone_id": "sender", "reply_window_ends_at": datetime.now(timezone.utc) + timedelta(hours=1)}
    db["conversations_org"].rows = [conversation]

    def resolve(*args, **kwargs):
        if failure == "before":
            raise RuntimeError("temporary lookup failure")
        return {"wa_id": "waba", "phone_id": "sender", "phone": "business", "access_token": "test"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            assert "/sender/messages" in url and kwargs["json"]["to"] == "customer"
            events.append("provider")
            if failure == "timeout":
                raise RequestError("timeout")
            rejected = failure == "rejected" or (failure == "partial" and events.count("provider") == 2)
            return types.SimpleNamespace(status_code=400 if rejected else 200, text="rejected", json=lambda: {"messages": [] if failure == "missing_id" else [{"id": "wamid.accepted"}]})

    async def store(**kwargs):
        if failure == "storage":
            raise RuntimeError("storage unavailable after acceptance")
        events.append("stored")
        stored.append(kwargs)
        return kwargs["message_id"]

    async def activity(**kwargs):
        assert "stored" in events
        assert kwargs["business_phone_id"] == "sender"
        events.append("activity")

    async def broadcast(**kwargs):
        assert "stored" in events
        events.append("broadcast")

    async def noop(*args, **kwargs):
        return True

    async def pricing(*args):
        return {"mode": "off"}

    modules = {
        "services.entitlement_service": types.SimpleNamespace(require_feature=lambda *args: None, reserve_quota_async=noop, commit_quota_async=noop, record_ai_usage_async=noop),
        "services.pricing_decision_service": types.SimpleNamespace(get_pricing_settings=pricing),
        "services.pricing_service": types.SimpleNamespace(is_pricing_enabled=lambda *args: False, validate_ai_pricing_response=noop),
        "services.ai_policy_service": types.SimpleNamespace(is_effective_ai_enabled=noop),
    }
    previous = {key: sys.modules.get(key) for key in modules}
    sys.modules.update(modules)
    context = {
        "asyncio": asyncio, "Optional": Optional, "time": time, "datetime": datetime, "timezone": timezone,
        "timedelta": timedelta, "uuid": uuid, "db": db, "normalize_phone": str, "IG_VERSION": "test",
        "HTTPException": HTTPException, "AIProviderUnavailable": Unavailable, "ConnectionFailure": ConnectionFailure,
        "ReplyOperationConflict": type("ReplyOperationConflict", (RuntimeError,), {}),
        "ReturnDocument": types.SimpleNamespace(AFTER=True), "MSG_SPLIT": "|MSG_SPLIT|",
        "MessageRole": types.SimpleNamespace(AGENT="agent", AI="ai"),
        "httpx": types.SimpleNamespace(AsyncClient=Client, RequestError=RequestError), "_meta_http_client": Client,
        "logger": types.SimpleNamespace(**{name: lambda *a, **kw: None for name in ("error", "warning", "info", "exception")}),
        "resolve_outbound_whatsapp_connection": resolve,
        "claim_reply_manifest": lambda *args: {"contents": args[5]}, "claim_reply": lambda *args: None,
        "start_provider": lambda *args: True, "complete_reply": lambda *args: True,
        "release_claim": lambda *args, **kwargs: None, "mark_unknown": lambda *args: None,
        "store_whatsapp_message": store, "create_or_update_whatsapp_conversation": activity, "broadcast_main_ws": broadcast,
    }
    load("services/wa_service.py", {"send_whatsapp_message", "send_whatsapp_template_message", "ensure_whatsapp_reply_window_open", "_utc_datetime", "_is_definitive_meta_rejection"}, context)
    try:
        for template in (False, True):
            async def send(mode="reply"):
                if template:
                    return await context["send_whatsapp_template_message"]("customer", {"name": "reminder", "language": "en"}, "waba", "org", None, mode, None, None, conversation_id=conversation["conversation_id"])
                content = "First|MSG_SPLIT|Second" if failure == "partial" else "Hospital reminder"
                return await context["send_whatsapp_message"]({"conversation_id": conversation["conversation_id"], "content": content, "sender_id": "waba"}, "org", mode)

            for mode in (["reply", "private"] if template else ["reply", "private", "ai"]):
                failure = None
                events.clear()
                stored.clear()
                assert (await send(mode))["message_id"]
                assert len(stored) == 1 and stored[0]["business_phone_id"] == "sender"
                assert events.count("provider") == (0 if mode == "private" else 1)
                assert stored[0]["status"] == ("delivered" if mode == "private" else "sending")
            for failure in ("before", "rejected", "timeout", "missing_id", "storage"):
                events.clear()
                try:
                    await send()
                except (HTTPException, Unavailable) as error:
                    safe_to_retry = getattr(error, "delivery_not_attempted", False)
                    assert safe_to_retry == (failure in {"before", "rejected"}), (template, failure, safe_to_retry)
                else:
                    raise AssertionError((template, failure, "failure incorrectly reported as success"))
                assert "activity" not in events and "broadcast" not in events
            if not template:
                failure = "partial"
                events.clear()
                try:
                    await send()
                except HTTPException as error:
                    assert error.delivery_not_attempted is False, "partial acceptance must not retry the entire payload"
                else:
                    raise AssertionError("partial rejection was ignored")
    finally:
        for key, value in previous.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
    print("Actual text/template senders passed: replies, private notes, AI, pre-send failures, rejection, timeout, missing IDs, storage failure and partial acceptance.")


if __name__ == "__main__":
    asyncio.run(check())
    asyncio.run(check_senders())
