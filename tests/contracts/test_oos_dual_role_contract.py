import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from services import oos_service


class _Cursor:
    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def to_list(self, *, length):
        return []


class _OOSCollection:
    def __init__(self, request=None):
        self.request = request
        self.updates = []

    async def find_one(self, query):
        if "sent_dealers.message_ids" in query:
            return self.request
        return None

    def find(self, _query):
        return _Cursor()

    async def update_one(self, query, update):
        self.updates.append((query, update))
        return SimpleNamespace(modified_count=1)


class _DB:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "oos_requests"
        return self.collection


def _request():
    return {
        "org_id": "org_test",
        "query_id": "query-1",
        "status": "dealers_contacted",
        "sent_dealers": [{
            "dealer_phone": "919999999999",
            "dealer_name": "Test Dealer",
            "message_ids": ["wamid.request"],
        }],
    }


def test_product_question_quoting_oos_request_is_not_stored_as_dealer_reply():
    collection = _OOSCollection(_request())
    with patch.object(oos_service, "get_async_mongo_db", lambda: _DB(collection)):
        handled = asyncio.run(oos_service.handle_dealer_reply(
            "org_test",
            "919999999999",
            "what all products you got in stock?",
            {"id": "wamid.request"},
            source_message_id="wamid.customer",
        ))

    assert handled is False
    assert not collection.updates


def test_product_question_without_reply_context_does_not_use_phone_fallback():
    collection = _OOSCollection(_request())
    with patch.object(oos_service, "get_async_mongo_db", lambda: _DB(collection)):
        handled = asyncio.run(oos_service.handle_dealer_reply(
            "org_test",
            "919999999999",
            "5060 laptop",
            source_message_id="wamid.customer",
        ))

    assert handled is False
    assert not collection.updates


def test_supplier_answer_still_uses_existing_oos_pipeline():
    collection = _OOSCollection(_request())
    completed = []

    async def complete(org_id, request, reply):
        completed.append((org_id, request["query_id"], reply["content"]))

    with (
        patch.object(oos_service, "get_async_mongo_db", lambda: _DB(collection)),
        patch.object(oos_service, "_complete_dealer_reply_steps", complete),
    ):
        handled = asyncio.run(oos_service.handle_dealer_reply(
            "org_test",
            "919999999999",
            "YES AVAILABLE 15000 RS",
            {"id": "wamid.request"},
            source_message_id="wamid.customer",
        ))

    assert handled is True
    assert len(collection.updates) == 1
    assert completed == [("org_test", "query-1", "YES AVAILABLE 15000 RS")]
