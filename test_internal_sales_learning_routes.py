import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from services.sales_learning_service import approve_sales_learning_policy, internal_learning_report


def _load_routes():
    settings = types.SimpleNamespace(X_INTERNAL_SECRET="secret")
    config = types.ModuleType("config")
    config.settings = settings
    utils = types.ModuleType("services.utils")
    utils.serialize_mongo = lambda value: value
    spec = importlib.util.spec_from_file_location("internal_sales_learning_routes_contract", Path(__file__).parent / "api/internal_sales_learning_routes.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"config": config, "services.utils": utils}):
        spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class _Collection:
    def __init__(self, documents=None):
        self.documents = {str(item["_id"]): dict(item) for item in (documents or [])}

    async def find_one(self, query, *_args):
        for item in self.documents.values():
            if all(key.startswith("$") or item.get(key) == value for key, value in query.items()):
                return dict(item)
        return None

    async def update_one(self, query, update, upsert=False):
        key = str(query["_id"])
        if key in self.documents:
            return _Result()
        self.documents[key] = dict(update["$setOnInsert"])
        return _Result(key)


class _Database(dict):
    def __getitem__(self, key):
        return self.setdefault(key, _Collection())


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def to_list(self, **_kwargs):
        return self.rows


class _ReportEvents:
    def __init__(self, rows):
        self.rows = iter(rows)

    def aggregate(self, _pipeline):
        return _Cursor(next(self.rows))


class _ReportRows:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return _Cursor(self.rows)


def _ready_profile(policy_id="ready", complaint=2):
    return {
        "_id": policy_id, "org_id": "org-1", "stage": "qualifying", "objection": "price",
        "language": "en", "move": "prove_value", "resolved": 40, "billed": 20, "paid": 10,
        "complaint": complaint, "first_outcome_at": datetime.now(timezone.utc) - timedelta(days=30),
    }


def _all_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value), set())
    return set()


def test_private_boundary_and_batch_validation():
    routes = _load_routes()
    routes._require_internal_secret("secret")
    with pytest.raises(HTTPException) as denied:
        routes._require_internal_secret("wrong")
    assert denied.value.status_code == 401
    assert routes.LearningReportsPayload(org_ids=[" org-1 ", "org-1", "org-2"]).org_ids == ["org-1", "org-2"]
    with pytest.raises(ValueError):
        routes.LearningReportsPayload(org_ids=["org-1.$bad"])


def test_approval_route_returns_forbidden_for_a_non_owner():
    routes = _load_routes()
    with patch.object(routes, "approve_sales_learning_policy", new=AsyncMock(side_effect=PermissionError("owner required"))):
        with pytest.raises(HTTPException) as denied:
            asyncio.run(routes.approve_policy(
                "ready", routes.ApprovePolicyPayload(org_id="org-1", actor_user_id="user-other"), "secret",
            ))
    assert denied.value.status_code == 403


def test_policy_approval_is_idempotent_and_rejects_unsafe_candidates():
    db = _Database({
        "organizations": _Collection([{"_id": "org-document", "org_id": "org-1", "owner": {"clerk_user_id": "owner-1"}}]),
        "sales_learning_profiles_org-1": _Collection([_ready_profile(), _ready_profile("unsafe", 12)]),
        "sales_learning_policy_approvals_org-1": _Collection(),
    })
    with patch("services.sales_learning_service._database", return_value=db):
        first = asyncio.run(approve_sales_learning_policy("org-1", "ready", "owner-1"))
        second = asyncio.run(approve_sales_learning_policy("org-1", "ready", "owner-1"))
        with pytest.raises(PermissionError):
            asyncio.run(approve_sales_learning_policy("org-1", "ready", "owner-2"))
        with pytest.raises(ValueError):
            asyncio.run(approve_sales_learning_policy("org-1", "unsafe", "owner-1"))
    assert first == second
    assert second["approved_by"] == "owner-1"
    assert set(second) == {"_id", "org_id", "status", "approved_by", "approved_at", "evidence"}


def test_internal_report_returns_aggregate_learning_proof_without_lead_identity():
    now = datetime.now(timezone.utc)
    profile = {**_ready_profile(), "updated_at": now}
    db = _Database({
        "sales_learning_events_org-1": _ReportEvents([
            [{"_id": "paid", "count": 10}],
            [{"unique_conversations": 40, "last_event_at": now, "customer_reply": 30, "commitment": 20, "invoice_draft": 15, "billed": 12, "paid": 10, "lost": 8, "complaint": 2, "handoff": 1, "resolved": 20}],
            [{"_id": {"policy_id": "ready", "cohort": cohort}, "conversations": 20, "resolved": 10, "billed": 6, "paid": 5, "lost": 4, "complaint": 1} for cohort in ("control", "test")],
        ]),
        "sales_learning_profiles_org-1": _ReportRows([profile]),
        "sales_learning_policy_approvals_org-1": _ReportRows([{"_id": "ready", "approved_by": "owner-1", "approved_at": now}]),
    })
    entitlement = types.ModuleType("services.entitlement_service")
    entitlement.require_feature = lambda *_args: {"state": "active"}
    with patch.dict(sys.modules, {"services.entitlement_service": entitlement}), \
            patch("services.sales_learning_service._database", return_value=db):
        report = asyncio.run(internal_learning_report("org-1"))
    assert report["status"] == "TESTING_10"
    assert report["cohort_comparison_ready"] == ["ready"]
    assert report["rates"]["paid"] == 0.25
    assert "phone" in _all_keys({"nested": {"phone": "positive control"}})
    assert not {"conversation_id", "source_id", "target_message_id", "text", "phone"} & _all_keys(report)


def test_internal_report_checks_active_ai_access_before_tenant_data():
    denied = HTTPException(status_code=403, detail="AI is not active")
    entitlement = types.ModuleType("services.entitlement_service")
    entitlement.require_feature = Mock(side_effect=denied)
    with patch.dict(sys.modules, {"services.entitlement_service": entitlement}), \
            patch("services.sales_learning_service._database") as database:
        with pytest.raises(HTTPException) as error:
            asyncio.run(internal_learning_report("org-1"))
    assert error.value is denied
    database.assert_not_called()
