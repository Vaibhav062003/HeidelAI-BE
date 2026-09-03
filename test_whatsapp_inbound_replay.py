import asyncio
import os

from pymongo.errors import DuplicateKeyError


for key in (
    "LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT", "AZURE_OPENAI_SUBSCRIPTION_KEY",
    "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT",
):
    os.environ.setdefault(key, "test")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_CLUSTER", "test")

from schemas.models import MessageRole
from services import wa_service


def test_only_meta_4xx_is_a_definitive_rejection():
    assert wa_service._is_definitive_meta_rejection(400)
    assert wa_service._is_definitive_meta_rejection(429)
    assert not wa_service._is_definitive_meta_rejection(408)
    assert not wa_service._is_definitive_meta_rejection(500)
    assert not wa_service._is_definitive_meta_rejection(302)


class _Messages:
    def __init__(self, existing):
        self.existing = existing

    def insert_one(self, _message):
        raise DuplicateKeyError("duplicate message_id")

    def find_one(self, query, *_args, **_kwargs):
        return self.existing if query.get("message_id") == self.existing["message_id"] else None


class _Conversations:
    def __init__(self, existing):
        self.existing = existing
        self.updates = 0

    def find_one(self, query, *_args, **_kwargs):
        return self.existing if query.get("conversation_id") == self.existing["conversation_id"] else None

    def find_one_and_update(self, *_args, **_kwargs):
        self.updates += 1
        return self.existing


class _DB:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


def test_identical_inbound_message_replay_has_no_side_effects(monkeypatch):
    existing = {
        "id": "wamid.1", "message_id": "wamid.1", "platform": "whatsapp",
        "conversation_id": "whatsapp_919999999999", "content": "hello",
        "type": "text", "sender_id": "919999999999",
        "role": MessageRole.CUSTOMER, "mode": "reply",
    }
    monkeypatch.setattr(wa_service, "db", _DB(_Messages(existing)))

    pushes = []

    async def push(*_args, **_kwargs):
        pushes.append(_kwargs)

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("a replay emitted a notification")

    monkeypatch.setattr(wa_service, "schedule_customer_message_push", push)
    monkeypatch.setattr(wa_service, "broadcast_main_ws", unexpected)
    monkeypatch.setattr(wa_service, "broadcast_whatsapp_message", unexpected)
    stored_id = asyncio.run(wa_service.store_whatsapp_message(
        org_id="org", content="hello", conversation_id="whatsapp_919999999999",
        customer_phone_no="919999999999", type="text", payload=None,
        recipient_id="waba", role=MessageRole.CUSTOMER, mode="reply",
        message_id="wamid.1", notify_push=True,
    ))
    assert stored_id == "wamid.1"
    assert len(pushes) == 1 and pushes[0]["message_id"] == "wamid.1"


def test_message_id_reuse_with_different_content_fails(monkeypatch):
    existing = {
        "id": "wamid.1", "message_id": "wamid.1", "platform": "whatsapp",
        "conversation_id": "whatsapp_919999999999", "content": "original",
        "type": "text", "sender_id": "919999999999",
        "role": MessageRole.CUSTOMER, "mode": "reply",
    }
    monkeypatch.setattr(wa_service, "db", _DB(_Messages(existing)))
    try:
        asyncio.run(wa_service.store_whatsapp_message(
            org_id="org", content="changed", conversation_id="whatsapp_919999999999",
            customer_phone_no="919999999999", type="text", payload=None,
            recipient_id="waba", role=MessageRole.CUSTOMER, mode="reply",
            message_id="wamid.1",
        ))
    except DuplicateKeyError:
        return
    raise AssertionError("a mismatched message replay was accepted")


def test_conversation_replay_does_not_increment_or_broadcast(monkeypatch):
    conversations = _Conversations({
        "conversation_id": "whatsapp_919999999999",
        "applied_outbound_message_ids": ["wamid.1"],
    })
    monkeypatch.setattr(wa_service, "db", _DB(conversations))
    conversation_id = asyncio.run(wa_service.create_or_update_whatsapp_conversation(
        org_id="org", recipient_id="waba", customer_phone_no="919999999999",
        customer_name="Customer", last_message="hello", last_sender="customer",
        idempotency_key="wamid.1",
    ))
    assert conversation_id == "whatsapp_919999999999"
    assert conversations.updates == 0
