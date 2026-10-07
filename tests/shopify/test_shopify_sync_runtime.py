#!/usr/bin/env python3
"""Run with python3 tests/shopify/test_shopify_sync_runtime.py; no external services."""

import asyncio
import copy
import re
import types
from datetime import datetime, timedelta, timezone

from test_shopify_worker_runtime import FakeCollection, FakeDB, HTTPException, async_noop, load_service


BASE = datetime(2026, 10, 7, 6, tzinfo=timezone.utc)
service = load_service()
real_graphql = service._graphql


def record(kind, number, created=None, updated=None, lines=1):
    created = created or BASE - timedelta(hours=1)
    products = [{"id": f"line-{number}-{i}", "name": "Product", "title": "Product", "quantity": 1} for i in range(lines)]
    return {"id": f"gid://shopify/{'Order' if kind == 'orders' else 'AbandonedCheckout'}/{number}",
            "name": f"#{number}", "createdAt": created.isoformat(), "updatedAt": (updated or created).isoformat(),
            "customer": {"id": f"customer-{number}", "firstName": "Shopper", "defaultPhoneNumber": {"phoneNumber": "+919876543210"}},
            "abandonedCheckoutUrl": f"https://demo.myshopify.com/{number}/recover",
            "lineItems": {"nodes": products, "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "refunds": [], "transactions": [], "netPaymentSet": {"shopMoney": {"amount": "100", "currencyCode": "INR"}},
            "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED"}


class Shopify:
    def __init__(self, records=()):
        self.records = {row["id"]: copy.deepcopy(row) for row in records}
        self.calls = []
        self.fail = None
        self.hook = None

    async def query(self, connection, query, variables, **kwargs):
        service._sync_http_timeout(kwargs.get("timeout", 20))
        self.calls.append((query, copy.deepcopy(variables)))
        if self.hook:
            await self.hook(query, variables)
        if self.fail:
            failure, self.fail = self.fail, None
            raise failure
        if "first" in variables:
            kind = "orders" if "OrderSortKeys" in query else "checkouts"
            root = "orders" if kind == "orders" else "abandonedCheckouts"
            field, start, end = re.fullmatch(r"(created_at|updated_at):>='([^']+)' \1:<'([^']+)'", variables["query"]).groups()
            key = "createdAt" if field == "created_at" else "updatedAt"
            rows = [row for row in self.records.values() if ("/Order/" in row["id"]) == (kind == "orders")
                    and service._parse_datetime(start) <= service._parse_datetime(row[key]) < service._parse_datetime(end)]
            rows.sort(key=lambda row: (row[key], row["id"]) if variables["sortKey"] != "ID" else row["id"])
            offset = int(variables.get("after") or 0)
            selected = rows[offset:offset + variables["first"]]
            return {root: {"nodes": [{key: row[key] for key in ("id", "createdAt", "updatedAt")} for row in selected],
                           "pageInfo": {"hasNextPage": offset + len(selected) < len(rows), "endCursor": str(offset + len(selected)) if selected else None}}}
        row = copy.deepcopy(self.records.get(variables["id"]))
        if not row:
            return {"node": None}
        if "SyncVersion" in query:
            return {"node": {key: row[key] for key in ("id", "createdAt", "updatedAt")}}
        if "ReportLines" in query:
            offset = int(variables["after"])
            selected = row["lineItems"]["nodes"][offset:offset + 100]
            return {"node": {"lineItems": {"nodes": selected, "pageInfo": {"hasNextPage": offset + len(selected) < len(row["lineItems"]["nodes"]), "endCursor": str(offset + len(selected))}}}}
        if "ReportRefundLines" in query:
            raise AssertionError("use the dedicated refund test provider")
        products = row["lineItems"]["nodes"]
        row["lineItems"] = {"nodes": products[:10], "pageInfo": {"hasNextPage": len(products) > 10, "endCursor": "10" if len(products) > 10 else None}}
        return {"node": row}


def setup(records=(), connection=None, orders=(), carts=()):
    clock = [BASE]
    service.utcnow = lambda: clock[0]
    service.SYNC_PASS_SECONDS = 60
    connection = {"_id": "connection-org", "org_id": "org", "shop": "demo.myshopify.com", "status": "connected", "scopes": ["read_orders"],
                  "sync_version": service.SYNC_VERSION, "sync_status": "pending", "sync_requested_at": BASE,
                  "connected_at": BASE, "sync_import_floor_at": BASE - timedelta(days=1),
                  "next_verification_at": service._next_verification(BASE), **(connection or {})}
    service.db = FakeDB(shopify_connections=FakeCollection([connection]), shopify_orders=FakeCollection(orders), shopify_checkouts=FakeCollection(carts))
    service._commerce_event = async_noop
    api = Shopify(records)
    service._graphql = api.query
    return clock, api


def connection():
    return service.db.shopify_connections.documents[0]


async def run(clock):
    await service.sync_shopify_org("org")
    assert connection().get("sync_last_request_count", 0) <= 20
    clock[0] += timedelta(seconds=61)


async def finish(clock, limit=60):
    for _ in range(limit):
        await run(clock)
        if connection()["sync_status"] == "complete":
            return
    raise AssertionError(connection())


async def initial_and_incremental():
    rows = [record(kind, i) for kind in ("orders", "checkouts") for i in range(12)]
    rows += [record("orders", 99, BASE - timedelta(days=50)), record("checkouts", 99, BASE - timedelta(days=50))]
    clock, api = setup(rows)
    await run(clock)
    assert len(service.db.shopify_orders.documents) <= 5 and len(service.db.shopify_checkouts.documents) <= 5
    assert not connection().get("synced_through_at")
    saved_end = connection()["sync_window_end_at"]
    await finish(clock)
    assert connection()["synced_through_at"] == saved_end == BASE
    assert len(service.db.shopify_orders.documents) == len(service.db.shopify_checkouts.documents) == 12
    assert not service.db.shopify_order_notifications.documents
    # An overlapping refresh checks metadata, without hydrating unchanged records.
    api.calls.clear()
    await service.request_sync("org")
    await finish(clock)
    assert api.calls and all("first" in variables for _, variables in api.calls), api.calls
    # Updates to stored older history are allowed; unknown older history is excluded.
    old = record("orders", 88, BASE - timedelta(days=40))
    service.db.shopify_orders.documents.append(service._order_from_graphql("org", "demo.myshopify.com", old))
    old["updatedAt"] = (clock[0] - timedelta(seconds=1)).isoformat()
    old["displayFinancialStatus"] = "REFUNDED"
    api.records[old["id"]] = old
    api.records[rows[-2]["id"]]["updatedAt"] = old["updatedAt"]
    await service.request_sync("org")
    await finish(clock)
    assert next(row for row in service.db.shopify_orders.documents if row["provider_id"] == old["id"])["payment_status"] == "REFUNDED"
    assert not any(row["provider_id"].endswith("/99") for row in service.db.shopify_orders.documents)


async def window_boundaries_and_requests():
    rows = [record("orders", i, BASE - timedelta(hours=1)) for i in range(8)]
    clock, api = setup(rows)
    later = record("orders", 100, BASE + timedelta(seconds=5))
    await run(clock)
    saved = copy.deepcopy(connection()["sync_pages"])
    api.records[later["id"]] = later
    for _ in range(4):
        await service.request_sync("org")
    assert connection()["sync_pages"] == saved and connection()["sync_window_end_at"] == BASE
    for _ in range(5):
        await run(clock)
        if connection().get("synced_through_at"):
            break
    assert connection()["synced_through_at"] == BASE
    assert not any(row["provider_id"] == later["id"] for row in service.db.shopify_orders.documents)
    assert connection()["sync_status"] == "pending"
    # The coalesced request starts a later window once this window has completed.
    await finish(clock)
    assert any(row["provider_id"] == later["id"] for row in service.db.shopify_orders.documents)
    # Exactly the window end is deferred; equal timestamps across IDs are all imported.
    edge = record("orders", 200, clock[0])
    api.records[edge["id"]] = edge
    await service.request_sync("org")
    await finish(clock)
    assert not any(row["provider_id"] == edge["id"] for row in service.db.shopify_orders.documents)
    await service.request_sync("org")
    await finish(clock)
    assert any(row["provider_id"] == edge["id"] for row in service.db.shopify_orders.documents)


async def failures_and_cursor_recovery():
    clock, api = setup([record("orders", i) for i in range(8)])
    api.fail = HTTPException(503, "Shopify is throttled")
    await run(clock)
    assert connection()["sync_status"] == "failed" and not connection().get("synced_through_at")
    end = connection()["sync_window_end_at"]
    await finish(clock)
    assert connection()["synced_through_at"] == end
    clock, api = setup([record("orders", i) for i in range(8)])
    await run(clock)
    api.fail = service._SyncCursorExpired()
    await finish(clock)
    assert connection()["synced_through_at"] == BASE and len(service.db.shopify_orders.documents) == 8
    # Missing page information is an error, never a successful empty window.
    clock, api = setup()
    async def malformed(*_args, **_kwargs):
        return {"orders": {"nodes": []}, "abandonedCheckouts": {"nodes": []}}
    service._graphql = malformed
    await run(clock)
    assert connection()["sync_status"] == "failed" and not connection().get("synced_through_at")
    # A genuinely empty window does complete.
    clock, api = setup()
    await finish(clock)
    assert connection()["synced_through_at"] == BASE


async def large_record_and_mutation():
    large = record("orders", 1, lines=2200)
    clock, api = setup([large])
    await run(clock)
    assert not service.db.shopify_orders.documents and connection()["sync_details"]["orders"]
    assert len(connection()["sync_details"]["orders"]["lineItems"]["nodes"]) > 10
    await finish(clock)
    assert len(service.db.shopify_orders.documents[0]["products"]) == 2200
    # A newer version during child pagination discards the earlier partial version.
    clock, api = setup([large])
    await run(clock)
    api.records[large["id"]] = record("orders", 1, updated=clock[0] - timedelta(seconds=1), lines=3)
    await finish(clock)
    assert len(service.db.shopify_orders.documents[0]["products"]) == 3
    # A change during hydration also restarts rather than combining versions.
    clock, api = setup([record("orders", 1, lines=11)])
    changed = [False]
    async def mutate(query, variables):
        if "ReportLines" in query and not changed[0]:
            changed[0] = True
            api.records[variables["id"]] = record("orders", 1, updated=BASE - timedelta(minutes=1), lines=4)
    api.hook = mutate
    await finish(clock)
    assert len(service.db.shopify_orders.documents[0]["products"]) == 4


async def refund_marker_and_webhook_floor():
    row = record("orders", 1, BASE - timedelta(days=40), BASE - timedelta(days=2))
    stored = service._order_from_graphql("org", "demo.myshopify.com", row)
    clock, api = setup([row], {"synced_through_at": BASE - timedelta(minutes=1)}, orders=[stored])
    await service._apply_webhook(connection(), "refunds/create", {"order_id": 1}, BASE)
    assert service.db.shopify_orders.documents[0]["details_refresh_token"]
    await finish(clock)
    assert not service.db.shopify_orders.documents[0].get("details_refresh_token")
    assert service.db.shopify_orders.documents[0]["payment_status"] == "PAID"
    # A second refund during hydration must remain queued even at the same provider timestamp.
    await service._apply_webhook(connection(), "refunds/create", {"order_id": 1}, clock[0])
    newer = [False]
    async def refund_again(query, _variables):
        if "SyncVersion" in query and not newer[0]:
            newer[0] = True
            await service._apply_webhook(connection(), "refunds/create", {"order_id": 1}, clock[0])
    api.hook = refund_again
    await run(clock)
    assert service.db.shopify_orders.documents[0].get("details_refresh_token")
    api.hook = None
    await finish(clock)
    assert not service.db.shopify_orders.documents[0].get("details_refresh_token")
    await service._apply_webhook(connection(), "orders/updated", {"id": 99, "created_at": (BASE - timedelta(days=50)).isoformat(), "updated_at": clock[0].isoformat()}, clock[0])
    assert len(service.db.shopify_orders.documents) == 1
    await service._apply_webhook(connection(), "checkouts/update", {"id": 99, "created_at": (BASE - timedelta(days=50)).isoformat(), "updated_at": clock[0].isoformat()}, clock[0])
    assert not service.db.shopify_checkouts.documents


async def tombstones_claims_and_downtime():
    row = record("orders", 1)
    deleted = {**service._order_from_graphql("org", "demo.myshopify.com", row), "record_status": "deleted"}
    clock, api = setup([row], orders=[deleted])
    await finish(clock)
    assert service.db.shopify_orders.documents[0]["record_status"] == "deleted"
    assert all("first" in variables for _, variables in api.calls)
    cart = service._checkout_from_graphql("org", "demo.myshopify.com", record("checkouts", 1), historical=False)
    cart.update(record_status="converted", reminder_status="converted")
    incoming = {**cart, "record_status": "abandoned", "provider_updated_at": BASE}
    await service.db.shopify_checkouts.insert_one(cart)
    await service._upsert_newer(service.db.shopify_checkouts, {"org_id": "org", "provider_id": cart["provider_id"]}, incoming)
    assert service.db.shopify_checkouts.documents[0]["record_status"] == "converted"
    # Losing ownership during the API call stops the old worker before insertion.
    clock, api = setup([row])
    async def disconnect(query, _variables):
        if "ShopifyOrder($id" in query:
            connection()["status"] = "disconnected"
            connection().pop("sync_claim_token", None)
    api.hook = disconnect
    await run(clock)
    assert not service.db.shopify_orders.documents and not connection().get("synced_through_at")
    # Catch-up starts at the saved checkpoint, not an arbitrary one-day clamp.
    row = record("orders", 1, BASE - timedelta(days=3))
    clock, api = setup([row], {"synced_through_at": BASE - timedelta(days=4), "sync_import_floor_at": BASE - timedelta(days=10)})
    await finish(clock)
    assert len(service.db.shopify_orders.documents) == 1
    assert any("updated_at" in variables.get("query", "") for _, variables in api.calls)
    clock, api = setup(connection={"synced_through_at": BASE - timedelta(days=70)})
    await run(clock)
    assert connection()["sync_status"] == "failed" and "access limit" in connection()["sync_error"] and not api.calls


async def migration_and_nightly():
    clock, api = setup(connection={"sync_version": 1, "last_sync_at": BASE - timedelta(minutes=30),
        "sync_full": True, "sync_cutoff_at": BASE - timedelta(days=60), "sync_cursor": {"orders": "old"},
        "automation_enabled_at": BASE - timedelta(hours=1)})
    await run(clock)
    assert connection()["synced_through_at"] == BASE
    assert connection()["automation_enabled_at"] == BASE - timedelta(hours=1)
    assert not connection().get("sync_full") and not connection().get("sync_cutoff_at")
    assert all(variables.get("after") != "old" for _, variables in api.calls)
    midnight = datetime(2026, 10, 7, 18, 35, tzinfo=timezone.utc)  # Oct 8, 00:05 IST
    assert service._next_verification(BASE) == midnight
    clock, api = setup(connection={"synced_through_at": BASE, "sync_status": "complete", "last_sync_at": midnight - timedelta(minutes=1)})
    clock[0] = midnight - timedelta(seconds=1)
    await service.process_shopify_syncs()
    assert not api.calls
    clock[0] = midnight
    await service.process_shopify_syncs()
    assert connection()["sync_mode"] == "verification" and connection()["synced_through_at"] == BASE
    assert connection()["last_verification_at"] == midnight
    assert all("updated_at" in variables["query"] and variables["first"] <= 5 for _, variables in api.calls)
    safe = await service.get_connection("org")
    assert safe["sync_catching_up"] and safe["sync_lag_seconds"] > 1800
    assert "sync_details" not in safe and "sync_claim_token" not in safe


async def durable_writes_fairness_and_webhook_versions():
    class FailProgressOnce(FakeCollection):
        failed = False
        async def update_one(self, query, update, **kwargs):
            if not self.failed and update.get("$set", {}).get("sync_counts", {}).get("orders", {}).get("scanned"):
                self.failed = True
                raise RuntimeError("database unavailable after record save")
            return await super().update_one(query, update, **kwargs)
    row = record("orders", 1)
    row["checkoutToken"] = "checkout-1"
    cart = service._checkout_from_graphql("org", "demo.myshopify.com", record("checkouts", 1), historical=False)
    cart.update(_id="cart-1", checkout_token="checkout-1", reminder_status="pending")
    clock, api = setup([row], carts=[cart])
    service.db.shopify_connections = FailProgressOnce(service.db.shopify_connections.documents)
    service.db.collections["shopify_connections"] = service.db.shopify_connections
    await run(clock)
    assert connection()["sync_status"] == "failed" and not connection().get("synced_through_at")
    assert len(service.db.shopify_orders.documents) == 1
    await finish(clock)
    assert len(service.db.shopify_orders.documents) == 1
    assert service.db.shopify_checkouts.documents[0]["reminder_status"] == "converted"
    # An expired claimed worker can be reclaimed; an unexpired worker cannot.
    clock, api = setup([record("orders", 1)], {"sync_status": "syncing", "sync_claim_token": "other", "sync_claimed_at": BASE})
    await run(clock)
    assert not api.calls
    clock[0] += timedelta(minutes=6)
    await finish(clock)
    assert len(service.db.shopify_orders.documents) == 1
    # Rotate stores instead of selecting the same first five repeatedly.
    clock, api = setup([record("orders", i) for i in range(20)])
    template = copy.deepcopy(connection())
    service.db.shopify_connections.documents = [{**template, "_id": f"store-{i}", "org_id": f"org-{i}"} for i in range(6)]
    await service.process_shopify_syncs()
    assert all(row.get("sync_window_end_at") for row in service.db.shopify_connections.documents[:5])
    assert not service.db.shopify_connections.documents[5].get("sync_window_end_at")
    clock[0] += timedelta(seconds=61)
    await service.process_shopify_syncs()
    assert service.db.shopify_connections.documents[5].get("sync_window_end_at")
    # The latest stored version, rather than the last delivered webhook, wins.
    clock, api = setup()
    payload = {"id": 1, "created_at": (BASE - timedelta(hours=1)).isoformat(), "updated_at": (BASE - timedelta(minutes=1)).isoformat(), "financial_status": "paid"}
    await service._apply_webhook(connection(), "orders/updated", payload, BASE)
    saved_token = service.db.shopify_orders.documents[0]["details_refresh_token"]
    payload.update(updated_at=(BASE - timedelta(minutes=2)).isoformat(), financial_status="pending")
    await service._apply_webhook(connection(), "orders/updated", payload, BASE - timedelta(minutes=2))
    assert service.db.shopify_orders.documents[0]["payment_status"] == "PAID"
    assert service.db.shopify_orders.documents[0]["details_refresh_token"] == saved_token


async def refund_pagination_and_http_budget():
    class RefundShopify(Shopify):
        async def query(self, connection, query, variables, **kwargs):
            if "ReportRefundLines" in query:
                service._sync_http_timeout(20)
                self.calls.append((query, copy.deepcopy(variables)))
                refund = next(refund for row in self.records.values() for refund in row.get("refunds", []) if refund["id"] == variables["id"])
                lines = refund["refundLineItems"]["nodes"]
                offset = int(variables["after"])
                return {"node": {"refundLineItems": {"nodes": lines[offset:offset + 100], "pageInfo": {"hasNextPage": offset + 100 < len(lines), "endCursor": str(offset + 100)}}}}
            result = await super().query(connection, query, variables, **kwargs)
            if "ShopifyOrder($id" in query:
                for refund in result["node"]["refunds"]:
                    lines = refund["refundLineItems"]["nodes"]
                    refund["refundLineItems"] = {"nodes": lines[:10], "pageInfo": {"hasNextPage": len(lines) > 10, "endCursor": "10"}}
            return result
    row = record("orders", 1)
    row["refunds"] = [{"id": "refund-1", "refundLineItems": {"nodes": [{"quantity": 1, "subtotalSet": {"shopMoney": {"amount": "1", "currencyCode": "INR"}}, "lineItem": {"id": "line-1-0"}} for _ in range(2200)]}}]
    clock, _ = setup()
    api = RefundShopify([row])
    service._graphql = api.query
    await run(clock)
    assert not service.db.shopify_orders.documents and connection()["sync_details"]["orders"]
    await finish(clock)
    assert service.db.shopify_orders.documents[0]["products"][0]["refunded_quantity"] == 2200
    assert not service.db.shopify_orders.documents[0]["refund_lines_incomplete"]

    # Exercise the actual HTTP helper: retries and token refresh consume the same budget.
    clock, _ = setup()
    saved_httpx, seal, unseal, access = service.httpx, service._seal, service._unseal, service._access_token
    responses = []
    class Response:
        status_code = 200
        headers = {}
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def post(self, url, **kwargs):
            responses.append(url)
            if "oauth" in url:
                return Response({"access_token": "fresh", "expires_in": 3600})
            return Response({"data": {"orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}}})
    service.httpx = types.SimpleNamespace(AsyncClient=Client, HTTPError=type("HTTPError", (Exception,), {}))
    service._seal = service._unseal = lambda value: value
    connection().update(access_token="old", refresh_token="refresh", token_expires_at=BASE)
    budget = {"requests": 0, "deadline": service.time.monotonic() + 60}
    context = service._sync_budget.set(budget)
    try:
        local = copy.deepcopy(connection())
        for _ in range(19):
            await real_graphql(local, service.ORDERS_QUERY, {"first": 5})
        assert len(responses) == budget["requests"] == 20
        assert sum("oauth" in url for url in responses) == 1
        try:
            await real_graphql(local, service.ORDERS_QUERY, {"first": 5})
            raise AssertionError("request budget was exceeded")
        except service._SyncYield:
            pass
        budget.update(requests=0, deadline=service.time.monotonic() + 0.05)
        async def throttled(self, url, **kwargs):
            responses.append(url)
            return Response({"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]})
        Client.post = throttled
        try:
            await real_graphql(local, service.ORDERS_QUERY, {"first": 5})
            raise AssertionError("throttle sleep exceeded the remaining time budget")
        except service._SyncYield:
            pass
        assert budget["requests"] == 1
    finally:
        service._sync_budget.reset(context)
        service.httpx, service._seal, service._unseal, service._access_token = saved_httpx, seal, unseal, access


async def reconnect_and_repeated_child_cursor():
    clock, api = setup(connection={"synced_through_at": BASE - timedelta(hours=1), "connected_at": BASE - timedelta(days=5),
        "sync_window_start_at": BASE - timedelta(hours=1, minutes=10), "sync_window_end_at": BASE - timedelta(minutes=10),
        "sync_cursor": {"orders": "saved-cursor"}, "sync_mode": "incremental", "automation_enabled_at": BASE - timedelta(days=2),
        "order_notifications_enabled_at": BASE - timedelta(days=2)})
    before = copy.deepcopy(connection())
    saved_seal, saved_verify, saved_exchange, saved_register = service._seal, service.verify_callback_hmac, service._exchange_token, service._register_webhooks
    service._seal = lambda value: value
    service.verify_callback_hmac = lambda params: None  # HMAC has its own existing contract.
    async def exchange(*_args): return {"access_token": "renewed", "scope": "read_orders"}
    service._exchange_token, service._register_webhooks = exchange, async_noop
    params = {"shop": "demo.myshopify.com", "code": "code", "state": "state"}
    def oauth_state():
        service.db.collections["shopify_oauth_states"] = FakeCollection([{
            "_id": service.hashlib.sha256(b"state").hexdigest(), "org_id": "org", "shop": params["shop"],
            "user_id": "admin", "expires_at": BASE + timedelta(minutes=10)}])
    try:
        oauth_state()
        await service.complete_oauth(params)
        for key in ("synced_through_at", "connected_at", "sync_window_start_at", "sync_window_end_at", "sync_cursor", "automation_enabled_at", "order_notifications_enabled_at"):
            assert connection()[key] == before[key], key
        connection()["status"] = "disconnected"
        oauth_state()
        await service.complete_oauth(params)
        assert connection()["synced_through_at"] is None and connection()["sync_cursor"] == {}
        assert connection()["sync_import_floor_at"] == BASE - timedelta(days=1)
    finally:
        service._seal, service.verify_callback_hmac, service._exchange_token, service._register_webhooks = saved_seal, saved_verify, saved_exchange, saved_register
    # A repeated cursor must be rejected before duplicate child data is persisted.
    clock, api = setup([record("orders", 1, lines=200)])
    real_query = api.query
    repeated = [False]
    async def repeat_once(connection, query, variables, **kwargs):
        result = await real_query(connection, query, variables, **kwargs)
        if "ReportLines" in query and not repeated[0]:
            repeated[0] = True
            result["node"]["lineItems"]["pageInfo"]["endCursor"] = variables["after"]
        return result
    service._graphql = repeat_once
    await run(clock)
    assert connection()["sync_status"] == "failed"
    assert len(connection()["sync_details"]["orders"]["lineItems"]["nodes"]) == 10
    await finish(clock)
    assert len(service.db.shopify_orders.documents[0]["products"]) == 200


async def main():
    for check in (initial_and_incremental, window_boundaries_and_requests, failures_and_cursor_recovery,
                  large_record_and_mutation, refund_marker_and_webhook_floor, tombstones_claims_and_downtime,
                  migration_and_nightly, durable_writes_fairness_and_webhook_versions,
                  refund_pagination_and_http_budget, reconnect_and_repeated_child_cursor):
        await check()
        print(f"PASS {check.__name__}")
    print("SHOPIFY_SYNC_RUNTIME_OK")


if __name__ == "__main__":
    asyncio.run(main())
