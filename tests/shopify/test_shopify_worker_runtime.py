#!/usr/bin/env python3
"""Dependency-free behavioral checks for the Shopify reminder worker.

Run with ``python3 tests/shopify/test_shopify_worker_runtime.py``.  The production
module is loaded with tiny stand-ins for optional packages, then exercised against
an in-memory Mongo subset.  This intentionally tests runtime transitions instead
of grepping their source.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import re
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
NOW = datetime.now(timezone.utc)


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def _module(name: str, **values):
    module = types.ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def load_service():
    class Fernet:
        def __init__(self, *_args, **_kwargs):
            pass

    class InvalidToken(Exception):
        pass

    class DuplicateKeyError(Exception):
        pass

    class PyMongoError(Exception):
        pass

    class MessageRole:
        AGENT = "agent"

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

    httpx = _module("httpx", AsyncClient=AsyncClient, HTTPError=Exception, RequestError=Exception)
    httpx.Response = object
    cryptography = _module("cryptography")
    cryptography.fernet = _module("cryptography.fernet", Fernet=Fernet, InvalidToken=InvalidToken)
    _module("fastapi", HTTPException=HTTPException)
    _module("loguru", logger=_Logger())
    pymongo = _module("pymongo", ReturnDocument=types.SimpleNamespace(AFTER="after"), UpdateOne=object)
    pymongo.errors = _module(
        "pymongo.errors",
        DuplicateKeyError=DuplicateKeyError,
        PyMongoError=PyMongoError,
    )
    config = _module("config")
    config.settings = _module(
        "config.settings",
        SHOPIFY_CLIENT_ID="client",
        SHOPIFY_CLIENT_SECRET="secret",
        SHOPIFY_PUBLIC_BASE_URL="https://example.test",
        WA_VERSION="v24.0",
    )
    _module(
        "database",
        get_async_mongo_db=lambda: FakeDB(),
        get_mongo_db=lambda: FakeDB(),
    )
    schemas = _module("schemas")
    schemas.models = _module("schemas.models", MessageRole=MessageRole)
    _module(
        "services.wa_service",
        create_or_update_whatsapp_conversation=async_noop,
        store_whatsapp_message=async_noop,
    )
    _module(
        "services.websocket_service",
        broadcast_org_admin_ws=async_noop,
        broadcast_org_ws=async_noop,
    )
    _module("services.sales_workflow_service", verified_outcome=async_noop)
    spec = importlib.util.spec_from_file_location(
        "shopify_service_runtime_test", ROOT / "services/shopify_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    sys.modules["services.shopify_service"] = module
    import services
    services.shopify_service = module
    spec.loader.exec_module(module)
    return module


async def async_noop(*_args, **_kwargs):
    return None


def _match_value(exists: bool, value, condition) -> bool:
    if not isinstance(condition, dict) or not any(str(key).startswith("$") for key in condition):
        return (not exists or value is None) if condition is None else exists and value == condition
    for operator, expected in condition.items():
        if operator == "$exists" and exists is not bool(expected):
            return False
        if operator == "$in" and (not exists or value not in expected):
            return False
        if operator == "$nin" and exists and value in expected:
            return False
        if operator == "$ne" and exists and value == expected:
            return False
        if operator == "$lt" and (not exists or not value < expected):
            return False
        if operator == "$lte" and (not exists or not value <= expected):
            return False
        if operator == "$gt" and (not exists or not value > expected):
            return False
        if operator == "$gte" and (not exists or not value >= expected):
            return False
        if operator == "$not" and _match_value(exists, value, expected):
            return False
    return True


def matches(document: dict, query: dict) -> bool:
    for key, condition in query.items():
        if key == "$or":
            if not any(matches(document, branch) for branch in condition):
                return False
        elif key == "$and":
            if not all(matches(document, branch) for branch in condition):
                return False
        elif key == "$nor":
            if any(matches(document, branch) for branch in condition):
                return False
        else:
            if not _match_value(key in document, document.get(key), condition):
                return False
    return True


def apply_update(document: dict, update: dict) -> None:
    for key, value in update.get("$set", {}).items():
        document[key] = copy.deepcopy(value)
    for key in update.get("$unset", {}):
        document.pop(key, None)
    for key, value in update.get("$inc", {}).items():
        document[key] = document.get(key, 0) + value
    for key, value in update.get("$push", {}).items():
        target = document.setdefault(key, [])
        if isinstance(value, dict) and "$each" in value:
            target.extend(copy.deepcopy(value["$each"]))
            if value.get("$slice") is not None:
                document[key] = target[value["$slice"]:]
        else:
            target.append(copy.deepcopy(value))


class FakeCursor:
    def __init__(self, documents, projection=None):
        self.documents = list(documents)
        self.position = 0
        self.projection = projection

    def project(self, document):
        if not self.projection:
            return copy.deepcopy(document)
        include = {key for key, value in self.projection.items() if value}
        return {key: copy.deepcopy(value) for key, value in document.items() if key in include or key == "_id"}

    def sort(self, keys, direction=None):
        pairs = [(keys, direction)] if isinstance(keys, str) else keys
        for key, order in reversed(pairs):
            self.documents.sort(key=lambda item: item.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=order < 0)
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self

    async def to_list(self, length=None):
        return [self.project(document) for document in self.documents[:length]]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.position >= len(self.documents):
            raise StopAsyncIteration
        value = self.project(self.documents[self.position])
        self.position += 1
        return value


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = [copy.deepcopy(item) for item in (documents or [])]

    async def insert_one(self, document):
        row = copy.deepcopy(document)
        row.setdefault("_id", len(self.documents) + 1)
        for existing in self.documents:
            if existing.get("_id") == row["_id"] or (row.get("provider_id") and existing.get("org_id") == row.get("org_id") and existing.get("provider_id") == row["provider_id"]):
                raise sys.modules["pymongo.errors"].DuplicateKeyError("Duplicate record")
        self.documents.append(row)
        return types.SimpleNamespace(inserted_id=row["_id"])

    async def update_many(self, query, update):
        modified = 0
        for document in self.documents:
            if matches(document, query):
                apply_update(document, update)
                modified += 1
        return types.SimpleNamespace(matched_count=modified, modified_count=modified)

    async def update_one(self, query, update, upsert=False):
        for document in self.documents:
            if matches(document, query):
                apply_update(document, update)
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            document = {key: value for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
            document.update(copy.deepcopy(update.get("$setOnInsert", {})))
            apply_update(document, update)
            self.documents.append(document)
            return types.SimpleNamespace(matched_count=0, modified_count=0, upserted_id=len(self.documents))
        return types.SimpleNamespace(matched_count=0, modified_count=0)

    async def find_one_and_update(self, query, update, sort=None, return_document=None, upsert=False):
        candidates = [item for item in self.documents if matches(item, query)]
        if sort:
            for key, order in reversed(sort):
                candidates.sort(key=lambda item: item.get(key) or datetime.min.replace(tzinfo=timezone.utc), reverse=order < 0)
        if candidates:
            before = copy.deepcopy(candidates[0])
            apply_update(candidates[0], update)
            return before if return_document is False else copy.deepcopy(candidates[0])
        if upsert:
            if any(item.get("_id") == query.get("_id") for item in self.documents) and query.get("_id") is not None:
                raise sys.modules["pymongo.errors"].DuplicateKeyError("Duplicate _id")
            await self.update_one(query, update, upsert=True)
            return copy.deepcopy(self.documents[-1])
        return None

    async def find_one_and_delete(self, query):
        for index, document in enumerate(self.documents):
            if matches(document, query):
                return self.documents.pop(index)
        return None

    async def find_one(self, query, projection=None, sort=None):
        cursor = self.find(query, projection)
        if sort:
            cursor.sort(sort)
        return cursor.project(cursor.documents[0]) if cursor.documents else None

    def find(self, query, projection=None):
        documents = [item for item in self.documents if matches(item, query)]
        return FakeCursor(documents, projection)

    async def distinct(self, key, query):
        return list({item.get(key) for item in self.documents if matches(item, query) and item.get(key) is not None})

    async def count_documents(self, query):
        return sum(matches(item, query) for item in self.documents)


class ProviderFenceCollection(FakeCollection):
    """Signals if control returns to the event loop after provider-start CAS."""

    def __init__(self, documents=None):
        super().__init__(documents)
        self.provider_started = False
        self.yielded_after_start = False

    async def find_one_and_update(self, query, update, **kwargs):
        result = await super().find_one_and_update(query, update, **kwargs)
        if result and "provider_started_at" in update.get("$set", {}):
            self.provider_started = True
            asyncio.get_running_loop().call_soon(self._mark_yielded)
        return result

    def _mark_yielded(self):
        if self.provider_started:
            self.yielded_after_start = True


class FakeDB:
    def __init__(self, **collections):
        self.collections = collections

    def __getattr__(self, name):
        return self.collections.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self.collections.setdefault(name, FakeCollection())


class SyncFakeCollection:
    """Small synchronous collection for the legacy Inbox persistence lookup."""

    def __init__(self, documents=None):
        self.documents = [copy.deepcopy(item) for item in (documents or [])]

    def find_one(self, query, projection=None):
        for document in self.documents:
            if matches(document, query):
                if not projection:
                    return copy.deepcopy(document)
                include = {key for key, value in projection.items() if value}
                return {key: copy.deepcopy(value) for key, value in document.items() if key in include or key == "_id"}
        return None

    def update_one(self, query, update):
        for document in self.documents:
            if matches(document, query):
                apply_update(document, update)
                return types.SimpleNamespace(matched_count=1, modified_count=1)
        return types.SimpleNamespace(matched_count=0, modified_count=0)


class SyncFakeDB:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.setdefault(name, SyncFakeCollection())


def checkout(identifier: str, **overrides):
    value = {
        "_id": identifier,
        "org_id": "org",
        "provider_id": identifier,
        "phone": "+14155550100",
        "customer_name": "Customer",
        "whatsapp_consent": "SUBSCRIBED",
        "products": [{"name": "Board", "quantity": 1}],
        "recovery_url": "https://shop.test/recover/token",
        "record_status": "abandoned",
        "provider_created_at": NOW - timedelta(minutes=20),
        "provider_updated_at": NOW - timedelta(minutes=20),
        "reminder_status": "pending",
        "reminder_due_at": NOW - timedelta(minutes=1),
    }
    value.update(overrides)
    return value


async def run_worker(service, database, send):
    service.db = database
    service._send_claimed_checkout = send
    service._reconcile_accepted = async_noop
    service._finish_pending_disconnects = async_noop
    await service.process_shopify_reminders()


async def test_hanging_checkout_does_not_block_next(service):
    database = FakeDB(shopify_checkouts=FakeCollection([
        checkout("hang", reminder_due_at=NOW - timedelta(minutes=2)),
        checkout("fast", phone="+14155550101"),
    ]))
    fast_seen = asyncio.Event()
    never = asyncio.Event()

    async def send(item, _token):
        if item["_id"] == "hang":
            await never.wait()
        fast_seen.set()

    task = asyncio.create_task(run_worker(service, database, send))
    try:
        await asyncio.wait_for(fast_seen.wait(), timeout=0.3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_workers_claim_once(service):
    database = FakeDB(shopify_checkouts=FakeCollection([checkout("only")]))
    attempts = 0

    async def send(_item, _token):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0)

    await asyncio.gather(run_worker(service, database, send), run_worker(service, database, send))
    assert attempts == 1, f"expected one provider attempt, got {attempts}"


async def test_database_outage_defers_worker_ticks(service):
    class OfflineCollection:
        async def update_one(self, *_args, **_kwargs):
            raise service.PyMongoError("offline")

        async def update_many(self, *_args, **_kwargs):
            raise service.PyMongoError("offline")

    service.db = FakeDB(
        shopify_connections=OfflineCollection(),
        shopify_worker_heartbeats=OfflineCollection(),
    )
    await service.process_shopify_syncs()
    await service.process_shopify_reminders()


async def test_expired_claim_recovery(service):
    retry = checkout(
        "retry",
        reminder_status="sending",
        claim_token="old",
        claim_expires_at=NOW - timedelta(minutes=1),
    )
    unknown = checkout(
        "unknown",
        phone="+14155550102",
        reminder_status="sending",
        claim_token="old-2",
        claim_expires_at=NOW - timedelta(minutes=1),
        provider_started_at=NOW - timedelta(minutes=2),
    )
    collection = FakeCollection([retry, unknown])
    seen = []

    async def send(item, _token):
        seen.append(item["_id"])

    await run_worker(service, FakeDB(shopify_checkouts=collection), send)
    assert seen == ["retry"]
    recovered = {item["_id"]: item for item in collection.documents}
    assert recovered["unknown"]["reminder_status"] == "delivery_unknown"
    assert recovered["unknown"].get("reminder_due_at") is None


async def test_checkout_update_and_order_races(service):
    original = checkout(
        "cart",
        reminder_status="sending",
        claim_token="claim",
        provider_updated_at=NOW - timedelta(minutes=20),
    )
    collection = FakeCollection([original])
    service.db = FakeDB(shopify_checkouts=collection)
    service._commerce_event = async_noop
    service._repair_org_checkout_states = async_noop

    async def changed_graphql(*_args, **_kwargs):
        return {"node": {
            "id": "cart", "abandonedCheckoutUrl": original["recovery_url"],
            "createdAt": (NOW - timedelta(minutes=30)).isoformat(),
            "updatedAt": NOW.isoformat(), "completedAt": None,
            "customer": {"firstName": "Customer", "lastName": ""},
            "shippingAddress": {"phone": original["phone"]}, "billingAddress": None,
            "lineItems": {"nodes": [{"id": "p", "title": "Board", "quantity": 2, "originalTotalPriceSet": {"shopMoney": {"amount": "20", "currencyCode": "USD"}}}]},
            "totalPriceSet": {"shopMoney": {"amount": "20", "currencyCode": "USD"}},
        }}

    service._graphql = changed_graphql
    result = await service._refresh_checkout_before_send(original, "claim", {"org_id": "org", "shop": "shop", "reminder_delay_minutes": 10})
    assert result is None
    updated = collection.documents[0]
    assert updated["reminder_status"] == "pending"
    assert updated["reminder_due_at"] >= NOW + timedelta(minutes=9, seconds=50)

    updated.update({"reminder_status": "sending", "claim_token": "claim-2"})

    async def converted_graphql(*_args, **_kwargs):
        payload = await changed_graphql()
        payload["node"]["completedAt"] = NOW.isoformat()
        return payload

    service._graphql = converted_graphql
    await service._refresh_checkout_before_send(updated.copy(), "claim-2", {"org_id": "org", "shop": "shop", "reminder_delay_minutes": 10})
    assert collection.documents[0]["reminder_status"] == "converted"


async def test_refresh_shape_change_does_not_restart_elapsed_timer(service):
    original = checkout(
        "cart-shape",
        reminder_status="sending",
        claim_token="claim",
        provider_updated_at=NOW - timedelta(minutes=20),
        products=[{"name": "Board", "quantity": 1}],
    )
    collection = FakeCollection([original])
    service.db = FakeDB(shopify_checkouts=collection)
    service._commerce_event = async_noop
    service._repair_org_checkout_states = async_noop

    async def same_activity_graphql(*_args, **_kwargs):
        return {"node": {
            "id": original["provider_id"], "abandonedCheckoutUrl": original["recovery_url"],
            "createdAt": original["provider_created_at"].isoformat(),
            "updatedAt": original["provider_updated_at"].isoformat(), "completedAt": None,
            "customer": {"firstName": "Customer", "lastName": ""},
            "shippingAddress": {"phone": original["phone"]}, "billingAddress": None,
            "lineItems": {"nodes": [{
                "id": "gid://shopify/AbandonedCheckoutLineItem/1", "title": "Board", "quantity": 1,
                "originalTotalPriceSet": {"shopMoney": {"amount": "20", "currencyCode": "USD"}},
            }]},
            "totalPriceSet": {"shopMoney": {"amount": "20", "currencyCode": "USD"}},
        }}

    service._graphql = same_activity_graphql
    refreshed = await service._refresh_checkout_before_send(
        original.copy(), "claim", {"org_id": "org", "shop": "shop.test", "reminder_delay_minutes": 10},
    )
    assert refreshed is not None, "equivalent Shopify payload shape restarted an elapsed timer"
    assert refreshed["reminder_status"] == "sending"
    assert refreshed["claim_token"] == "claim"
    assert refreshed["products"][0]["id"].endswith("/1")


async def test_newest_checkout_per_phone(service):
    old = checkout("old", provider_updated_at=NOW - timedelta(minutes=5))
    new = checkout("new", provider_updated_at=NOW - timedelta(minutes=1))
    collection = FakeCollection([old, new])
    service.db = FakeDB(shopify_checkouts=collection)
    service._configuration_ready = lambda *_args, **_kwargs: _true()
    service._is_whatsapp_suppressed = lambda *_args, **_kwargs: _false()
    await service._reconcile_phone_checkouts(
        "org", old["phone"],
        {"automation_enabled": True, "automation_enabled_at": NOW - timedelta(hours=1), "reminder_delay_minutes": 10},
        configured=True,
    )
    states = {item["_id"]: item["reminder_status"] for item in collection.documents}
    assert states == {"old": "superseded", "new": "pending"}


async def _true():
    return True


async def _false():
    return False


async def test_stop_suppression(service):
    messages = FakeCollection([{
        "conversation_id": "whatsapp_14155550100", "role": "customer",
        "mode": None, "content": " STOP! ", "timestamp": NOW,
    }])
    database = FakeDB(
        whatsapp_suppressions=FakeCollection(),
        messages_org=messages,
    )
    database.collections["messages_org"] = messages
    service.db = database
    assert await service._is_whatsapp_suppressed("org", "+14155550100")
    assert database.whatsapp_suppressions.documents[0]["reason"] == "customer_opt_out"


async def test_websocket_hang_is_bounded(service):
    never = asyncio.Event()

    async def hanging_broadcast(*_args, **_kwargs):
        await never.wait()

    service.broadcast_org_ws = hanging_broadcast
    await asyncio.wait_for(service._commerce_event("org", "checkout", "cart"), timeout=1.3)


def _configured_send_db(collection):
    return FakeDB(
        shopify_checkouts=collection,
        shopify_orders=FakeCollection(),
        shopify_connections=FakeCollection([{
            "_id": "connection", "org_id": "org", "shop": "shop.test",
            "status": "connected", "automation_enabled": True, "automation_enabled_at": NOW - timedelta(hours=1),
            "whatsapp_id": "waba", "whatsapp_phone_id": "phone",
            "template_name": "cart", "template_language": "en",
            "template_status": "APPROVED", "reminder_delay_minutes": 10,
        }]),
        whatsapp_connections=FakeCollection([{
            "_id": "wa-connection",
            "org_id": "org", "wa_id": "waba", "phone_id": "phone",
            "access_token": "token", "is_active": True,
        }]),
        whatsapp_suppressions=FakeCollection(),
    )


def _prepare_send(service, database):
    service.db = database
    service._refresh_checkout_before_send = lambda item, *_args, **_kwargs: _value(item)
    service._is_whatsapp_suppressed = lambda *_args, **_kwargs: _false()
    service._selected_whatsapp_connection = lambda *_args, **_kwargs: _value({
        "wa_id": "waba", "phone_id": "phone", "access_token": "token",
    })
    service.list_compatible_templates = lambda *_args, **_kwargs: _value([{
        "name": "cart", "language": "en", "status": "APPROVED", "category": "MARKETING",
    }])
    service._commerce_event = async_noop


async def _value(value):
    return value


async def test_provider_fence_timeout_is_unknown_and_not_retried(service):
    item = checkout("fence", reminder_status="sending", claim_token="claim")
    collection = ProviderFenceCollection([item])
    database = _configured_send_db(collection)
    _prepare_send(service, database)
    posts = 0
    fence_was_crossed = False

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal posts, fence_was_crossed
            posts += 1
            fence_was_crossed = collection.yielded_after_start
            raise service.httpx.RequestError("timeout")

    service.httpx.AsyncClient = Client
    await service._send_claimed_checkout(item.copy(), "claim")
    stored = collection.documents[0]
    assert posts == 1
    assert not fence_was_crossed, "await occurred after provider-start CAS and before Meta POST"
    assert stored["reminder_status"] == "delivery_unknown"
    await service._send_claimed_checkout(stored.copy(), "claim")
    assert posts == 1, "ambiguous provider outcome was retried"


async def test_acceptance_precedes_inbox_and_reconciles_once(service, parameter_count=3):
    item = checkout("accepted", reminder_status="sending", claim_token="claim")
    collection = FakeCollection([item])
    database = _configured_send_db(collection)
    _prepare_send(service, database)
    database.shopify_connections.documents[0]["template_parameter_count"] = parameter_count
    if parameter_count == 2:
        item["products"] = []  # The two-variable template does not require product text.
        collection.documents[0]["products"] = []
    posts = 0

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.1"}]}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal posts
            posts += 1
            parameters = _kwargs["json"]["template"]["components"][0]["parameters"]
            assert len(parameters) == parameter_count
            assert parameters[-1]["text"].startswith("https://")
            if parameter_count == 3:
                assert parameters[1]["text"] == service._product_summary(item["products"])
            return Response()

    service.httpx.AsyncClient = Client
    await service._send_claimed_checkout(item.copy(), "claim")
    stored = collection.documents[0]
    assert stored["reminder_status"] == "sent", stored
    assert stored["provider_message_id"] == "wamid.1"
    assert stored.get("inbox_sync_status") == "pending"
    assert posts == 1

    messages = SyncFakeCollection()
    service.sync_db = SyncFakeDB(messages_org=messages)
    conversation_calls = 0
    store_calls = 0
    fail_store = True

    async def create_conversation(**kwargs):
        nonlocal conversation_calls
        conversation_calls += 1
        assert kwargs["idempotency_key"] == "wamid.1"
        return "whatsapp_14155550100"

    async def store_message(**kwargs):
        nonlocal store_calls, fail_store
        store_calls += 1
        assert kwargs["message_id"] == "wamid.1"
        if fail_store:
            fail_store = False
            raise RuntimeError("Inbox unavailable")
        messages.documents.append({
            "_id": "message-1",
            "message_id": kwargs["message_id"],
            "conversation_id": kwargs["conversation_id"],
        })

    service.create_or_update_whatsapp_conversation = create_conversation
    service.store_whatsapp_message = store_message
    service._finish_pending_disconnects = async_noop

    # Meta acceptance is durable before the first Inbox attempt, which fails.
    await service.process_shopify_reminders()
    stored = collection.documents[0]
    assert stored["reminder_status"] == "sent"
    assert stored["provider_message_id"] == "wamid.1"
    assert stored["inbox_sync_status"] == "retryable"
    assert stored["inbox_attempts"] == 1
    assert posts == 1, "Inbox failure repeated the Meta send"

    # Make the independently scheduled Inbox retry due, then run it successfully.
    stored["inbox_next_attempt_at"] = service.utcnow() - timedelta(seconds=1)
    await service.process_shopify_reminders()
    stored = collection.documents[0]
    assert stored["inbox_sync_status"] == "complete"
    assert len(messages.documents) == 1
    assert conversation_calls == 2
    assert store_calls == 2
    assert posts == 1, "Inbox retry repeated the Meta send"

    # A later worker pass must observe completion and do nothing.
    await service.process_shopify_reminders()
    assert len(messages.documents) == 1
    assert conversation_calls == 2
    assert store_calls == 2
    assert posts == 1


async def test_order_event_sends_approved_template_once(service, parameter_count=3, event="received"):
    job = {
        "_id": "order-job", "org_id": "org", "order_provider_id": "gid://shopify/Order/1",
        "event": event, "event_at": NOW, "status": "sending", "claim_token": "claim",
        **({"tracking": {"company": "Blue Dart", "number": "123456789", "url": "https://track.example/123456789"}} if event == "shipped" else {}),
    }
    notifications = FakeCollection([job])
    service.db = FakeDB(
        shopify_order_notifications=notifications,
        shopify_orders=FakeCollection([{
            "_id": "order", "org_id": "org", "provider_id": job["order_provider_id"], "record_status": "active",
            "phone": "+14155550100", "customer_name": "Customer", "order_number": "#1001",
            "products": [{"name": "Board", "quantity": 2}] if parameter_count == 3 else [], "provider_created_at": NOW,
        }]),
        shopify_connections=FakeCollection([{
            "org_id": "org", "status": "connected", "order_notifications_enabled": True,
            "order_notifications_enabled_at": NOW - timedelta(hours=1),
            "whatsapp_id": "waba", "whatsapp_phone_id": "phone",
            "order_templates": {event: {"name": f"order_{event}", "language": "en", "status": "APPROVED", "parameter_count": parameter_count}},
        }]),
        whatsapp_connections=FakeCollection([{"_id": "wa", "org_id": "org", "wa_id": "waba", "phone_id": "phone", "is_active": True}]),
    )
    service._is_whatsapp_suppressed = lambda *_args, **_kwargs: _false()
    service._selected_whatsapp_connection = lambda *_args, **_kwargs: _value({"wa_id": "waba", "phone_id": "phone", "access_token": "token"})
    service._commerce_event = async_noop
    posts = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"messages": [{"id": "wamid.order.1"}]}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            posts.append(kwargs["json"])
            return Response()

    service.httpx.AsyncClient = Client
    await service._send_order_notification(job.copy(), "claim")
    stored = notifications.documents[0]
    assert stored["status"] == "sent", stored
    assert stored["provider_message_id"] == "wamid.order.1"
    assert len(posts) == 1
    assert posts[0]["template"]["name"] == f"order_{event}"
    expected = ["#1001", "Blue Dart: 123456789 | Track: https://track.example/123456789"] if event == "shipped" else ["#1001"] if parameter_count == 1 else ["Customer", "#1001", "Board x 2"]
    assert [item["text"] for item in posts[0]["template"]["components"][0]["parameters"]] == expected


async def test_shipped_waits_for_tracking_update(service):
    class Notifications(FakeCollection):
        async def insert_one(self, document):
            if self.documents:
                raise service.DuplicateKeyError("already queued")
            self.documents.append(copy.deepcopy(document))

    notifications = Notifications()
    orders = FakeCollection([{"org_id": "org", "provider_id": "gid://shopify/Order/1", "provider_created_at": NOW}])
    service.db = FakeDB(shopify_order_notifications=notifications, shopify_orders=orders)
    service._commerce_event = async_noop
    connection = {"org_id": "org", "shop": "example.myshopify.com", "order_notifications_enabled": True,
                  "order_notifications_enabled_at": NOW - timedelta(hours=1),
                  "order_templates": {"shipped": {"status": "APPROVED", "parameter_count": 2}}}
    await service._apply_webhook(connection, "fulfillments/create", {"order_id": 1}, NOW)
    assert not notifications.documents
    connection["order_templates"]["shipped"]["parameter_count"] = 3
    await service._apply_webhook(connection, "fulfillments/update", {"order_id": 1, "tracking_number": "123456789"}, NOW)
    assert not notifications.documents
    connection["order_templates"]["shipped"]["parameter_count"] = 2
    await service._apply_webhook(connection, "fulfillments/update", {
        "order_id": 1, "tracking_company": "Blue Dart", "tracking_number": "123456789",
        "tracking_url": "https://track.example/123456789",
    }, NOW)
    assert len(notifications.documents) == 1
    assert notifications.documents[0]["tracking"]["number"] == "123456789"
    assert orders.documents[0]["tracking"]["company"] == "Blue Dart"
    await service._apply_webhook(connection, "fulfillments/update", {
        "order_id": 1, "tracking_company": "Blue Dart", "tracking_number": "987654321",
    }, NOW)
    assert len(notifications.documents) == 1
    assert notifications.documents[0]["tracking"]["number"] == "987654321"


async def test_immediate_meta_rejection_is_visible_and_retried(service):
    job = {
        "_id": "order-job", "org_id": "org", "order_provider_id": "order-1",
        "event": "received", "event_at": NOW, "status": "sending", "claim_token": "claim",
        "attempt_count": 1,
    }
    notifications = FakeCollection([job])
    service.db = FakeDB(
        shopify_order_notifications=notifications,
        shopify_orders=FakeCollection([{
            "org_id": "org", "provider_id": "order-1", "record_status": "active",
            "phone": "+14155550100", "customer_name": "Customer", "order_number": "#1001",
            "products": [{"name": "Board", "quantity": 1}], "provider_created_at": NOW,
        }]),
        shopify_connections=FakeCollection([{
            "org_id": "org", "status": "connected", "order_notifications_enabled": True,
            "order_notifications_enabled_at": NOW - timedelta(hours=1),
            "order_templates": {"received": {"name": "order_received", "language": "en", "status": "APPROVED"}},
        }]),
        whatsapp_connections=FakeCollection([{"_id": "wa", "org_id": "org", "wa_id": "waba", "phone_id": "phone", "is_active": True}]),
    )
    messages = SyncFakeCollection()
    service.sync_db = SyncFakeDB(messages_org=messages)
    service._is_whatsapp_suppressed = lambda *_args, **_kwargs: _false()
    service._selected_whatsapp_connection = lambda *_args, **_kwargs: _value({"wa_id": "waba", "phone_id": "phone", "access_token": "token"})
    service._commerce_event = async_noop

    async def create_conversation(**kwargs):
        assert kwargs["business_phone_id"] == "phone"
        return "whatsapp_phone_14155550100"

    async def store_message(**kwargs):
        messages.documents.append({
            "message_id": kwargs["message_id"], "status": kwargs["status"],
            "delivery_error": kwargs["delivery_error"], "ai_metadata": kwargs["bot_context"],
        })

    service.create_or_update_whatsapp_conversation = create_conversation
    service.store_whatsapp_message = store_message

    class Response:
        status_code = 429
        headers = {"content-type": "application/json"}

        @staticmethod
        def json():
            return {"error": {"code": 130429, "message": "Rate limit reached", "error_data": {"details": "Try again later"}}}

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    service.httpx.AsyncClient = Client
    await service._send_order_notification(job.copy(), "claim")
    stored = notifications.documents[0]
    assert stored["status"] == "retryable", stored
    assert stored["auto_retry_count"] == 1
    assert stored["provider_error"]["code"] == 130429
    assert stored["provider_error"]["retry_state"] == "scheduled"
    assert "provider_started_at" not in stored
    assert messages.documents[0]["status"] == "failed", messages.documents[0]
    assert messages.documents[0]["delivery_error"]["retry_state"] == "scheduled"


async def test_async_meta_failure_keeps_terminal_reason(service):
    notifications = FakeCollection([{
        "_id": "job", "org_id": "org", "order_provider_id": "order-1", "event": "received",
        "status": "sent", "provider_message_id": "wamid.failed", "auto_retry_count": 3,
        "send_whatsapp_id": "waba", "send_phone_id": "phone", "attempt_count": 4,
    }])
    service.db = FakeDB(
        shopify_order_notifications=notifications,
        shopify_orders=FakeCollection([{
            "org_id": "org", "provider_id": "order-1", "phone": "+14155550100",
            "customer_name": "Customer", "order_number": "#1001", "products": [{"name": "Board"}],
        }]),
    )
    messages = SyncFakeCollection([{"_id": "message", "message_id": "wamid.failed", "status": "sent"}])
    service.sync_db = SyncFakeDB(messages_org=messages)
    service._commerce_event = async_noop
    error = service.normalize_whatsapp_delivery_error(
        {"errors": [{"code": 131026, "title": "Message undeliverable", "error_data": {"details": "Recipient unavailable"}}]},
        stage="delivery",
    )
    returned = await service.update_shopify_reminder_delivery("org", "wamid.failed", "failed", error)
    stored = notifications.documents[0]
    assert returned["code"] == 131026 and returned["retry_state"] == "terminal"
    assert stored["status"] == "failed"
    assert stored["inbox_sync_status"] == "complete", stored
    assert "provider_message_id" not in stored
    assert messages.documents[0]["status"] == "failed", messages.documents[0]
    assert messages.documents[0]["delivery_error"]["details"] == "Recipient unavailable"


async def test_late_failure_does_not_regress_delivered_notification(service):
    notifications = FakeCollection([{
        "_id": "job", "org_id": "org", "order_provider_id": "order-1", "event": "received",
        "status": "sent", "provider_message_id": "wamid.delivered", "delivery_status": "delivered",
        "delivery_rank": 2,
    }])
    service.db = FakeDB(shopify_order_notifications=notifications)
    result = await service.update_shopify_reminder_delivery(
        "org", "wamid.delivered", "failed",
        service.normalize_whatsapp_delivery_error({"errors": [{"code": 131026}]}, stage="delivery"),
    )
    assert result is None
    assert notifications.documents[0]["delivery_status"] == "delivered"
    assert notifications.documents[0]["provider_message_id"] == "wamid.delivered"


def test_delays_and_product_summary(service):
    assert {5, 10, 15}.issubset(service.ALLOWED_DELAYS)
    default = getattr(service, "DEFAULT_REMINDER_DELAY_MINUTES", 30)
    assert default == 10
    assert "read_fulfillments" in service.OAUTH_SCOPES
    assert service.ORDER_EVENT_TOPICS == {
        "orders/create": "received", "fulfillments/create": "shipped",
        "orders/fulfilled": "fulfilled", "orders/cancelled": "cancelled",
    }
    assert "FULFILLMENTS_UPDATE" in service.WEBHOOK_TOPICS
    assert service._tracking_details({"company": "Blue Dart", "number": "123456789", "url": ""}) == "Blue Dart: 123456789"
    compliant_template = {
        "status": "APPROVED", "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": "Hi {{1}}, order {{2}} for {{3}} is ready."},
            {"type": "FOOTER", "text": service.SHOPIFY_TEMPLATE_FOOTER},
        ],
    }
    assert service._template_compatible(compliant_template, "order")
    order_id_template = {**compliant_template, "components": [{"type": "BODY", "text": "Order {{1}} has been received."}, compliant_template["components"][1]]}
    assert service._template_compatible(order_id_template, "order")
    assert service._template_parameter_count(order_id_template) == 1
    tracking_template = {**compliant_template, "components": [{"type": "BODY", "text": "Order {{1}} tracking: {{2}}."}, compliant_template["components"][1]]}
    assert service._template_compatible(tracking_template, "order")
    assert not service._template_compatible(order_id_template, "cart")
    cart_template = {**tracking_template, "category": "MARKETING", "components": [{"type": "BODY", "text": "Hi {{1}}, complete your order here: {{2}}. Our team is happy to help."}, compliant_template["components"][1]]}
    assert service._template_compatible(cart_template, "cart")
    assert service._template_parameter_count(cart_template) == 2
    assert not service._template_compatible({**cart_template, "status": "PENDING"}, "cart")
    assert not service._template_compatible({**cart_template, "components": [{"type": "BODY", "text": "Hi {{1}}, complete your order here: {{3}}. Our team is happy to help."}, compliant_template["components"][1]]}, "cart")
    assert service._template_compatible({**compliant_template, "category": "MARKETING"}, "cart")
    assert not service._template_compatible({**compliant_template, "components": compliant_template["components"][:1]}, "order")
    assert not service._template_compatible({
        **compliant_template,
        "components": [
            {"type": "BODY", "text": "{{1}} order {{2}} for {{3}}"},
            compliant_template["components"][1],
        ],
    }, "order")
    for event in ("abandoned_cart", "received", "shipped", "fulfilled", "cancelled"):
        payload = service._default_template_payload(event)
        assert payload["name"].endswith("_v4" if event == "shipped" else "_v3")
        assert payload["category"] == ("MARKETING" if event == "abandoned_cart" else "UTILITY")
        assert [component["type"] for component in payload["components"]] == ["BODY", "FOOTER"]
        assert payload["components"][1]["text"] == service.SHOPIFY_TEMPLATE_FOOTER
        assert not re.search(r"^\s*{{\s*\d+\s*}}|{{\s*\d+\s*}}\s*$", payload["components"][0]["text"])
        examples = payload["components"][0]["example"]["body_text"][0]
        assert len(examples) == (2 if event == "shipped" else 3) and all(examples)
        assert examples[2].startswith("https://") if event == "abandoned_cart" else examples[0].startswith("#") if event == "shipped" else examples[1].startswith("#")
    summary = service._product_summary([
        {"name": "Board", "quantity": 6},
        {"name": "Wax", "quantity": 1},
        {"name": "Helmet", "quantity": 2},
        {"name": "Boots", "quantity": 1},
    ])
    assert summary in {
        "Board ×6, Wax, Helmet ×2 +1 more",
        "Board x 6, Wax, Helmet x 2 +1 more",
    }, summary


async def test_legacy_repair_does_not_scan_historical_phones(service):
    service.utcnow = lambda: NOW
    activation = NOW - timedelta(minutes=5)
    connection = {"_id": "connection", "org_id": "org", "status": "connected", "automation_enabled": True,
                  "automation_enabled_at": activation, "reminder_delay_minutes": 15, "whatsapp_id": "waba", "whatsapp_phone_id": "phone", "sync_version": service.SYNC_VERSION}
    carts = FakeCollection([
        checkout("old", provider_created_at=NOW-timedelta(days=30), reminder_status="pending", claim_token="old-claim"),
        checkout("submitted", provider_created_at=NOW-timedelta(days=30), reminder_status="sending", provider_started_at=NOW-timedelta(minutes=1)),
        checkout("new", provider_created_at=NOW-timedelta(minutes=1)),
        checkout("other", org_id="other", provider_created_at=NOW-timedelta(days=30)),
    ])
    service.db = FakeDB(shopify_connections=FakeCollection([connection]), shopify_checkouts=carts)
    async def no_full_scan(*args, **kwargs):
        raise AssertionError("startup scanned every checkout phone")
    carts.distinct = no_full_scan
    await service.repair_legacy_shopify_connections()
    assert [row["reminder_status"] for row in carts.documents] == ["historical", "sending", "pending", "pending"]
    assert "claim_token" not in carts.documents[0]
    saved = service.db.shopify_connections.documents[0]
    assert saved["automation_enabled_at"] == activation and saved["reminder_delay_minutes"] == 15
    # Settings changes may reconcile only unsent carts created after the activation cutoff.
    async def eligible_phones(field, query):
        assert field == "phone" and query["provider_created_at"] == {"$gte": activation}
        assert query["provider_started_at"] == {"$exists": False}
        assert set(query["reminder_status"]["$in"]) == service.RECONCILABLE_REMINDERS
        return []
    carts.distinct = eligible_phones
    service._configuration_ready = lambda *args: _value(True)
    await service._repair_org_checkout_states("org", saved)


async def test_records_batch_followups(service):
    carts = [checkout(str(index)) for index in range(31)]
    jobs = FakeCollection([
        {"org_id": "org", "checkout_id": "9", "status": "pending", "due_at": NOW, "phone": "private"},
        {"org_id": "other", "checkout_id": "8", "status": "accepted"},
    ])
    service.db = FakeDB(shopify_checkouts=FakeCollection(carts), commerce_retargeting_jobs=jobs)
    async def chat(org_id, item, record_type, phone_id):
        return f"chat:{item['provider_id']}"
    service._record_conversation_id = chat
    calls = []
    find = jobs.find
    def batch(query, projection=None):
        calls.append(query)
        return find(query, projection)
    async def no_per_cart(*args, **kwargs):
        raise AssertionError("records loaded follow-ups one cart at a time")
    jobs.find, jobs.find_one = batch, no_per_cart
    page = await service.list_records("org", "checkout")
    assert len(page["items"]) == 30 and page["next_cursor"]
    assert len(calls) == 1 and calls[0]["org_id"] == "org"
    assert set(calls[0]["checkout_id"]["$in"]) == {item["provider_id"] for item in page["items"]}
    by_id = {item["provider_id"]: item for item in page["items"]}
    assert by_id["9"]["followup"]["status"] == "pending"
    assert by_id["9"]["followup"]["due_at"] == service._iso_utc(NOW)
    assert "phone" not in by_id["9"]["followup"] and "followup" not in by_id["8"]
    assert by_id["9"]["conversation_id"] == "chat:9"
    await service.list_records("empty", "checkout")
    await service.list_records("org", "order")
    assert len(calls) == 1


async def test_batch_recovery_permissions(service):
    marketing = FakeCollection([{"_id": "org:customer:whatsapp", "granted": True, "address_key": "14155550100"}])
    storefront = FakeCollection([{"_id": "org:cart", "granted": True, "phone_key": "14155550101"}])
    service.db = FakeDB(commerce_marketing_consents=marketing, shopify_storefront_consents=storefront)
    calls = []
    for collection in (marketing, storefront):
        original = collection.find
        def batch(query, projection=None, original=original):
            calls.append(query)
            return original(query, projection)
        collection.find = batch
        async def no_single(*args, **kwargs):
            raise AssertionError("permissions queried one cart at a time")
        collection.find_one = no_single
    rows = [checkout("one", customer_id="customer", whatsapp_consent=None),
            checkout("changed", customer_id="customer", phone="+14155550999", whatsapp_consent=None),
            checkout("other", org_id="other", customer_id="customer", whatsapp_consent=None),
            checkout("cart", customer_id="cart", phone="+14155550101", whatsapp_consent=None),
            {"whatsapp_consent": "SUBSCRIBED"}, {}]
    assert await service.recovery_permissions(rows) == [True, False, False, True, True, False]
    assert len(calls) == 2
    assert await service.recovery_permissions([]) == [] and len(calls) == 2
    assert await service.recovery_permission({"whatsapp_consent": "SUBSCRIBED"})
    assert not await service.recovery_permission({}) and len(calls) == 2


async def test_summary_date_scope(service):
    service.utcnow = lambda: NOW
    service.get_connection = lambda *_: _value({"connected": True})
    service.db = FakeDB(
        shopify_checkouts=FakeCollection([
            checkout("old", provider_updated_at=NOW-timedelta(days=30)),
            checkout("sent", provider_updated_at=NOW, reminder_status="sent"),
            checkout("failed", provider_updated_at=NOW, reminder_status="failed"),
            checkout("other", org_id="other", provider_updated_at=NOW),
        ]),
        shopify_orders=FakeCollection([
            {"org_id": "org", "provider_updated_at": NOW},
            {"org_id": "org", "provider_updated_at": NOW-timedelta(days=30)},
        ]),
    )
    result = await service.commerce_summary("org", start=NOW-timedelta(hours=1), end=NOW+timedelta(hours=1))
    assert result["counts"]["carts"] == 2 and result["counts"]["orders"] == 1
    assert result["counts"]["sent"] == 1 and result["counts"]["failed"] == 1
    assert result["counts"]["pending"] == result["counts"]["overdue"] == 0


async def main():
    test_delays_and_product_summary(load_service())
    await test_legacy_repair_does_not_scan_historical_phones(load_service())
    await test_records_batch_followups(load_service())
    await test_batch_recovery_permissions(load_service())
    await test_summary_date_scope(load_service())
    await test_hanging_checkout_does_not_block_next(load_service())
    await test_concurrent_workers_claim_once(load_service())
    await test_database_outage_defers_worker_ticks(load_service())
    await test_expired_claim_recovery(load_service())
    await test_checkout_update_and_order_races(load_service())
    await test_refresh_shape_change_does_not_restart_elapsed_timer(load_service())
    await test_newest_checkout_per_phone(load_service())
    await test_stop_suppression(load_service())
    assert load_service()._provider_reports_block({"error": {"message": "Recipient blocked your business"}})
    await test_websocket_hang_is_bounded(load_service())
    await test_provider_fence_timeout_is_unknown_and_not_retried(load_service())
    await test_acceptance_precedes_inbox_and_reconciles_once(load_service())
    await test_acceptance_precedes_inbox_and_reconciles_once(load_service(), parameter_count=2)
    await test_order_event_sends_approved_template_once(load_service())
    await test_order_event_sends_approved_template_once(load_service(), parameter_count=1)
    await test_order_event_sends_approved_template_once(load_service(), parameter_count=2, event="shipped")
    await test_shipped_waits_for_tracking_update(load_service())
    await test_immediate_meta_rejection_is_visible_and_retried(load_service())
    await test_async_meta_failure_keeps_terminal_reason(load_service())
    await test_late_failure_does_not_regress_delivered_notification(load_service())
    print("SHOPIFY_WORKER_RUNTIME_OK")


if __name__ == "__main__":
    asyncio.run(main())
