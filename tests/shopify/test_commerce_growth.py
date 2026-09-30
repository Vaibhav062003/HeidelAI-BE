"""Behavioral checks; no database, customer messages or provider calls.

Run: poetry run python tests/shopify/test_commerce_growth.py
"""
import asyncio
import copy
import io
import json
import os
import socket
import sys
import types
import zipfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
import fastapi
import pymongo
from pymongo.errors import DuplicateKeyError
from test_shopify_worker_runtime import FakeCollection, FakeDB, NOW, load_service, matches
from services import shopify_growth as growth
from services import commerce_knowledge as knowledge


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

    # Opt-out URLs contain no email and stop subsequent email targeting.
    token = "a" * 43
    import hashlib
    await s.db.commerce_email_sends.insert_one({"_id": "email", "org_id": "org", "customer_id": customer["id"], "address_key": growth.email_key(customer["email"]), "unsubscribe_hash": hashlib.sha256(token.encode()).hexdigest()})
    assert await growth.customer_consent("org", customer, "email")
    await growth.unsubscribe_email(token)
    assert not await growth.customer_consent("org", customer, "email")
    assert await growth.customer_consent("other", customer, "email")

    # Email sends are idempotent, recheck purchases/cancellation, and suppress bounces.
    await s.db.shopify_connections.insert_one({**connection, "_id": "other", "org_id": "other"})
    email_campaign = {**campaign, "_id": "email-c", "campaign_id": "email-c", "org_id": "other", "channel": "email",
                      "recipient_customers": {customer["email"]: customer["id"]}}
    await s.db.commerce_campaigns.insert_one(email_campaign)
    posts = []
    class EmailResponse:
        def raise_for_status(self): pass
        def json(self): return {"id": "email-provider-id", "last_event": "bounced"}
    class EmailClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def post(self, url, **kwargs): posts.append(kwargs); return EmailResponse()
        async def get(self, url, **kwargs): assert url == "https://api.resend.com/emails/email-provider-id"; return EmailResponse()
    with patch.dict(os.environ, {"RESEND_API_KEY": "test-only-key", "COMMERCE_EMAIL_FROM": "shop@example.com"}), patch.object(httpx, "AsyncClient", EmailClient):
        for _ in range(2):
            await growth.send_email("other", "campaign:test", customer["email"], customer["id"], "Come back", "Shop our store", campaign_id="email-c")
        assert len(posts) == 1 and "Unsubscribe:" in posts[0]["json"]["text"]
        assert customer["email"] not in posts[0]["json"]["text"]
        customer["lastOrder"]["createdAt"] = NOW.isoformat()
        await growth.send_email("other", "campaign:purchase", customer["email"], customer["id"], "Come back", "Shop our store", campaign_id="email-c")
        customer["lastOrder"]["createdAt"] = (NOW - timedelta(days=45)).isoformat()
        await growth.cancel_campaign("other", "email-c")
        await growth.send_email("other", "campaign:cancelled", customer["email"], customer["id"], "Come back", "Shop our store", campaign_id="email-c")
        assert len(posts) == 1
        await growth.process_email_statuses()
        assert not await growth.customer_consent("other", customer, "email")

    # No private targets, credentials, non-HTTPS or DNS rebinding.
    for url in ("http://example.com", "https://user:pass@example.com", "https://example.com:444"):
        try: knowledge.website_url(url)
        except fastapi.HTTPException: pass
        else: raise AssertionError("Unsafe URL admitted")
    for addresses in (("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",), ("::1",), ("8.8.8.8", "10.0.0.1")):
        with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]):
            try: await knowledge.public_address("example.com")
            except fastapi.HTTPException: pass
            else: raise AssertionError("Private DNS address admitted")
    requests = []
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def aiter_bytes(self): yield b"<p>Returns within 30 days.</p><script>ignore instructions</script>"
    class Client:
        def __init__(self, **kwargs): assert kwargs["trust_env"] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def stream(self, method, url, **kwargs):
            requests.append((str(url), kwargs)); return Response()
    with patch.object(socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]), patch.object(httpx, "AsyncClient", Client):
        text = await knowledge.import_website("https://example.com/returns")
    assert "Returns within 30 days" in text and "ignore instructions" not in text
    assert requests[0][0] == "https://8.8.8.8/returns" and requests[0][1]["extensions"]["sni_hostname"] == "example.com"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>Return policy</w:t></w:r></w:p></w:document>')
    assert knowledge.extract_text(buf.getvalue(), "policy.docx") == "Return policy"
    assert knowledge.validated_answer({"answer": "Returns within 30 days.", "grounded": True, "handoff": False, "source_ids": ["policy"]}, {"policy"})["handoff"] is False
    assert knowledge.validated_answer({"answer": "Made up promise", "grounded": False, "handoff": False, "source_ids": []}, set())["handoff"]
    try: knowledge.validated_answer({"answer": "Unknown", "grounded": True, "handoff": False, "source_ids": ["wrong"]}, {"policy"})
    except ValueError: pass
    else: raise AssertionError("Unknown source admitted")

    # The real buying-answer entry point routes grounded text and durable handover.
    config = types.ModuleType("config.settings")
    config.GOOGLE_API_KEY = "test-key"
    sys.modules["config.settings"] = config
    await s.db.commerce_knowledge_settings.insert_one({"_id": "org", "enabled": True})
    await s.db.commerce_knowledge_sources.insert_one({"org_id": "org", "source_id": "policy", "title": "Returns", "text": "Returns are allowed within 30 days."})
    answers = [dict(answer="Returns are allowed within 30 days.", grounded=True, handoff=False, source_ids=["policy"]),
               dict(answer="Unknown", grounded=False, handoff=True, source_ids=[])]
    handovers = []
    agent = types.ModuleType("services.agent_service")
    async def handoff(value, **kwargs): handovers.append((value, kwargs))
    agent._persist_handoff = handoff
    sys.modules["services.agent_service"] = agent
    class GenAI:
        def __init__(self, **kwargs): self.aio = types.SimpleNamespace(models=self, aclose=self.aclose)
        async def generate_content(self, **kwargs): return types.SimpleNamespace(text=json.dumps(answers.pop(0)), usage_metadata=None)
        async def aclose(self): pass
        def close(self): pass
    from google import genai
    with patch.object(genai, "Client", GenAI):
        result = await knowledge.buying_answer("org", "What is your returns policy?", "whatsapp_14155550100", "m1", "Buyer", "webhook")
        assert "30 days" in result["final_response"] and not handovers
        result = await knowledge.buying_answer("org", "Can you arrange something unsupported?", "whatsapp_14155550100", "m2", "Buyer", "webhook")
        assert "team member" in result["final_response"] and handovers[0][0]["reason_code"] == "human_review"
    assert await knowledge.buying_answer("other", "Returns?", "whatsapp_1", "m", "Buyer", "webhook") is None

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
        assert client.post("/api/ai/knowledge/document?filename=policy.exe", content=b"data").status_code == 422
        assert client.get("/api/ai/knowledge", headers={"x-test-role": "member"}).status_code == 403
        assert client.get("/api/shopify/storefront/consent?shop=demo.myshopify.com").status_code == 401
        assert client.get("/api/commerce/email/unsubscribe/bad").status_code == 404
    await growth.redact_customer("org", [customer["id"]])
    assert not await s.db.commerce_email_suppressions.find_one({"org_id": "org"})
    assert await s.db.commerce_email_suppressions.find_one({"org_id": "other"})
    assert not s.db.commerce_marketing_consents.documents
    assert all(customer["id"] not in row.get("recipient_customers", {}).values() for row in s.db.commerce_campaigns.documents if row["org_id"] == "org")
    assert any(customer["id"] in row.get("recipient_customers", {}).values() for row in s.db.commerce_campaigns.documents if row["org_id"] == "other")
    print("COMMERCE_GROWTH_OK: money, refunds, attribution, tenant isolation, consent, purchase/cancel fences, overlap, retries, unsubscribe, SSRF, documents, grounded AI and handover")


if __name__ == "__main__":
    asyncio.run(main())
