"""Money and history regressions. All database and provider boundaries are mocked."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_CLUSTER", "test")
os.environ.setdefault("INTERNAL_HMAC_SECRET", "test-only-signing-secret-32-characters")
for key in ("LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT", "AZURE_OPENAI_SUBSCRIPTION_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT"):
    os.environ.setdefault(key, "test")

import pytest
from fastapi import HTTPException
from services.pricing_policy import PriceSnapshot, QuoteCandidate, decide_b2b_pricing, decide_pricing_v1
from services import b2b_pricing_service as b2b, daily_pricing_service as daily

NOW = datetime.now(timezone.utc)


def run(value):
    return asyncio.run(value)


def snapshot(**kwargs):
    return PriceSnapshot(**({"sku": "GPU-A", "approved_floor_price": "100", "target_selling_price": None,
        "selling_gst_inclusive": True, "gst_rate": 18, "stock_available": None,
        "price_source_sheet_id": "sheet", "price_version": "v1", "price_verified_at": NOW} | kwargs))


def candidate(**kwargs):
    return QuoteCandidate(**({"amount": "125", "sku": "GPU-A", "quantity": 10, "gst_inclusive": True,
        "gst_rate": 18, "structured": True, "exact_product_match": True} | kwargs))


def test_b2b_bulk_without_target_or_exact_stock_preserves_v1():
    assert decide_b2b_pricing(snapshot(), candidate(), evidence_ids=("human",), now=NOW).action == "auto_send"
    assert decide_pricing_v1(snapshot(), candidate(), now=NOW).action == "review_required"
    assert decide_b2b_pricing(snapshot(), candidate(), now=NOW).action == "review_required"


@pytest.mark.parametrize("product,quote", [
    ({"approved_floor_price": "126"}, {}), ({"mrp": "120"}, {}),
    ({"price_verified_at": NOW - timedelta(hours=2)}, {}), ({"selling_gst_inclusive": None}, {}),
    ({"price_source_sheet_id": None}, {}), ({}, {"sku": "OTHER"}),
    ({}, {"gst_rate": 12}), ({}, {"structured": False}),
])
def test_b2b_money_and_provenance_fail_closed(product, quote):
    assert decide_b2b_pricing(snapshot(**product), candidate(**quote), evidence_ids=("human",), now=NOW).action == "review_required"


def test_floor_movement_and_tax_comparison():
    assert decide_b2b_pricing(snapshot(approved_floor_price="120"), candidate(), evidence_ids=("human",), now=NOW).action == "auto_send"
    assert decide_b2b_pricing(snapshot(approved_floor_price="126"), candidate(), evidence_ids=("human",), now=NOW).action == "review_required"
    result = decide_b2b_pricing(snapshot(approved_floor_price="100", selling_gst_inclusive=False), candidate(amount="110"), evidence_ids=("human",), now=NOW)
    assert "quote_below_floor" in result.reasons


def test_read_receipts_do_not_relearn_a_human_quote_but_failure_invalidates_it():
    row = {"role": "agent", "content": "GPU-A INR 125 each", "status": "sent"}
    assert b2b.fingerprint(row) == b2b.fingerprint({**row, "status": "read"})
    assert b2b.fingerprint(row) != b2b.fingerprint({**row, "status": "failed"})


def test_historical_floor_is_unknown_without_an_exact_sent_snapshot():
    collection = SimpleNamespace(find_one=AsyncMock(return_value={}))
    with patch.object(b2b, "get_async_mongo_db", return_value={"sales_offers_org": collection}):
        assert run(b2b.historical_floor("org", "product", "message")) == {}
        collection.find_one.return_value = {"minimum_snapshots": [{"product_id": "product", "approved_floor_price": "90", "price_version": "old"}]}
        assert run(b2b.historical_floor("org", "product", "message"))["price_version"] == "old"


def test_greeting_does_not_read_history_or_call_ai():
    from services import agent_service
    with patch.object(b2b, "history_context", AsyncMock()) as history, patch.object(agent_service, "_chatbot_http_client") as client:
        case = {"org_id": "org", "conversation_id": "buyer"}
        assert run(b2b.recommend(case, {"message_id": "greeting", "text": "hello"})) == case
        history.assert_not_awaited()
        client.assert_not_called()


def test_failed_human_source_cannot_support_automatic_pricing():
    source = {"message_id": "m", "conversation_id": "buyer", "role": "agent", "mode": "reply", "status": "failed", "content": "INR 125 each"}
    cursor = SimpleNamespace(limit=lambda n: SimpleNamespace(to_list=AsyncMock(return_value=[source])))
    collection = SimpleNamespace(find=lambda query: cursor)
    evidence = {"conversation_id": "buyer", "source_message_id": "m", "source_hashes": {"m": b2b.fingerprint(source)}}
    with patch.object(b2b, "get_async_mongo_db", return_value={"messages_org": collection}):
        assert run(b2b._valid_sources("org", evidence)) is False


def test_reconcile_never_verifies_a_different_sheet():
    change = {"_id": "change", "status": "uncertain", "product_id": "p", "price_source_sheet_id": "original", "product_type": "component"}
    product = {"_id": "p", "price_source_sheet_id": "different", "type": "component", "minimum_publication": {"change_id": "change"}}
    db = SimpleNamespace(stock_change_requests=SimpleNamespace(find_one=AsyncMock(return_value=change)))
    user = SimpleNamespace(role="admin", org_id="org", user_id="agent")
    with patch.object(daily, "get_async_mongo_db", return_value=db), patch.object(daily, "product_by_id", AsyncMock(return_value=product)), patch.object(daily, "catalogue_org", AsyncMock(return_value="org")):
        with pytest.raises(HTTPException, match="source or ownership changed"):
            run(daily.resolve_minimum(user, "change", "verify"))


def test_shared_catalogue_read_permission_does_not_authorize_price_writes():
    user = SimpleNamespace(role="admin", org_id="org", user_id="agent")
    with patch.object(daily, "catalogue_org", AsyncMock(return_value="another-org")):
        with pytest.raises(HTTPException) as error:
            run(daily.request_minimum(user, {"minimum": "125"}))
        assert error.value.status_code == 403


def test_duplicate_publication_is_not_reapplied():
    user = SimpleNamespace(role="admin", org_id="org", user_id="agent")
    prior = {"product_id": "p", "base_version": "v1", "changes": {"approved_floor_price": "125.00"}, "status": "uncertain"}
    db = SimpleNamespace(stock_change_requests=SimpleNamespace(find_one=AsyncMock(return_value=prior)))
    with patch.object(daily, "catalogue_org", AsyncMock(return_value="org")), patch.object(daily, "get_async_mongo_db", return_value=db), patch.object(daily, "publish_minimum", AsyncMock()) as publish:
        result = run(daily.request_minimum(user, {"product_id": "p", "minimum": "125", "expected_version": "v1", "request_id": "retry"}))
        assert result["status"] == "uncertain"
        publish.assert_not_awaited()
        with pytest.raises(HTTPException):
            run(daily.request_minimum(user, {"product_id": "p", "minimum": "126", "expected_version": "v1", "request_id": "retry"}))


def test_historical_minimum_compares_the_same_tax_basis():
    floor = {"approved_floor_price": "100", "gst_inclusive": False, "gst_rate": 18}
    assert b2b.floor_in_quote_basis(floor, True, 18)["approved_floor_price"] == "118.00"
    assert b2b.floor_in_quote_basis(floor, False, 18)["approved_floor_price"] == "100.00"
    assert b2b.floor_in_quote_basis(floor, True, 12) == {}


def test_dealer_demand_uses_grounded_product_evidence_and_current_request_key():
    from services import product_request_service, product_request_policy, sales_workflow_service
    source = {"message_id": "m", "role": "customer", "content": "need 2 units GPU-A", "timestamp": NOW}
    item = {"line_id": "line", "request": "GPU-A", "source_message_id": "m", "evidence": source["content"]}
    product = {"line_id": "line", "product_id": "p", "name": "GPU A"}
    class DB(dict):
        product_requests = SimpleNamespace(update_one=AsyncMock())
    db = DB({"messages_org": SimpleNamespace(find_one=AsyncMock(return_value=source))})
    async def capture(job, rows, messages):
        assert job["counterparty_role"] == "buyer"
        assert product_request_policy.grounded_request(rows[0], messages)
    with patch.object(b2b, "get_async_mongo_db", return_value=db), patch.object(b2b, "supplier_message", AsyncMock(return_value=False)), patch.object(sales_workflow_service, "catalogue_org", AsyncMock(return_value="org")), patch.object(product_request_service, "capture_product_requests", AsyncMock(side_effect=capture)):
        run(b2b.capture_request({"org_id": "org", "conversation_id": "buyer", "items": [item]}, [product]))
    key = product_request_policy.request_key("org", "buyer", "GPU-A")
    assert db.product_requests.update_one.call_args.args[0]["$or"] == [{"product_key": key}, {"_id": key}]


def test_backend_rejects_variant_prefix_and_failed_tax_support():
    text = "GPU-A10 2 units at INR 120 each, GST 18% included"
    source = {"message_id": "m", "conversation_id": "buyer", "role": "agent", "status": "sent", "content": text, "timestamp": NOW}
    product = {"product_id": "p", "sku": "GPU-A", "name": "GPU A"}
    row = {"source_message_id": "m", "product_id": "p", "unit_price": "120", "quantity": 2,
        "gst_inclusive": True, "gst_rate": "18", "evidence": text, "product_source_id": "m", "product_evidence": "GPU-A",
        "quantity_source_id": "m", "quantity_evidence": "2 units", "tax_source_id": "m", "tax_evidence": "GST 18% included"}
    assert run(b2b.save_examples("org", [row], {"m": source}, [product])) == {}
    source["content"] = text.replace("GPU-A10", "GPU-A")
    row["evidence"] = source["content"]
    row["tax_source_id"] = "failed"
    assert run(b2b.save_examples("org", [row], {"m": source, "failed": {**source, "status": "failed"}}, [product])) == {}
    row["tax_source_id"], row["unit_price"] = "m", "18"
    assert run(b2b.save_examples("org", [row], {"m": source}, [product])) == {}


def test_prepared_message_can_retry_pricing_without_reextracting():
    from services import sales_workflow_service as workflow, agent_service
    case = {"case_id": "case", "org_id": "org", "conversation_id": "buyer", "version": 1,
        "status": "active", "b2b_pricing": True, "prepared_messages": ["m"], "items": []}
    collection = SimpleNamespace(update_one=AsyncMock())
    message = {"message_id": "m", "text": "best price"}
    with patch.object(workflow, "require_enabled", AsyncMock()), patch.object(workflow, "ensure_case", AsyncMock(return_value=case)), patch.object(workflow, "_collection", return_value=collection), patch.object(b2b, "enabled", AsyncMock(return_value=True)), patch.object(b2b, "recommend", AsyncMock(return_value=case)) as recommend, patch.object(workflow, "assess_and_save", AsyncMock(return_value=case)), patch.object(agent_service, "_chatbot_http_client") as client:
        run(workflow.prepare_purchase("org", "buyer", 1, message=message))
        recommend.assert_awaited_once_with({**case, "purchase_path": "quote"}, message)
        client.assert_not_called()


def test_sent_quote_snapshot_binds_sku_and_tax():
    case = {"org_id": "org", "conversation_id": "buyer", "quote_id": "q"}
    offer = {"quote_revision": 2}
    quote = {"quote_id": "q", "revision_number": 2, "items": [
        {"id": "line", "sku": "GPU-B", "quantity": 2, "private_unit_price": 125, "gst_rate": 12, "gst_inclusive": False}]}
    db = {"sales_offers_org": SimpleNamespace(find_one=AsyncMock(return_value=offer)),
          "quotes_org": SimpleNamespace(find_one=AsyncMock(return_value=quote))}
    with patch.object(b2b, "get_async_mongo_db", return_value=db):
        sent = run(b2b.last_sent_quote(case))
    assert sent["items"][0]["sku"] == "GPU-B"
    assert sent["items"][0]["gst_rate"] == 12 and sent["items"][0]["gst_inclusive"] is False


@pytest.mark.parametrize("sent_change,floor,intent", [({}, "100", "accept"), ({}, "126", "review"),
    ({"sku": "GPU-B"}, "100", "review"), ({"gst_rate": 12}, "100", "review"),
    ({"gst_inclusive": False}, "100", "review"), ({"extra_line": True}, "100", "review")])
def test_acceptance_rechecks_actual_sent_terms_and_current_floor(sent_change, floor, intent):
    from services import sales_workflow_service as workflow, agent_service
    item = {"line_id": "line", "product_id": "p", "request": "GPU-A", "quantity": 2}
    price = {**item, "sku": "GPU-A", "unit_price": "125", "gst_rate": "18", "gst_inclusive": True}
    case = {"case_id": "case", "org_id": "org", "conversation_id": "buyer", "version": 1, "quoted_version": 1,
        "status": "active", "items": [item], "b2b_decision": {"prices": [price]}}
    previous = {"quote_id": "q", "revision": 1, "valid_until": (NOW + timedelta(days=1)).date().isoformat(),
        "discount": 0, "items": [{**price, **sent_change}]}
    if sent_change.get("extra_line"):
        previous["items"].append({**price, "line_id": "unvalidated", "sku": "GPU-B"})
    source = {"message_id": "m", "timestamp": NOW}
    db = {"messages_org": SimpleNamespace(find_one=AsyncMock(side_effect=[source, source, None])),
        "quote_settings_org": SimpleNamespace(find_one=AsyncMock(return_value={})),
        "dealer_pricing_memory_org": SimpleNamespace(update_one=AsyncMock())}
    async def save(query, update, **kwargs):
        return {**case, **update["$set"]}
    cases = SimpleNamespace(find_one_and_update=AsyncMock(side_effect=save), update_one=AsyncMock())
    response = SimpleNamespace(raise_for_status=Mock(), json=lambda: {"intent": "accept", "proposals": [], "examples": []})
    with patch.object(b2b, "get_async_mongo_db", return_value=db), patch.object(b2b, "supplier_message", AsyncMock(return_value=False)), patch.object(b2b, "current_product", AsyncMock(return_value=({"_id": "p"}, snapshot(approved_floor_price=floor)))), patch.object(b2b, "capture_request", AsyncMock()), patch.object(b2b, "history_context", AsyncMock(return_value=([], [], {}))), patch.object(b2b, "last_sent_quote", AsyncMock(return_value=previous)), patch.object(b2b, "save_examples", AsyncMock(return_value={})), patch.object(workflow, "_collection", return_value=cases), patch.object(agent_service, "_chatbot_http_client", return_value=SimpleNamespace(post=AsyncMock(return_value=response))):
        result = run(b2b.recommend(case, {"message_id": "m", "text": "proceed"}))
    assert result["b2b_decision"]["intent"] == intent, result["b2b_decision"]
    assert cases.find_one_and_update.call_args.args[0]["b2b_claim"]
