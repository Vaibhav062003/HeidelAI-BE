from datetime import datetime, timedelta, timezone

from services.sales_learning_service import choose_guidance, detect_language


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
