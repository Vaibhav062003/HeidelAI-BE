"""Private, derived sales learning signals. Raw conversations stay in message collections."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

EVENT_TYPES = frozenset({
    "customer_reply", "reaction_added", "reaction_removed", "ai_decision",
    "message_delivered", "message_failed", "commitment", "invoice_draft",
    "billed", "paid", "lost", "complaint", "handoff", "correction",
})
PROFILE_EVENTS = frozenset({"commitment", "billed", "paid", "lost", "complaint"})
_INDEXED_ORGS: set[str] = set()


def _database():
    from database import get_async_mongo_db
    return get_async_mongo_db()


def detect_language(text: str) -> str:
    value = str(text or "")
    if any("\u0900" <= char <= "\u097f" for char in value):
        return "hi"
    hindi_words = {"karo", "kara", "bhejo", "nahi", "chahiye", "kitna", "haan", "abhi"}
    words = set("".join(char if char.isalnum() else " " for char in value.casefold()).split())
    return "hinglish" if words & hindi_words else "en"


def _segment(case: dict[str, Any], language: str) -> tuple[str, str, str, str]:
    history = case.get("stage_history") or []
    stage = str((history[-1] if history else {}).get("from") or case.get("stage") or "new")[:40]
    objection = str((case.get("signals") or {}).get("objection") or "none")[:80]
    move = str((case.get("last_move") or {}).get("type") or "unknown")[:80]
    return stage, objection, language, move


def choose_guidance(profiles: list[dict[str, Any]], conversation_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    eligible = []
    for profile in profiles:
        resolved = int(profile.get("resolved") or 0)
        first = profile.get("first_outcome_at")
        age_ok = isinstance(first, datetime) and first <= now - timedelta(days=7)
        complaint_rate = float(profile.get("complaint") or 0) / max(1, resolved)
        if resolved >= 30 and age_ok and complaint_rate <= 0.2:
            eligible.append(profile)
    if not eligible:
        return {"policy_version": "default", "cohort": "control", "recommended_moves": []}
    eligible.sort(key=lambda item: (
        float(item.get("paid") or 0) / max(1, int(item.get("resolved") or 0)),
        float(item.get("billed") or 0) / max(1, int(item.get("resolved") or 0)),
    ), reverse=True)
    best = eligible[0]
    resolved = int(best.get("resolved") or 0)
    rollout = 10 if resolved < 120 else 50 if resolved < 240 else 100
    bucket = int(hashlib.sha256(conversation_id.encode()).hexdigest()[:8], 16) % 100
    cohort = "test" if bucket < rollout else "control"
    return {
        "policy_version": str(best.get("_id") or "default")[:80],
        "cohort": cohort,
        "recommended_moves": [str(best.get("move"))] if cohort == "test" and best.get("move") else [],
        "sample_size": resolved,
    }


async def _ensure_indexes(org_id: str) -> None:
    if org_id in _INDEXED_ORGS:
        return
    db = _database()
    events = db[f"sales_learning_events_{org_id}"]
    await events.create_index("expires_at", expireAfterSeconds=0)
    await events.create_index([("conversation_id", 1), ("created_at", -1)])
    await db[f"sales_learning_profiles_{org_id}"].create_index(
        [("stage", 1), ("objection", 1), ("language", 1), ("resolved", -1)]
    )
    _INDEXED_ORGS.add(org_id)


async def _update_profile(org_id: str, event_type: str, case: dict[str, Any], language: str, now: datetime) -> None:
    if event_type not in PROFILE_EVENTS:
        return
    stage, objection, language, move = _segment(case, language)
    profile_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{org_id}:{stage}:{objection}:{language}:{move}").hex
    resolved = event_type in {"billed", "paid", "lost", "complaint"}
    update: dict[str, Any] = {
        "$setOnInsert": {
            "org_id": org_id, "stage": stage, "objection": objection,
            "language": language, "move": move,
        },
        "$set": {"updated_at": now},
        "$inc": {event_type: 1, "resolved": int(resolved)},
    }
    if resolved:
        update["$min"] = {"first_outcome_at": now}
    await _database()[f"sales_learning_profiles_{org_id}"].update_one(
        {"_id": profile_id},
        update,
        upsert=True,
    )


async def record_sales_learning_event(
    *, org_id: str, event_type: str, conversation_id: str,
    source_id: str, case: dict[str, Any] | None = None,
    target_message_id: str = "", channel: str = "", language: str = "",
    value: str = "",
) -> bool:
    if event_type not in EVENT_TYPES or not org_id or not conversation_id or not source_id:
        return False
    await _ensure_indexes(org_id)
    now = datetime.now(timezone.utc)
    case = dict(case or {})
    language = language or "unknown"
    event_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{org_id}:{event_type}:{conversation_id}:{source_id}:{target_message_id}:{value}",
    ).hex
    document = {
        "_id": event_id, "org_id": org_id, "event_type": event_type,
        "conversation_id": conversation_id, "case_id": str(case.get("case_id") or ""),
        "source_id": source_id, "target_message_id": target_message_id,
        "channel": channel[:20], "language": language[:20], "value": value[:80],
        "stage": str(case.get("stage") or "new")[:40],
        "move": str((case.get("last_move") or {}).get("type") or "")[:80],
        "objection": str((case.get("signals") or {}).get("objection") or "none")[:80],
        "buying_signal": str((case.get("signals") or {}).get("buying_signal") or "none")[:80],
        "policy_version": str((case.get("learning_trace") or {}).get("policy_version") or "default")[:80],
        "cohort": str((case.get("learning_trace") or {}).get("cohort") or "control")[:20],
        "created_at": now, "expires_at": now + timedelta(days=365),
    }
    result = await _database()[f"sales_learning_events_{org_id}"].update_one(
        {"_id": event_id}, {"$setOnInsert": document}, upsert=True,
    )
    created = bool(getattr(result, "upserted_id", None))
    if created:
        if event_type in PROFILE_EVENTS and language == "unknown":
            previous = await _database()[f"sales_learning_events_{org_id}"].find_one(
                {"conversation_id": conversation_id, "event_type": "customer_reply"},
                sort=[("created_at", -1)],
            )
            language = str((previous or {}).get("language") or language)
        await _update_profile(org_id, event_type, case, language, now)
    return created


async def record_customer_response(
    org_id: str, conversation_id: str, message_id: str, text: str,
    *, target_message_id: str = "", channel: str = "",
) -> None:
    from services.sales_case_service import get_active_sales_case
    case = await get_active_sales_case(org_id, conversation_id)
    if case:
        await record_sales_learning_event(
            org_id=org_id, event_type="customer_reply", conversation_id=conversation_id,
            source_id=message_id, target_message_id=target_message_id, case=case,
            channel=channel, language=detect_language(text),
        )


async def record_reaction(
    org_id: str, conversation_id: str, provider_event_id: str,
    target_message_id: str, emoji: str, action: str, channel: str,
) -> None:
    from services.sales_case_service import get_active_sales_case
    case = await get_active_sales_case(org_id, conversation_id)
    if case:
        await record_sales_learning_event(
            org_id=org_id,
            event_type="reaction_added" if action == "react" else "reaction_removed",
            conversation_id=conversation_id, source_id=provider_event_id,
            target_message_id=target_message_id, case=case, channel=channel,
            value=emoji or "removed",
        )


async def record_verified_tally_outcome(
    org_id: str, conversation_id: str, source_id: str, outcome: str,
) -> None:
    if outcome not in {"invoice_draft", "billed", "paid"} or not conversation_id:
        return
    from services.sales_case_service import get_active_sales_case, set_sales_action_state
    case = await get_active_sales_case(org_id, conversation_id)
    if not case:
        return
    await record_sales_learning_event(
        org_id=org_id, event_type=outcome, conversation_id=conversation_id,
        source_id=source_id, case=case, channel="tally",
    )
    if outcome in {"billed", "paid"}:
        await set_sales_action_state(
            org_id=org_id, case_id=str(case["case_id"]), source_message_id=source_id,
            action_type="tally_invoice", status="completed",
            fields={"billing_status": outcome}, stage="paid" if outcome == "paid" else "billed",
        )


async def get_sales_guidance(org_id: str, conversation_id: str, sales_context: dict[str, Any], text: str) -> dict[str, Any]:
    language = detect_language(text)
    profiles = await _database()[f"sales_learning_profiles_{org_id}"].find({
        "org_id": org_id,
        "stage": str(sales_context.get("stage") or "new"),
        "objection": str((sales_context.get("signals") or {}).get("objection") or "none"),
        "language": language,
    }).sort("resolved", -1).limit(50).to_list(length=50)
    return choose_guidance(profiles, conversation_id)


async def internal_learning_report(org_id: str) -> dict[str, Any]:
    db = _database()
    rows = await db[f"sales_learning_events_{org_id}"].aggregate([
        {"$group": {"_id": "$event_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]).to_list(length=50)
    profiles = await db[f"sales_learning_profiles_{org_id}"].find(
        {"org_id": org_id}, {"org_id": 0}
    ).sort("resolved", -1).limit(50).to_list(length=50)
    return {"events": {str(row["_id"]): row["count"] for row in rows}, "profiles": profiles}
