import asyncio
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))

from services.ai_reply_guard import AIProviderUnavailable


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _Response:
    payload = {"response": "A safe customer reply"}
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        return _Response()


def _load_service(records, failures):
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = _Client
    httpx.RequestError = type("RequestError", (Exception,), {})
    httpx.TimeoutException = type("TimeoutException", (httpx.RequestError,), {})
    httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    loguru = types.ModuleType("loguru")
    loguru.logger = _Logger()
    settings = types.ModuleType("config.settings")
    settings.INTERNAL_HMAC_SECRET = "s" * 32
    config = types.ModuleType("config")
    config.settings = settings
    team = types.ModuleType("services.team_service")

    async def create_ai_escalation(org_id, data):
        if failures:
            raise RuntimeError("database private detail")
        records.append((org_id, data))
        return data

    team.create_ai_escalation = create_ai_escalation
    sys.modules.update(
        {
            "httpx": httpx,
            "loguru": loguru,
            "config": config,
            "config.settings": settings,
            "services.team_service": team,
        }
    )
    spec = importlib.util.spec_from_file_location("math_handoff_agent_service", ROOT / "services/agent_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _call(module, *, message_id="wamid.123"):
    return await module.send_to_agent(
        conversation_id="whatsapp_919999999999",
        user_input="private customer text",
        persona="sales",
        location="india",
        customer_name="Customer",
        org_id="org_test",
        source="webhook",
        message_id=message_id,
    )


async def _exercise():
    records = []
    failures = []
    module = _load_service(records, failures)
    _Response.payload = {
        "response": "A safe customer reply",
        "handoff": {"requested": True, "reason_code": "quotation"},
    }
    assert await _call(module) == {"final_response": "A safe customer reply", "usage": {}}
    assert await _call(module) == {"final_response": "A safe customer reply", "usage": {}}
    assert len(records) == 2 and records[0][1]["id"] == records[1][1]["id"]
    saved = records[0][1]
    assert saved["dedupe_id"] == saved["id"]
    assert saved["metadata"]["source_message_id"] == "wamid.123"
    assert saved["metadata"]["reason"] == "quotation"
    assert saved["customer_text"] == "private customer text"

    before = len(records)
    _Response.payload = {
        "response": "A safe customer reply",
        "handoff": {"requested": True, "reason_code": "quotation", "customer_text": "leak"},
    }
    try:
        await _call(module)
    except AIProviderUnavailable as exc:
        assert exc.category == "malformed_handoff"
    else:
        raise AssertionError("malformed handoff was accepted")
    assert len(records) == before

    _Response.payload = {
        "response": "A safe customer reply",
        "handoff": {"requested": True, "reason_code": "customer_vinod"},
    }
    try:
        await _call(module)
    except AIProviderUnavailable as exc:
        assert exc.category == "malformed_handoff"
    else:
        raise AssertionError("unapproved handoff reason was accepted")

    _Response.payload = {"response": "", "handoff": {"requested": True, "reason_code": "human"}}
    try:
        await _call(module)
    except AIProviderUnavailable as exc:
        assert exc.category == "empty_response" and exc.escalation_required is False
    else:
        raise AssertionError("invalid reply was returned after a handoff")

    failures.append(True)
    _Response.payload = {
        "response": "A safe customer reply",
        "handoff": {"requested": True, "reason_code": "human"},
    }
    try:
        await _call(module)
    except AIProviderUnavailable as exc:
        assert exc.category == "handoff_persistence_failed"
    else:
        raise AssertionError("reply escaped after handoff persistence failed")


def main():
    asyncio.run(_exercise())
    team_source = (ROOT / "services/team_service.py").read_text(encoding="utf-8")
    pma_source = (ROOT / "services/pma_chatbot.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for" in team_source and "timeout=1" in team_source
    assert '{"_id": dedupe_id}' in team_source
    assert "_validated_handoff" in pma_source and "_persist_handoff" in pma_source
    assert pma_source.index("await _persist_handoff(") < pma_source.index("final_response = validate_ai_reply")
    print("Math handoff persistence contract: ok")


if __name__ == "__main__":
    main()
