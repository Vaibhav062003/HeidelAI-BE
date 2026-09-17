import importlib.util
import os

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("ENV", "test")
os.environ.setdefault("MONGODB_URI", "mongodb://127.0.0.1:27017")
os.environ.setdefault("MONGODB_CLUSTER", "native_invoice_http_test")

from auth.dependencies import get_current_user
from auth.models import CurrentUserModel
from services.native_invoice_master_service import _pincode


def test_customer_preset_pincode_is_normalized_or_omitted() -> None:
    assert _pincode("400 001") == "400001"
    assert _pincode("unknown") == ""


def _routes_module():
    spec = importlib.util.spec_from_file_location("native_invoice_routes_http_test", "api/native_invoice_routes.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_native_invoice_http_auth_tenant_scope_and_admin_gate(monkeypatch) -> None:
    routes = _routes_module()
    app = FastAPI()
    app.include_router(routes.router)

    unauthenticated = TestClient(app).get("/api/native-invoices/presets/customers")
    assert unauthenticated.status_code in {401, 403}

    user = CurrentUserModel(user_id="billing-user", org_id="org-a", role="member", full_name="Billing")
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(routes, "native_invoicing_enabled", lambda org_id: org_id == "org-a")
    monkeypatch.setattr(routes, "feature_access", lambda *_args: {"state": "active"})

    async def teams(org_id):
        assert org_id == "org-a"
        return [{"type": "billing", "member_ids": ["billing-user"]}]

    async def customers(org_id, search, limit):
        assert (org_id, search, limit) == ("org-a", "Acme", 5)
        return [{"name": "Acme"}]

    monkeypatch.setattr(routes, "list_work_teams", teams)
    monkeypatch.setattr(routes, "customer_presets", customers)
    response = TestClient(app).get("/api/native-invoices/presets/customers?search=Acme&limit=5")
    assert response.status_code == 200
    assert response.json() == {"customers": [{"name": "Acme"}]}

    close = TestClient(app).post(
        "/api/native-invoices/invoice-1/close",
        headers={"X-Idempotency-Key": "close-invoice-1"},
        json={"expected_revision": 1, "reason": "period close"},
    )
    assert close.status_code == 403
    assert close.json()["detail"] == "This action requires an organization administrator"


def test_static_native_invoice_routes_precede_dynamic_invoice_route() -> None:
    routes = _routes_module()
    paths = [(method, route.path) for route in routes.router.routes for method in sorted(route.methods or [])]
    expected = {
        ("POST", "/api/native-invoices/preview"),
        ("GET", "/api/native-invoices/export.json"),
        ("GET", "/api/native-invoices/export.xlsx"),
        ("GET", "/api/native-invoices/presets/customers"),
        ("GET", "/api/native-invoices/presets/items"),
        ("POST", "/api/native-invoices/{invoice_id}/receipt/render"),
        ("GET", "/api/native-invoices/{invoice_id}/receipt-link"),
    }
    assert expected <= set(paths)
    assert paths.index(("GET", "/api/native-invoices/presets/customers")) < paths.index(("GET", "/api/native-invoices/{invoice_id}"))
