"""Behavioral checks; no database, customer messages or provider calls.

Run: poetry run python tests/shopify/test_commerce_growth.py
"""
import asyncio
import copy
import json
import sys
import types
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
import fastapi
import pymongo
from pymongo.errors import DuplicateKeyError
from test_shopify_worker_runtime import FakeCollection, FakeDB, NOW, load_service, matches
from services import shopify_growth as growth


class Collection(FakeCollection):
    async def insert_one(self, row):
        if row.get("_id") is not None and any(item.get("_id") == row["_id"] for item in self.documents):
            raise DuplicateKeyError("Duplicate")
        self.documents.append(copy.deepcopy(row))
        return types.SimpleNamespace(inserted_id=row.get("_id"))

    async def delete_many(self, query):
        self.documents = [row for row in self.documents if not matches(row, query)]

    async def delete_one(self, query):
        await self.delete_many(query)


class DB(FakeDB):
    def __getattr__(self, name):
        return self.collections.setdefault(name, Collection())

    __getitem__ = __getattr__


async def main():
    # Reuse the existing worker fixture, restoring optional-package stubs afterwards.
    modules = dict(sys.modules)
    s = load_service()
    for name in list(sys.modules):
        if name in modules:
            sys.modules[name] = modules[name]
        elif name.startswith(("config", "database", "schemas", "cryptography", "services.wa_service", "services.websocket_service")):
            del sys.modules[name]
    sys.modules["services.shopify_service"] = s
    s.DuplicateKeyError = DuplicateKeyError
    s.db = DB()
    s.utcnow = lambda: NOW
    growth.service = lambda: s
    customer = {"id": "gid://shopify/Customer/1", "displayName": "Buyer", "numberOfOrders": "1", "email": "buyer@example.com",
                "emailMarketingConsent": {"marketingState": "SUBSCRIBED"}, "defaultPhoneNumber": {"phoneNumber": "+14155550100"},
                "lastOrder": {"createdAt": (NOW - timedelta(days=45)).isoformat()}}
    connection = {"_id": "org", "org_id": "org", "shop": "demo.myshopify.com", "status": "connected"}
    s.db.shopify_connections = Collection([connection])
    s.db.collections["shopify_connections"] = s.db.shopify_connections
    async def graphql(_connection, query, variables, **kwargs):
        if "customers(" in query:
            return {"customers": {"nodes": [copy.deepcopy(customer)], "pageInfo": {"hasNextPage": False}}}
        if "customer(id:" in query:
            return {"customer": copy.deepcopy(customer)}
        return {"shop": {"currencyCode": "INR"}, "products": {"nodes": []}}
    s._graphql = graphql
    wa = types.ModuleType("services.wa_service")
    wa.get_broadcast_suppressed_phone_keys = lambda _org, _phones: set()
    sys.modules["services.wa_service"] = wa

    # Exact decimal totals, cancellation/refund adjustment, duplicates, currency and tenant fences.
    touch = {"org_id": "org", "kind": "recovery", "order_id": "order1", "sent_at": NOW - timedelta(days=2)}
    order = {"org_id": "org", "provider_id": "order1", "customer_id": customer["id"], "created_at": NOW - timedelta(days=1),
             "payment_status": "PARTIALLY_REFUNDED", "net_payment": {"amount": "80.10", "currency": "INR"}}
    second = {**order, "provider_id": "order2", "net_payment": {"amount": "0.20", "currency": "USD"}}
    touches = [touch, {**touch, "kind": "campaign", "campaign_id": "c", "customer_id": customer["id"], "sent_at": NOW - timedelta(hours=36)}]
    result = growth.attribution([order, order, second, {**order, "org_id": "other", "provider_id": "foreign"}], touches, NOW)
    assert result["matched_orders"] == 2 and result["sales"] == [{"currency": "INR", "amount": "80.10"}, {"currency": "USD", "amount": "0.20"}]
    assert all(row["campaign_id"] == "c" for row in result["orders"])
    for changed in ({"cancelled_at": NOW}, {"record_status": "deleted"}, {"created_at": NOW - timedelta(days=10)}, {"created_at": NOW + timedelta(hours=1)}):
        assert growth.attribution([{**order, **changed}], [touch], NOW)["matched_orders"] == 0
    unpaid = {**order, "net_payment": None, "payment_status": "PENDING"}
    assert growth.attribution([unpaid], [touch], NOW)["value_unavailable"] == 1
    assert s._webhook_net_payment({"financial_status": "partially_refunded", "currency": "INR", "total_price": "100.10", "refunds": [{"transactions": [{"kind": "refund", "status": "success", "amount": "20.00"}]}]})["amount"] == "80.10"
    assert s._webhook_net_payment({"financial_status": "partially_refunded", "total_price": "100.10"}) is None
    assert s._template_compatible({"status": "APPROVED", "category": "MARKETING", "components": [{"type": "BODY", "text": "Come back and shop"}]}, "campaign")
    assert not s._template_compatible({"status": "APPROVED", "category": "MARKETING", "components": [{"type": "BODY", "text": "Hi {{1}}"}]}, "campaign")

    # An actual Shopify count avoids treating a 60-day import as lifetime history.
    assert growth.segment_matches(customer, "first_time", 30, NOW)
    assert growth.segment_matches({**customer, "numberOfOrders": "7"}, "repeat", 30, NOW)
    assert growth.segment_matches(customer, "lapsed", 30, NOW)
    assert not growth.segment_matches(customer, "lapsed", 60, NOW)
    payload = {"channel": "whatsapp", "segment": "first_time", "lapsed_days": 30}
    assert (await growth.audience("org", payload))["eligible"] == 0
    consent = {"_id": f"org:{customer['id']}:whatsapp", "org_id": "org", "customer_id": customer["id"], "granted": True, "address_key": "14155550100"}
    await s.db.commerce_marketing_consents.insert_one(consent)
    assert await s.recovery_permission({"org_id": "org", "customer_id": customer["id"], "phone": "+14155550100"})
    assert not await s.recovery_permission({"org_id": "org", "customer_id": customer["id"], "phone": "+14155550999"})
    assert not await s.recovery_permission({"org_id": "other", "customer_id": customer["id"], "phone": "+14155550100"})
    assert (await growth.audience("org", payload))["eligible"] == 1
    campaign = {"_id": "c", "campaign_id": "c", "org_id": "org", "shop": connection["shop"], "status": "running",
                "created_at": NOW - timedelta(days=1), "recipient_customers": {"14155550100": customer["id"]}, **payload}
    await s.db.commerce_campaigns.insert_one(campaign)
    assert not await growth.recipient_eligible("other", "c", "14155550100")
    assert await growth.recipient_eligible("org", "c", "14155550100")
    assert not await growth.recipient_eligible("org", "c", "14155550100")
    # The same atomic allowance protects recovery versus campaigns in either order.
    assert not await s.reserve_marketing_contact("org", "whatsapp", "14155550100", "recovery:cart")
    assert await s.reserve_marketing_contact("other", "whatsapp", "14155550100", "c")
    winners = await asyncio.gather(*(s.reserve_marketing_contact("org", "whatsapp", "14155550101", str(index)) for index in range(10)))
    assert sum(winners) == 1
    customer["lastOrder"]["createdAt"] = NOW.isoformat()
    assert not await growth.recipient_eligible("org", "c", "14155550100", reserve=False)
    customer["lastOrder"]["createdAt"] = (NOW - timedelta(days=45)).isoformat()
    wa.get_broadcast_suppressed_phone_keys = lambda _org, _phones: {"14155550100"}
    assert not await growth.recipient_eligible("org", "c", "14155550100", reserve=False)
    wa.get_broadcast_suppressed_phone_keys = lambda _org, _phones: set()
    await s.db.commerce_campaigns.update_one({"_id": "c"}, {"$set": {"status": "cancelled"}})
    assert not await growth.recipient_eligible("org", "c", "14155550100", reserve=False)
    await s.db.commerce_campaigns.update_one({"_id": "c"}, {"$set": {"status": "running"}})
    await s.db.shopify_connections.update_one({"_id": "org"}, {"$set": {"shop": "replacement.myshopify.com"}})
    assert not await growth.recipient_eligible("org", "c", "14155550100", reserve=False)
    await s.db.shopify_connections.update_one({"_id": "org"}, {"$set": {"shop": connection["shop"]}})

    # Merchant scheduling retries produce one persistent campaign and never send now.
    entitlements = types.ModuleType("services.entitlement_service")
    entitlements.require_feature = lambda *_: None
    entitlements.feature_access = lambda *_: {"limits": {"max_batch_recipients": 50}}
    sys.modules["services.entitlement_service"] = entitlements
    async def templates(_org): return [{"name": "retention", "language": "en"}]
    growth.campaign_templates = templates
    values = {**payload, "name": "First order thanks", "scheduled_for": NOW + timedelta(minutes=5), "template_name": "retention", "template_language": "en"}
    first = await growth.create_campaign("org", values, "request-123456789")
    again = await growth.create_campaign("org", values, "request-123456789")
    assert first["campaign_id"] == again["campaign_id"] and "recipient_customers" not in first
    assert len(s.db.commerce_campaigns.documents) == 2

    # Reject unsupported channels and stop legacy email campaigns without provider calls.
    await s.db.shopify_connections.insert_one({**connection, "_id": "other", "org_id": "other"})
    email_campaign = {**campaign, "_id": "email-c", "campaign_id": "email-c", "channel": "email", "status": "scheduled", "scheduled_for": NOW,
                      "recipient_customers": {customer["email"]: customer["id"]}}
    await s.db.commerce_campaigns.insert_one(email_campaign)
    assert not await growth.customer_consent("org", customer, "email")
    assert not await growth.recipient_eligible("org", "email-c", customer["email"])
    for work in (growth.audience("org", {**payload, "channel": "email"}),
                 growth.create_campaign("org", {**values, "channel": "email"}, "email-123456789")):
        try: await work
        except fastapi.HTTPException as error: assert error.status_code == 422
        else: raise AssertionError("An email campaign was admitted")
    await growth.process_campaigns()
    assert (await s.db.commerce_campaigns.find_one({"_id": "email-c"}))["status"] == "cancelled"
    assert not hasattr(growth, "send_email") and not hasattr(growth, "process_email_recovery")
    assert all(item["channel"] == "whatsapp" for item in await growth.list_campaigns("org"))

    # A due WhatsApp campaign still dispatches through the existing broadcast worker.
    await s.db.shopify_connections.insert_one({**connection, "_id": "worker", "org_id": "worker"})
    await s.db.commerce_marketing_consents.insert_one({**consent, "_id": f"worker:{customer['id']}:whatsapp", "org_id": "worker"})
    await s.db.commerce_campaigns.insert_one({**campaign, "_id": "due", "campaign_id": "due", "org_id": "worker",
                                             "status": "scheduled", "scheduled_for": NOW, "total": 1,
                                             "template_name": "retention", "template_language": "en"})
    dispatches = []
    async def quota(*_args): pass
    entitlements.consume_quota_async = quota
    async def sender(_connection): return {"wa_id": "wa", "phone_id": "phone", "access_token": "test"}
    s._selected_whatsapp_connection = sender
    def job(org_id, identifier, *_args):
        s.db.bulk_jobs.documents.append({"org_id": org_id, "job_id": identifier, "status": "running"})
    async def broadcast(*args, **kwargs):
        assert args[2] == "worker" and kwargs["campaign_id"] == "due"
        assert await growth.recipient_eligible("worker", "due", args[7][0].phone_number)
        dispatches.append(args[7][0].phone_number)
        await s.db.bulk_jobs.update_one({"job_id": "due"}, {"$set": {"status": "completed"}})
    wa.create_bulk_job = job
    wa.run_bulk_send_job = broadcast
    await growth.process_campaigns()
    assert dispatches == ["14155550100"]
    assert (await s.db.commerce_campaigns.find_one({"_id": "due"}))["status"] == "completed"

    # Historical email data is still removed by customer redaction, scoped to the organization.
    await s.db.commerce_email_sends.insert_one({"_id": "legacy", "org_id": "org", "customer_id": customer["id"], "address_key": "legacy-hash"})
    for org in ["org", "other"]:
        await s.db.commerce_email_suppressions.insert_one({"_id": f"{org}:legacy-hash", "org_id": org})
    await s.db.commerce_campaigns.insert_one({**campaign, "_id": "foreign", "campaign_id": "foreign", "org_id": "other"})

    # Real FastAPI/Pydantic boundary: role checks, scoped results and malformed payloads.
    from typing import Annotated
    from fastapi import Depends, Request
    from fastapi.testclient import TestClient
    from auth.models import CurrentUserModel
    dependencies = types.ModuleType("auth.dependencies")
    async def fixture_user(request: Request):
        return CurrentUserModel(user_id="test", org_id=request.headers.get("x-test-org", "org"), role=request.headers.get("x-test-role", "admin"))
    dependencies.CurrentUser = Annotated[CurrentUserModel, Depends(fixture_user)]
    sys.modules["auth.dependencies"] = dependencies
    limiter = types.ModuleType("core.limiter")
    limiter.limiter = types.SimpleNamespace(limit=lambda *_: lambda function: function)
    sys.modules["core.limiter"] = limiter  # Rate limiting itself is unchanged by this feature.
    import importlib.util
    spec = importlib.util.spec_from_file_location("growth_routes_test", Path(__file__).resolve().parents[2] / "api/commerce_growth_routes.py")
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    app = fastapi.FastAPI()
    app.include_router(routes.router)
    app.state.limiter = routes.limiter
    with TestClient(app) as client:
        assert client.post("/api/commerce/shopify/campaigns/preview", json=payload, headers={"x-test-role": "member"}).status_code == 403
        assert client.put("/api/commerce/shopify/growth-settings", json={"attribution_days": 7}, headers={"x-heidel-organization": "other"}).status_code == 409
        assert client.post("/api/commerce/shopify/campaigns/preview", json={**payload, "org_id": "other"}).status_code == 422
        invalid = {**values, "request_id": "request-123456789", "scheduled_for": "2020-01-01T00:00:00Z"}
        assert client.post("/api/commerce/shopify/campaigns", json=invalid).status_code == 422
        response = client.put("/api/commerce/shopify/growth-settings", json={"attribution_days": 7, "message_rate": {"amount": "0.10", "currency": "INR"}})
        assert response.status_code == 200 and response.json()["message_rate"]["amount"] == "0.10"
        assert client.put("/api/commerce/shopify/growth-settings", json={"message_rate": {"amount": "-0.1", "currency": "INR"}}).status_code == 422
        assert client.get("/api/shopify/storefront/consent?shop=demo.myshopify.com").status_code == 401
        assert client.post("/api/commerce/shopify/campaigns/preview", json={**payload, "channel": "email"}).status_code == 422
        assert client.put("/api/commerce/shopify/growth-settings", json={"email_recovery_enabled": True}).status_code == 422
        assert client.get("/api/ai/knowledge").status_code == 404
        assert client.get("/api/commerce/email/unsubscribe/bad").status_code == 404
        assert "email_ready" not in client.get("/api/commerce/shopify/growth-settings").json()
    await growth.redact_customer("org", [customer["id"]])
    assert not await s.db.commerce_email_suppressions.find_one({"org_id": "org"})
    assert await s.db.commerce_email_suppressions.find_one({"org_id": "other"})
    assert all(row["org_id"] != "org" for row in s.db.commerce_marketing_consents.documents)
    assert await s.db.commerce_marketing_consents.find_one({"org_id": "worker"})
    assert all(customer["id"] not in row.get("recipient_customers", {}).values() for row in s.db.commerce_campaigns.documents if row["org_id"] == "org")
    assert any(customer["id"] in row.get("recipient_customers", {}).values() for row in s.db.commerce_campaigns.documents if row["org_id"] == "other")
    print("COMMERCE_GROWTH_OK: money, refunds, attribution, tenant isolation, consent, purchase/cancel fences, overlap, retries, unsupported email rejection, legacy cancellation and privacy cleanup")


if __name__ == "__main__":
    asyncio.run(main())
