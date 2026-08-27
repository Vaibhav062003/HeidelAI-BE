"""Small dependency-free contract for the seller command centre."""

import importlib.util
import sys
import types
from pathlib import Path


root = Path(__file__).parent
fastapi = types.ModuleType("fastapi")
fastapi.HTTPException = type("HTTPException", (Exception,), {"__init__": lambda self, status_code=500, detail="": Exception.__init__(self, detail)})
sys.modules["fastapi"] = fastapi
database = types.ModuleType("database")
database.get_async_mongo_db = lambda: None
sys.modules["database"] = database
utils = types.ModuleType("services.utils")
utils.serialize_mongo = lambda value: value
sys.modules["services.utils"] = utils

spec = importlib.util.spec_from_file_location(
    "seller_intelligence_service", root / "services/seller_intelligence_service.py",
)
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)

low_score, low_factors, low_reasons = service.score_action({
    "stage": "payment_pending", "ai_confidence": 0.64, "amount": 100_000,
})
assert low_factors["buying_intent"] == 0
assert all(reason["source"] != "sales_case" for reason in low_reasons)

high_score, high_factors, _ = service.score_action({
    "stage": "payment_pending", "ai_confidence": 0.65, "amount": 500_000,
    "priority": "high", "needs_reply": True, "unread_count": 2,
    "followup_due_minutes": -10, "reply_window_minutes": 60,
    "operational_issue": "Payment is overdue",
})
assert high_score == 97
assert high_factors == {
    "urgency": 30, "revenue_exposure": 25, "buying_intent": 20,
    "waiting_risk": 12, "operational_risk": 10,
}

service_source = (root / "services/seller_intelligence_service.py").read_text()
assert '"unavailable" if not tally_integration' in service_source
assert '"receivables":' in service_source and "if tally_integration and not invoice_error else None" in service_source
assert '"SELLER_INTELLIGENCE"' in (root / "services/entitlement_service.py").read_text()

routes = (root / "api/seller_intelligence_routes.py").read_text()
for contract in ('@router.get("")', '@router.get("/items/{item_id:path}")', '@router.patch("/items/{item_id:path}")', '@router.patch("/preferences")'):
    assert contract in routes
assert "seller_intelligence_routes.router" in (root / "api/__init__.py").read_text()

print("SELLER_INTELLIGENCE_BACKEND_OK")
