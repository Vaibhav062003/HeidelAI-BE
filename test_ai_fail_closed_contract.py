import ast
import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.ai_reply_guard import AIProviderUnavailable, validate_ai_reply


ROOT = Path(__file__).parent


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _RequestError(Exception):
    pass


class _TimeoutException(_RequestError):
    pass


class _HTTPStatusError(Exception):
    def __init__(self, response):
        self.response = response


class _Response:
    def __init__(self, status=200, payload=None, malformed=False):
        self.status_code = status
        self._payload = payload
        self._malformed = malformed

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPStatusError(self)

    def json(self):
        if self._malformed:
            raise ValueError("bad json")
        return self._payload


class _Client:
    response = _Response(payload={"response": "Valid reply"})
    error = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        if self.error:
            raise self.error
        return self.response


class _Collection:
    def __init__(self, document):
        self.document = document

    async def find_one(self, *_args, **_kwargs):
        if isinstance(self.document, Exception):
            raise self.document
        return self.document


class _DB:
    def __init__(self, organization, conversation):
        self.organizations = _Collection(organization)
        self.conversation = _Collection(conversation)

    def __getitem__(self, _name):
        return self.conversation


def _load_agent_service():
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = _Client
    httpx.RequestError = _RequestError
    httpx.TimeoutException = _TimeoutException
    httpx.HTTPStatusError = _HTTPStatusError
    sys.modules["httpx"] = httpx
    loguru = types.ModuleType("loguru")
    loguru.logger = _Logger()
    settings = types.ModuleType("config.settings")
    settings.X_INTERNAL_SECRET = "test-secret"
    config = types.ModuleType("config")
    config.settings = settings
    sys.modules["loguru"] = loguru
    sys.modules["config"] = config
    sys.modules["config.settings"] = settings
    spec = importlib.util.spec_from_file_location("agent_service_contract", ROOT / "services/agent_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.httpx.AsyncClient = _Client
    return module


def _load_ai_policy_service():
    fastapi = types.ModuleType("fastapi")
    fastapi.HTTPException = type("HTTPException", (Exception,), {})
    pymongo = types.ModuleType("pymongo")
    pymongo.ReturnDocument = types.SimpleNamespace(AFTER=1)
    database = types.ModuleType("database")
    database.get_async_mongo_db = lambda: _DB(None, None)
    utils = types.ModuleType("services.utils")
    utils.serialize_mongo = lambda value: value
    websocket = types.ModuleType("services.websocket_service")

    async def _broadcast(*_args, **_kwargs):
        return None

    websocket.broadcast_org_ws = _broadcast
    sys.modules["fastapi"] = fastapi
    sys.modules["pymongo"] = pymongo
    sys.modules["database"] = database
    sys.modules["services.utils"] = utils
    sys.modules["services.websocket_service"] = websocket
    spec = importlib.util.spec_from_file_location("ai_policy_contract", ROOT / "services/ai_policy_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.logger = _Logger()
    return module


async def _call(module):
    return await module.send_to_agent("whatsapp_123456", "hello", "sales", "india", "Customer", "org_test")


def _failure_category(module, *, response=None, error=None):
    _Client.response = response or _Response(payload={"response": "Valid reply"})
    _Client.error = error
    try:
        asyncio.run(_call(module))
    except AIProviderUnavailable as exc:
        return exc.category
    raise AssertionError("AI failure was returned as a customer reply")


def test_provider_contract():
    module = _load_agent_service()
    assert asyncio.run(_call(module)) == {"final_response": "Valid reply"}
    assert _failure_category(module, response=_Response(status=500, payload={})) == "http_error"
    assert _failure_category(module, error=_TimeoutException("timeout")) == "timeout"
    assert _failure_category(module, response=_Response(malformed=True)) == "malformed_response"
    assert _failure_category(module, response=_Response(payload={})) == "empty_response"
    assert _failure_category(
        module,
        response=_Response(payload={"response": "I apologize, but I'm experiencing technical difficulties. Please try again later."}),
    ) == "legacy_failure_text"


def test_all_clients_use_shared_guard():
    for path in ("services/agent_service.py", "services/pma_chatbot.py", "services/heidelai_bot.py"):
        source = (ROOT / path).read_text()
        assert "validate_ai_reply" in source
        assert "AIProviderUnavailable" in source
        assert "technical difficulties" not in source


def test_effective_ai_state_fails_closed():
    module = _load_ai_policy_service()
    now = datetime.now(timezone.utc)
    cases = (
        (None, {"is_ai_enabled": True}, False),
        ({"is_ai_enabled": False}, {"is_ai_enabled": True}, False),
        ({"is_ai_enabled": True}, {"is_ai_enabled": False}, False),
        ({"is_ai_enabled": True}, {"is_ai_enabled": True}, True),
        ({"is_ai_enabled": True}, {
            "is_ai_enabled": True,
            "auto_ai_disabled": True,
            "auto_ai_disabled_until": now + timedelta(minutes=5),
        }, False),
        ({"is_ai_enabled": True}, {
            "is_ai_enabled": True,
            "auto_ai_disabled": True,
            "auto_ai_disabled_until": now - timedelta(minutes=1),
        }, True),
        ({"is_ai_enabled": True}, RuntimeError("database unavailable"), False),
    )
    for organization, conversation, expected in cases:
        module.db = _DB(organization, conversation)
        assert asyncio.run(module.is_effective_ai_enabled("org", "whatsapp_123456")) is expected


def test_channel_and_scheduler_contract():
    webhook = (ROOT / "api/webhook_routes.py").read_text()
    scheduler = (ROOT / "core/scheduler.py").read_text()
    sender = (ROOT / "services/wa_service.py").read_text()
    instagram = (ROOT / "services/ig_service.py").read_text()
    reply_claim = (ROOT / "services/whatsapp_reply_idempotency.py").read_text()
    assert webhook.count("except AIProviderUnavailable") >= 5
    assert webhook.count("is_effective_ai_enabled") >= 10
    assert '"reason": "ai_provider_unavailable"' in webhook
    assert "HOLDING_REPLY" not in webhook
    assert "HOLDING_REPLY" not in sender
    assert 'user_input="hi"' not in scheduler
    assert "ai_escalation_dedup" in webhook
    assert '"$max": {"expires_at"' in webhook
    assert "enforce_ai_state=True" in webhook
    assert "ai_processing_complete" in webhook
    assert "release_instagram_webhook_downstream" in webhook
    assert "ai_failure_pending" in webhook
    assert "ai_failure_category" in webhook
    assert "state_already_checked=True" in webhook
    assert "resume_pending=True" in webhook
    assert "AI failure retry state could not be persisted" in webhook
    assert "Instagram AI downstream claim was replaced" in webhook
    assert 'mode not in {"reply", "ai"}' in reply_claim
    assert "is_effective_ai_enabled" in sender
    assert "len(joined_content) > 4096" in sender
    assert "not joined_content.strip()" in sender
    pending_resume = webhook.index("pending_ai_failure = messages_collection.find_one")
    text_dispatch = webhook.index('if message_type == "text":', pending_resume)
    assert pending_resume < text_dispatch
    assert "is_effective_ai_enabled" in instagram

    tree = ast.parse(scheduler)
    resume = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_ai_status"
    )
    names = {node.id for node in ast.walk(resume) if isinstance(node, ast.Name)}
    assert "send_to_agent" not in names
    assert "send_whatsapp_message" not in names


def test_reply_validator():
    assert validate_ai_reply("  Hello  ") == "Hello"
    for value in (None, "", "  "):
        try:
            validate_ai_reply(value)
        except AIProviderUnavailable as exc:
            assert exc.category == "empty_response"
        else:
            raise AssertionError("empty reply accepted")
    try:
        validate_ai_reply(123)
    except AIProviderUnavailable as exc:
        assert exc.category == "malformed_response"
    else:
        raise AssertionError("malformed reply accepted")
    try:
        validate_ai_reply(
            "I apologize, but I'm experiencing technical difficulties. "
            "Please try again later. Powered by HeidelAI"
        )
    except AIProviderUnavailable as exc:
        assert exc.category == "legacy_failure_text"
    else:
        raise AssertionError("embedded legacy failure reply accepted")


if __name__ == "__main__":
    test_provider_contract()
    test_all_clients_use_shared_guard()
    test_effective_ai_state_fails_closed()
    test_channel_and_scheduler_contract()
    test_reply_validator()
    print("AI fail-closed contract: ok")
