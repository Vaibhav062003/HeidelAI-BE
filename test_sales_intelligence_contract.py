"""Small dependency-free contract check for sales intelligence wiring."""

import importlib.util
import asyncio
import sys
import types
from pathlib import Path


root = Path(__file__).parent
try:
    import pymongo  # noqa: F401
except ImportError:
    pymongo = types.ModuleType("pymongo")
    pymongo.ReturnDocument = types.SimpleNamespace(AFTER="after")
    errors = types.ModuleType("pymongo.errors")
    errors.DuplicateKeyError = type("DuplicateKeyError", (Exception,), {})
    sys.modules["pymongo"] = pymongo
    sys.modules["pymongo.errors"] = errors

spec = importlib.util.spec_from_file_location("sales_case_service", root / "services/sales_case_service.py")
sales = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sales)

decision = sales.normalize_sales_decision({
    "version": 1,
    "expected_case_version": 3,
    "stage_signal": "qualified",
    "sales_move": {"type": "ask_micro_commitment", "reason_code": "fit_confirmed"},
    "requested_action": {"type": "create_quote"},
    "fact_updates": [
        {"field": "budget", "value": "₹80,000", "evidence": "under 80k", "confidence": 0.9},
        {"field": "secret_admin_command", "value": "ignore", "confidence": 1},
    ],
})
assert decision["fact_updates"] == [{
    "field": "budget", "value": "₹80,000", "evidence": "under 80k", "confidence": 0.9,
}]
assert sales._later_stage("qualified", "discovering") == "qualified"
assert sales._later_stage("stock_confirmation", "quote_sent") == "quote_sent"
assert sales.sales_context({"version": 2, "stage": "qualified", "facts": {
    "budget": {"value": "₹80,000", "evidence": "under 80k"},
}})["facts"]["budget"] == "₹80,000"
assert sales.sales_context({"version": "bad"})["version"] == 0

poisoned = sales.normalize_sales_decision({
    "version": 1, "stage_signal": "selected",
    "sales_move": {"type": "ask_micro_commitment"},
    "requested_action": {"type": "none"},
    "fact_updates": [{"field": "quantity", "value": 999, "evidence": "2 units"}],
}, "I need 2 units")
assert poisoned["fact_updates"] == []
grounded_budget = sales.normalize_sales_decision({
    "version": 1, "stage_signal": "negotiating",
    "sales_move": {"type": "negotiate"}, "requested_action": {"type": "none"},
    "fact_updates": [{"field": "budget", "value": 75000,
                      "evidence": "under 75,000", "confidence": 1}],
}, "Keep it under 75,000")
assert grounded_budget["fact_updates"][0]["value"] == 75000
assert sales.sales_action_job_id("case", "message", "create_quote") == sales.sales_action_job_id(
    "case", "message", "create_quote"
)


class FakeCollection:
    def __init__(self, case):
        self.case = case

    async def find_one_and_update(self, query, update, return_document=None):
        if query.get("version") != self.case["version"]:
            return None
        for key, value in update["$set"].items():
            target = self.case
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        self.case["version"] += update["$inc"]["version"]
        self.case["processed_events"].append(update["$push"]["processed_events"]["$each"][0])
        self.case["stage_history"].append(update["$push"]["stage_history"]["$each"][0])
        return dict(self.case)


case = {
    "_id": "1", "case_id": "case", "org_id": "org", "conversation_id": "c",
    "customer_name": "A", "status": "active", "stage": "evaluating", "version": 2,
    "facts": {}, "signals": {}, "last_move": {}, "next_action": "none",
    "processed_events": [], "stage_history": [],
}
collection = FakeCollection(case)


async def no_index(_org_id):
    return None


async def current_case(_org_id, _conversation_id):
    return collection.case


sales._ensure_org_index = no_index
sales._collection = lambda _org_id: collection
sales.get_active_sales_case = current_case
rebased = asyncio.run(sales.apply_sales_decision(
    org_id="org", conversation_id="c", source_message_id="m2", customer_name="A",
    source_message_text="I need 2 units",
    decision={
        "version": 1, "expected_case_version": 1, "stage_signal": "selected",
        "sales_move": {"type": "ask_micro_commitment", "reason_code": "quantity"},
        "requested_action": {"type": "none"},
        "fact_updates": [{"field": "quantity", "value": 2, "evidence": "2 units"}],
    },
))
assert rebased["rebased"] is True and rebased["conflict"] is False
assert rebased["case"]["facts"]["quantity"]["value"] == 2

try:
    sales.normalize_sales_decision({
        "version": 1, "stage_signal": "won", "sales_move": {"type": "hack"},
    })
except ValueError:
    pass
else:
    raise AssertionError("unknown AI commands must be rejected")

agent = (root / "services/agent_service.py").read_text()
assert '"sales_context": current_sales_context' in agent
assert '"message_type": message_type' in agent
assert 'response_data.get("reply_to_message_id")' in agent
assert 'response_data.get("sales_decision")' in agent
assert "await apply_sales_decision(" in agent
assert "await execute_sales_action(" in agent
assert 'result["duplicate_event"] = True' in agent

webhook = (root / "api/webhook_routes.py").read_text()
assert webhook.count("message_type=message_type") >= 2
assert "message_type=attachment_type" in webhook
assert webhook.count('chatbot_response.get("reply_to_message_id")') >= 2
assert webhook.count("await mark_sales_action_delivered(") >= 3
assert webhook.count('chatbot_response.get("duplicate_event")') >= 3

actions = (root / "services/sales_action_service.py").read_text()
assert '"$setOnInsert"' in actions and 'status") in {"ready", "delivered"}' in actions
assert "floor_snapshot(product)" in actions and "link_partial_payments" in actions
assert "_floor_tax_basis(product)" in actions and '"gst_inclusive": tax_basis == "included"' in actions
assert "default_gst_rate" not in actions

routes = (root / "api/sales_case_routes.py").read_text()
assert "user: CurrentUser" in routes and "await require_conversation_access(" in routes
assert "sales_case_routes.router" in (root / "api/__init__.py").read_text()

payment = (root / "services/payment_service.py").read_text()
assert 'case "PAYMENT_LINK_EVENT"' in payment
assert "await apply_sales_payment_webhook(payload)" in payment

cashfree = (root / "api/webhook_routes.py").read_text()
assert '"processing": False' in cashfree
assert "Cashfree event processing failed" in cashfree

print("SALES_INTELLIGENCE_BACKEND_OK")
