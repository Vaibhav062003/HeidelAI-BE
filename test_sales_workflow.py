"""Run with pytest; no providers, customer messages, or live database are used."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from services import sales_workflow_service as workflow


ITEM = {"line_id": "line-a", "request": "RTX 4090", "quantity": 2, "product_id": "gpu-1",
        "source_message_id": "m1", "evidence": "2 units RTX 4090", "quantity_evidence": "2 units", "matches": [], "agent_corrected": False}
CASE = {"case_id": "case-a", "conversation_id": "chat-a", "org_id": "org-a", "version": 3,
        "workflow_version": 2, "status": "active", "items": [ITEM], "action_source_message_id": "revision-3"}


def run(awaitable):
    return asyncio.run(awaitable)


def test_evidence_and_incremental_requirements():
    second = {**ITEM, "line_id": "line-b", "request": "SSD", "product_id": None}
    change = {**ITEM, "operation": "update", "quantity": 3, "source_message_id": "m2", "quantity_evidence": "3 units", "evidence": "make RTX 4090 3 units"}
    result = workflow.merge_changes([ITEM, second], [change], [{"message_id": "m2", "text": change["evidence"]}])
    assert result[0]["quantity"] == 3 and result[1]["request"] == "SSD"
    with pytest.raises(ValueError):
        workflow.merge_changes([ITEM], [{**change, "quantity": 999}], [{"message_id": "m2", "text": change["evidence"]}])
    with pytest.raises(ValueError):
        workflow.merge_changes([ITEM], [{**change, "evidence": "made up"}], [{"message_id": "m2", "text": change["evidence"]}])
    assert workflow.merge_changes([ITEM], [], []) == [ITEM]


def test_stale_correction_is_rejected_without_publish():
    collection = SimpleNamespace(find_one_and_update=AsyncMock(return_value=None))
    with patch.object(workflow, "_collection", return_value=collection), patch.object(workflow, "publish", new_callable=AsyncMock) as publish:
        with pytest.raises(HTTPException) as error:
            run(workflow.save_items(CASE, [ITEM], 2, actor="agent"))
        assert error.value.status_code == 409
        assert collection.find_one_and_update.call_args.args[0]["version"] == 2
        publish.assert_not_awaited()


def test_delivery_rechecks_version_and_commercial_terms():
    import os
    for key in ("LANGCHAIN_API_KEY", "LANGCHAIN_PROJECT", "AZURE_OPENAI_SUBSCRIPTION_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_DEPLOYMENT"):
        os.environ.setdefault(key, "test")
    os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
    os.environ.setdefault("MONGODB_CLUSTER", "test")
    from services.ai_reply_guard import AIProviderUnavailable
    snapshot = {"auto_send": True, "items": [{**ITEM, "unit_price": 100, "gst_rate": 18, "price_version": "p1"}], "retail_link": None}
    job = {"case_id": "case-a", "purchase_version": 3, "source_message_id": "revision-3", "commercial_snapshot": snapshot}
    db = {"sales_action_jobs_org-a": SimpleNamespace(find_one=AsyncMock(return_value=job)), "conversations_org-a": SimpleNamespace(find_one=AsyncMock(return_value={}))}
    cases = SimpleNamespace(find_one=AsyncMock(return_value=CASE))
    with patch.object(workflow, "_database", return_value=db), patch.object(workflow, "_collection", return_value=cases), patch.object(workflow, "enabled", AsyncMock(return_value=True)), patch.object(workflow, "assess", AsyncMock(return_value=snapshot)) as assess:
        run(workflow.validate_delivery("org-a", "job"))
        assess.return_value = {**snapshot, "items": [{**snapshot["items"][0], "unit_price": 101}]}
        with pytest.raises(AIProviderUnavailable):
            run(workflow.validate_delivery("org-a", "job"))
        cases.find_one.return_value = {**CASE, "version": 4}
        assess.reset_mock()
        with pytest.raises(AIProviderUnavailable):
            run(workflow.validate_delivery("org-a", "job"))
        assess.assert_not_awaited()


def test_work_completion_does_not_prove_stock_and_due_summary():
    now = datetime.now(timezone.utc)
    case = {**CASE, "preparation": {"blockers": [{"reason": "Refresh stock"}]}}
    completed = {"case_id": "case-a", "status": "completed", "result": "confirmed"}
    assert workflow.work_summary(case, {}, [completed], [], now)["reason"] == "Refresh stock"
    active = {"case_id": "case-a", "status": "in_progress", "due_at": now - timedelta(hours=1), "assigned_agent_name": "Sam"}
    summary = workflow.work_summary(case, {}, [active], [], now)
    assert summary["attention"] == "blocked" and summary["owner_name"] == "Sam"
    assert workflow.work_summary({**CASE, "billing_status": "paid"}, {}, [active], [], now)["attention"] == "completed"


def test_ambiguous_verified_order_stays_unlinked():
    cursor = SimpleNamespace(limit=lambda n: SimpleNamespace(to_list=AsyncMock(return_value=[CASE, {**CASE, "case_id": "case-b"}])))
    cases = SimpleNamespace(find=lambda query: cursor, find_one_and_update=AsyncMock())
    with patch.object(workflow, "_collection", return_value=cases):
        assert not run(workflow.verified_outcome("org-a", "paid", "provider-event", order={"provider_id": "order", "cart_token": "shared"}))
        cases.find_one_and_update.assert_not_awaited()
        assert not run(workflow.verified_outcome("org-a", "paid", "event", order={"phone": "123"}))


def test_thanks_never_creates_another_quote():
    with patch.object(workflow, "event", new_callable=AsyncMock), patch.object(workflow, "enabled", AsyncMock(return_value=True)), patch("services.ai_policy_service.is_effective_ai_enabled", AsyncMock(return_value=True)), patch.object(workflow, "get_active_sales_case", AsyncMock(return_value=CASE)), patch.object(workflow, "ensure_case", AsyncMock(return_value=CASE)), patch.object(workflow, "prepare_purchase", AsyncMock(return_value=CASE)), patch.object(workflow, "start_action", new_callable=AsyncMock) as action:
        assert run(workflow.automatic_purchase("org-a", "chat-a", "m2", "thanks")) is None
        action.assert_not_awaited()


def test_stock_totals_and_review_mode_use_actual_pricing_policy():
    from services import sales_action_service as actions
    import sys
    stock_service = SimpleNamespace(effective_stock_product=AsyncMock(side_effect=lambda org, product: product), _stock_fresh=lambda value: True)
    now = datetime.now(timezone.utc)
    product = {"unique_identifier": "GPU", "model_name": "RTX 4090", "stock": 3,
               "approved_floor_price": 100, "target_selling_price": 120, "selling_gst_inclusive": True,
               "gst_rate": 18, "price_source_sheet_id": "sheet", "price_verified_at": now,
               "price_version": "v1", "updated_at": now}
    with patch.dict(sys.modules, {"services.stock_service": stock_service}), patch.object(workflow, "catalogue_org", AsyncMock(return_value="org-a")), patch.object(actions, "_product", AsyncMock(return_value=product)), patch("services.pricing_decision_service.get_pricing_settings", AsyncMock(return_value={"mode": "review"})) as settings:
        single = {**CASE, "items": [{**ITEM, "quantity": 1}]}
        initial = run(workflow.assess(single))
        assert initial["auto_send"] is True, initial
        settings.return_value = {"mode": "shadow"}
        assert run(workflow.assess(single))["auto_send"] is False
        settings.return_value = {"mode": "review"}
        combined = {**CASE, "items": [ITEM, {**ITEM, "line_id": "second"}]}
        result = run(workflow.assess(combined))
        assert not result["auto_send"]
        assert any("Combined quantity" in issue["reason"] for issue in result["blockers"])


def test_multiple_quote_lines_share_the_existing_totals_builder():
    from services import sales_action_service as actions
    line = {**ITEM, "name": "GPU", "sku": "GPU", "unit_price": 118, "gst_rate": 18, "gst_inclusive": True}
    preparation = {"auto_send": True, "items": [line, {**line, "line_id": "b", "name": "SSD", "quantity": 1, "unit_price": 236}]}
    jobs = SimpleNamespace(update_one=AsyncMock())
    quotes = SimpleNamespace(update_one=AsyncMock(), find_one=AsyncMock(return_value=None))
    class DB(dict):
        organizations = SimpleNamespace(find_one=AsyncMock(return_value={"org_name": "Merchant"}))
    db = DB({"sales_action_jobs_org-a": jobs, "quotes_org-a": quotes,
             "quote_settings_org-a": SimpleNamespace(find_one=AsyncMock(return_value={})),
             "conversations_org-a": SimpleNamespace(find_one=AsyncMock(return_value={"customer_name": "Customer"}))})
    with patch.object(actions, "get_async_mongo_db", return_value=db), patch.object(workflow, "assess", AsyncMock(return_value=preparation)), patch("services.entitlement_service.require_feature"):
        result = run(actions._create_quote(CASE, "revision-3"))
    document = quotes.update_one.call_args.args[1]["$setOnInsert"]
    assert len(document["items"]) == 2
    assert document["public_grand_total"] == 472
    assert document["private_gst_total"] == 72
    assert document["case_id"] == "case-a" and document["purchase_version"] == 3
    assert "GPU" in result["reply"] and "SSD" in result["reply"]


def test_model_specification_is_not_a_quantity():
    assert not workflow.quantity_supported(16, "16 GB RAM", "RAM")
    assert workflow.quantity_supported(2, "2 units", "RAM")


def test_local_math_url_overrides_hosted_default():
    import os
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-c", "from services.agent_service import CHATBOT_BASE_URL; print(CHATBOT_BASE_URL)"],
        env={**os.environ, "CHATBOT_BASE_URL": "http://127.0.0.1:8001/"},
        text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == "http://127.0.0.1:8001"
