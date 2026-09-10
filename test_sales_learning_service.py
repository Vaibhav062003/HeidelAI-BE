import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from services.sales_learning_service import (
    choose_guidance,
    detect_language,
    record_reaction,
    record_verified_tally_outcome,
)
from services.sales_case_service import set_verified_sales_outcome


def test_language_detection_keeps_hinglish_and_hindi_separate():
    assert detect_language("please send invoice") == "en"
    assert detect_language("bill bhejo") == "hinglish"
    assert detect_language("बिल भेजो") == "hi"


def test_guidance_needs_evidence_and_uses_a_stable_cohort():
    now = datetime.now(timezone.utc)
    too_small = [{
        "_id": "small", "move": "ask_for_order", "resolved": 29,
        "billed": 20, "paid": 10, "first_outcome_at": now - timedelta(days=30),
    }]
    assert choose_guidance(too_small, "c1", now)["recommended_moves"] == []

    profiles = [{
        "_id": "safe-policy", "move": "ask_for_order", "resolved": 240,
        "billed": 120, "paid": 80, "complaint": 2,
        "first_outcome_at": now - timedelta(days=30),
    }]
    first = choose_guidance(profiles, "same-conversation", now, {"safe-policy"})
    second = choose_guidance(profiles, "same-conversation", now, {"safe-policy"})
    assert first == second
    assert first["policy_version"] == "safe-policy"
    assert first["cohort"] == "test" and first["recommended_moves"] == ["ask_for_order"]


def test_ready_policy_stays_off_until_owner_approval():
    now = datetime.now(timezone.utc)
    profiles = [{
        "_id": "ready", "move": "ask_for_order", "resolved": 240,
        "billed": 120, "paid": 80, "complaint": 2,
        "first_outcome_at": now - timedelta(days=30),
    }]
    assert choose_guidance(profiles, "customer", now)["policy_version"] == "default"
    assert choose_guidance(profiles, "customer", now, {"another-policy"})["policy_version"] == "default"


def test_unapproved_winner_does_not_suppress_an_approved_policy():
    now = datetime.now(timezone.utc)
    profiles = [
        {"_id": "unapproved-winner", "move": "discount", "resolved": 240, "billed": 220, "paid": 200, "complaint": 2, "first_outcome_at": now - timedelta(days=30)},
        {"_id": "approved-runner", "move": "prove_value", "resolved": 240, "billed": 120, "paid": 80, "complaint": 2, "first_outcome_at": now - timedelta(days=30)},
    ]
    result = choose_guidance(profiles, "customer", now, {"approved-runner"})
    assert result["policy_version"] == "approved-runner"
    assert result["recommended_moves"] == ["prove_value"]


def test_unsafe_profile_is_not_used():
    now = datetime.now(timezone.utc)
    result = choose_guidance([{
        "_id": "unsafe", "move": "negotiate", "resolved": 100,
        "billed": 90, "complaint": 30,
        "first_outcome_at": now - timedelta(days=30),
    }], "c1", now)
    assert result["policy_version"] == "default"


def test_negative_customer_reaction_records_a_complaint_for_the_sales_case():
    case = {"case_id": "case-1", "stage": "qualified"}
    recorder = AsyncMock(return_value=True)

    async def record_reactions():
        await record_reaction("org-1", "conversation-1", "event-1", "message-1", "👎🏽", "react", "whatsapp")
        await record_reaction("org-1", "conversation-1", "event-2", "message-1", "❤️", "react", "whatsapp")

    with patch("services.sales_workflow_service.enabled", AsyncMock(return_value=False)), \
            patch("services.sales_case_service.get_active_sales_case", AsyncMock(return_value=case)), \
            patch("services.sales_learning_service.record_sales_learning_event", recorder):
        asyncio.run(record_reactions())

    assert [call.kwargs["event_type"] for call in recorder.await_args_list] == [
        "reaction_added", "complaint", "reaction_added",
    ]
    complaint = recorder.await_args_list[1].kwargs
    assert complaint["case"] == case
    assert complaint["source_id"] == "event-1"


def test_verified_tally_outcome_does_not_replace_action_owner():
    case = {
        "case_id": "case-1", "stage": "invoice_draft",
        "action_source_message_id": "wamid.customer",
    }
    set_outcome = AsyncMock()
    with patch("services.sales_workflow_service.enabled", AsyncMock(return_value=False)), \
            patch("services.sales_case_service.get_active_sales_case", AsyncMock(return_value=case)), \
            patch("services.sales_case_service.set_verified_sales_outcome", set_outcome), \
            patch("services.sales_learning_service.record_sales_learning_event", AsyncMock(return_value=True)):
        asyncio.run(record_verified_tally_outcome(
            "org-1", "conversation-1", "tally-paid:invoice-1", "paid",
        ))

    set_outcome.assert_awaited_once_with("org-1", "case-1", "paid")
    assert case["action_source_message_id"] == "wamid.customer"


def test_verified_outcome_is_monotonic_and_preserves_newer_action():
    class Cases:
        def __init__(self):
            self.case = {
                "case_id": "case-1", "status": "active", "stage": "invoice_draft",
                "billing_status": None, "next_action": "create_quote",
                "action_status": "processing",
                "action_source_message_id": "wamid.newer",
            }

        async def find_one(self, _query):
            return dict(self.case)

        async def find_one_and_update(self, query, update, return_document=None):
            if (
                self.case.get("stage") != query.get("stage")
                or self.case.get("billing_status") != query.get("billing_status")
            ):
                return None
            self.case.update(update["$set"])
            return dict(self.case)

    cases = Cases()
    with patch("services.sales_case_service._collection", return_value=cases):
        asyncio.run(set_verified_sales_outcome("org-1", "case-1", "paid"))
        asyncio.run(set_verified_sales_outcome("org-1", "case-1", "billed"))

    assert cases.case["billing_status"] == "paid"
    assert cases.case["stage"] == "paid"
    assert cases.case["next_action"] == "create_quote"
    assert cases.case["action_status"] == "processing"
    assert cases.case["action_source_message_id"] == "wamid.newer"
