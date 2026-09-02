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
    instances = 0
    last_post = None

    def __init__(self, **_kwargs):
        type(self).instances += 1
        self.is_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        type(self).last_post = (_args, _kwargs)
        return _Response()

    async def aclose(self):
        self.is_closed = True


def _load_service(records, failures, active_case=None, sales_state_failure=False):
    _Client.instances = 0
    _Client.last_post = None
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
    sales_cases = types.ModuleType("services.sales_case_service")
    sales_learning = types.ModuleType("services.sales_learning_service")
    sales_calls = []

    async def create_ai_escalation(org_id, data):
        if failures:
            raise RuntimeError("database private detail")
        records.append((org_id, data))
        return data

    team.create_ai_escalation = create_ai_escalation

    async def get_active_sales_case(*_args, **_kwargs):
        sales_calls.append(("get_active", None))
        if sales_state_failure:
            raise RuntimeError("database unavailable")
        return active_case

    async def get_sales_context(*_args, **_kwargs):
        return {"version": 0, "stage": "new", "facts": {}}

    async def apply_sales_decision(*_args, **_kwargs):
        raise AssertionError("unexpected sales decision")

    async def get_sales_guidance(*_args, **_kwargs):
        sales_calls.append(("guidance", None))
        return {"policy_version": "default", "cohort": "control", "recommended_moves": []}

    async def record_customer_response(*_args, **_kwargs):
        sales_calls.append(("record", _kwargs.get("case")))
        return None

    sales_cases.get_active_sales_case = get_active_sales_case
    sales_cases.get_sales_context = get_sales_context
    sales_cases.apply_sales_decision = apply_sales_decision
    sales_cases.sales_context = lambda case: {
        "version": int((case or {}).get("version") or 0),
        "stage": (case or {}).get("stage") or "new",
        "facts": (case or {}).get("facts") or {},
    }
    sales_learning.get_sales_guidance = get_sales_guidance
    sales_learning.record_customer_response = record_customer_response
    sys.modules.update(
        {
            "httpx": httpx,
            "loguru": loguru,
            "config": config,
            "config.settings": settings,
            "services.team_service": team,
            "services.sales_case_service": sales_cases,
            "services.sales_learning_service": sales_learning,
        }
    )
    spec = importlib.util.spec_from_file_location("math_handoff_agent_service", ROOT / "services/agent_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.sales_calls = sales_calls
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
    assert _Client.instances == 1
    assert len(records) == 2 and records[0][1]["id"] == records[1][1]["id"]
    saved = records[0][1]
    assert saved["dedupe_id"] == saved["id"]
    assert saved["metadata"]["source_message_id"] == "wamid.123"
    assert saved["metadata"]["reason"] == "quotation"
    assert saved["customer_text"] == "private customer text"

    _Response.payload = {
        "response": "A safe customer reply",
        "handoff": {
            "requested": True,
            "reason_code": "discount",
            "context": "  Was looking at: Asus Zephyrus G14 (Rs.67,381).  ",
        },
    }
    assert await _call(module) == {"final_response": "A safe customer reply", "usage": {}}
    saved_context = records[-1][1]
    assert saved_context["customer_text"] == "private customer text"
    assert saved_context["metadata"]["catalogue_context"] == (
        "Was looking at: Asus Zephyrus G14 (Rs.67,381)."
    )

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

    for context in (123, "x" * 501):
        _Response.payload = {
            "response": "A safe customer reply",
            "handoff": {"requested": True, "reason_code": "discount", "context": context},
        }
        try:
            await _call(module)
        except AIProviderUnavailable as exc:
            assert exc.category == "malformed_handoff"
        else:
            raise AssertionError("invalid handoff context was accepted")

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

    active_case = {"version": 4, "stage": "negotiating", "facts": {"budget": 60000}}
    optimized = _load_service([], [], active_case=active_case)
    _Response.payload = {"response": "A safe customer reply"}
    assert await _call(optimized) == {"final_response": "A safe customer reply", "usage": {}}
    assert optimized.sales_calls.count(("get_active", None)) == 1
    assert ("record", active_case) in optimized.sales_calls

    degraded = _load_service([], [], sales_state_failure=True)
    assert await _call(degraded) == {"final_response": "A safe customer reply", "usage": {}}
    assert b'"sales_context":{}' in _Client.last_post[1]["content"]
    source = (ROOT / "services/agent_service.py").read_text(encoding="utf-8")
    assert "asyncio.gather(" in source
    assert "asyncio.wait_for" not in source


def main():
    asyncio.run(_exercise())
    team_source = (ROOT / "services/team_service.py").read_text(encoding="utf-8")
    pma_source = (ROOT / "services/pma_chatbot.py").read_text(encoding="utf-8")
    webhook_source = (ROOT / "api/webhook_routes.py").read_text(encoding="utf-8")
    assert "asyncio.wait_for" in team_source and "timeout=1" in team_source
    assert '{"_id": dedupe_id}' in team_source
    assert "_validated_handoff" in pma_source and "_persist_handoff" in pma_source
    assert pma_source.index("await _persist_handoff(") < pma_source.index("final_response = validate_ai_reply")
    assert "asyncio.create_task(\n                                            _best_effort_typing" in webhook_source
    print("Math handoff persistence contract: ok")


if __name__ == "__main__":
    main()
