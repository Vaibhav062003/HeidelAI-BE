"""Private, derived sales learning signals. Raw conversations stay in message collections."""

from __future__ import annotations

import asyncio
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
RESOLVED_EVENTS = frozenset({"billed", "paid", "lost", "complaint"})
MIN_RESOLVED = 30
MIN_EVIDENCE_DAYS = 7
MAX_COMPLAINT_RATE = 0.2
NEGATIVE_REACTIONS = ("👎", "😠", "😡")
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


def _policy_summary(profile: dict[str, Any], approved: bool, now: datetime) -> dict[str, Any]:
    resolved = int(profile.get("resolved") or 0)
    first = profile.get("first_outcome_at")
    old_enough = isinstance(first, datetime) and first <= now - timedelta(days=MIN_EVIDENCE_DAYS)
    complaint_rate = float(profile.get("complaint") or 0) / max(1, resolved)
    eligible = resolved >= MIN_RESOLVED and old_enough and complaint_rate <= MAX_COMPLAINT_RATE
    rollout = 10 if resolved < 120 else 50 if resolved < 240 else 100
    if approved and complaint_rate > MAX_COMPLAINT_RATE:
        status = "AUTO_STOPPED"
    elif approved and eligible:
        status = "ACTIVE" if rollout == 100 else f"TESTING_{rollout}"
    elif eligible:
        status = "READY_FOR_APPROVAL"
    else:
        status = "COLLECTING"
    return {
        "policy_id": str(profile.get("_id") or "")[:80],
        "stage": str(profile.get("stage") or "new")[:40],
        "objection": str(profile.get("objection") or "none")[:80],
        "language": str(profile.get("language") or "unknown")[:20],
        "move": str(profile.get("move") or "unknown")[:80],
        "resolved": resolved,
        "billed": int(profile.get("billed") or 0),
        "paid": int(profile.get("paid") or 0),
        "lost": int(profile.get("lost") or 0),
        "complaint": int(profile.get("complaint") or 0),
        "complaint_rate": round(complaint_rate, 4),
        "first_outcome_at": first,
        "updated_at": profile.get("updated_at"),
        "rollout_percent": rollout,
        "eligible": eligible,
        "approved": approved,
        "status": status,
    }


def _best_policy(
    profiles: list[dict[str, Any]], now: datetime,
    approved_policy_ids: set[str] | frozenset[str],
) -> dict[str, Any] | None:
    eligible = [
        profile for profile in profiles
        if str(profile.get("_id") or "") in approved_policy_ids
        and _policy_summary(profile, False, now)["eligible"]
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda item: (
        float(item.get("paid") or 0) / max(1, int(item.get("resolved") or 0)),
        float(item.get("billed") or 0) / max(1, int(item.get("resolved") or 0)),
    ), reverse=True)
    return eligible[0]


def choose_guidance(
    profiles: list[dict[str, Any]], conversation_id: str, now: datetime | None = None,
    approved_policy_ids: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    best = _best_policy(profiles, now, approved_policy_ids)
    if not best:
        return {"policy_version": "default", "cohort": "control", "recommended_moves": []}
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
        if action == "react" and emoji.startswith(NEGATIVE_REACTIONS):
            await record_sales_learning_event(
                org_id=org_id, event_type="complaint", conversation_id=conversation_id,
                source_id=provider_event_id, target_message_id=target_message_id,
                case=case, channel=channel, value=emoji,
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
    policy_ids = [str(profile.get("_id") or "") for profile in profiles]
    approvals = await _database()[f"sales_learning_policy_approvals_{org_id}"].find({
        "_id": {"$in": policy_ids}, "org_id": org_id, "status": "APPROVED",
    }, {"_id": 1}).to_list(length=50)
    return choose_guidance(profiles, conversation_id, approved_policy_ids={str(row["_id"]) for row in approvals})


async def approve_sales_learning_policy(org_id: str, policy_id: str, actor_user_id: str) -> dict[str, Any]:
    db = _database()
    organization = await db["organizations"].find_one(
        {"org_id": org_id}, {"owner.clerk_user_id": 1},
    )
    if not organization:
        raise LookupError("Organization was not found")
    owner_user_id = str((organization.get("owner") or {}).get("clerk_user_id") or "")
    if not owner_user_id or actor_user_id != owner_user_id:
        raise PermissionError("Only the organization owner can approve sales learning policies")
    approvals = db[f"sales_learning_policy_approvals_{org_id}"]
    existing = await approvals.find_one({"_id": policy_id, "org_id": org_id, "status": "APPROVED"})
    if existing:
        return existing
    profile = await db[f"sales_learning_profiles_{org_id}"].find_one({"_id": policy_id, "org_id": org_id})
    if not profile:
        raise LookupError("Sales learning policy was not found")
    now = datetime.now(timezone.utc)
    evidence = _policy_summary(profile, False, now)
    if not evidence["eligible"]:
        raise ValueError("Sales learning policy is not ready for approval")
    document = {
        "_id": policy_id, "org_id": org_id, "status": "APPROVED",
        "approved_by": owner_user_id[:120], "approved_at": now,
        "evidence": {
            "resolved": evidence["resolved"], "billed": evidence["billed"],
            "paid": evidence["paid"], "complaint": evidence["complaint"],
            "complaint_rate": evidence["complaint_rate"],
        },
    }
    result = await approvals.update_one({"_id": policy_id}, {"$setOnInsert": document}, upsert=True)
    if getattr(result, "upserted_id", None):
        return document
    return await approvals.find_one({"_id": policy_id, "org_id": org_id, "status": "APPROVED"}) or document


async def internal_learning_report(org_id: str) -> dict[str, Any]:
    from services.entitlement_service import require_feature
    require_feature(org_id, "AI", strict=True)
    db = _database()
    events = db[f"sales_learning_events_{org_id}"]
    event_rows, conversation_rows, cohort_rows, profiles, approvals = await asyncio.gather(
        events.aggregate([
        {"$group": {"_id": "$event_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        ]).to_list(length=50),
        events.aggregate([
            {"$group": {"_id": "$conversation_id", "types": {"$addToSet": "$event_type"}, "last_event_at": {"$max": "$created_at"}}},
            {"$group": {
                "_id": None, "unique_conversations": {"$sum": 1}, "last_event_at": {"$max": "$last_event_at"},
                **{name: {"$sum": {"$cond": [{"$in": [name, "$types"]}, 1, 0]}} for name in (
                    "customer_reply", "commitment", "invoice_draft", "billed", "paid", "lost", "complaint", "handoff",
                )},
                "resolved": {"$sum": {"$cond": [{"$gt": [{"$size": {"$setIntersection": ["$types", list(RESOLVED_EVENTS)]}}, 0]}, 1, 0]}},
            }},
        ]).to_list(length=1),
        events.aggregate([
            {"$match": {"policy_version": {"$ne": "default"}, "cohort": {"$in": ["test", "control"]}}},
            {"$group": {"_id": {"policy_id": "$policy_version", "cohort": "$cohort", "conversation_id": "$conversation_id"}, "types": {"$addToSet": "$event_type"}}},
            {"$group": {
                "_id": {"policy_id": "$_id.policy_id", "cohort": "$_id.cohort"},
                "conversations": {"$sum": 1},
                **{name: {"$sum": {"$cond": [{"$in": [name, "$types"]}, 1, 0]}} for name in ("billed", "paid", "lost", "complaint")},
                "resolved": {"$sum": {"$cond": [{"$gt": [{"$size": {"$setIntersection": ["$types", list(RESOLVED_EVENTS)]}}, 0]}, 1, 0]}},
            }},
            {"$sort": {"_id.policy_id": 1, "_id.cohort": 1}},
        ]).to_list(length=200),
        db[f"sales_learning_profiles_{org_id}"].find({"org_id": org_id}).sort("resolved", -1).limit(100).to_list(length=100),
        db[f"sales_learning_policy_approvals_{org_id}"].find({"org_id": org_id, "status": "APPROVED"}, {"_id": 1, "approved_at": 1, "approved_by": 1}).to_list(length=100),
    )
    approved = {str(row["_id"]): row for row in approvals}
    now = datetime.now(timezone.utc)
    policies = [_policy_summary(profile, str(profile.get("_id")) in approved, now) for profile in profiles]
    for policy in policies:
        approval = approved.get(policy["policy_id"])
        if approval:
            policy["approved_at"] = approval.get("approved_at")
            policy["approved_by"] = approval.get("approved_by")
    totals = conversation_rows[0] if conversation_rows else {}
    unique = int(totals.get("unique_conversations") or 0)
    resolved = int(totals.get("resolved") or 0)
    if any(policy["status"] == "AUTO_STOPPED" for policy in policies):
        status = "AUTO_STOPPED"
    elif any(policy["status"] == "READY_FOR_APPROVAL" for policy in policies):
        status = "READY_FOR_APPROVAL"
    elif any(policy["status"] == "ACTIVE" for policy in policies):
        status = "ACTIVE"
    elif any(policy["status"] == "TESTING_50" for policy in policies):
        status = "TESTING_50"
    elif any(policy["status"] == "TESTING_10" for policy in policies):
        status = "TESTING_10"
    else:
        status = "COLLECTING" if unique else "NO_DATA"
    cohorts = [{
        "policy_id": str(row["_id"]["policy_id"]), "cohort": str(row["_id"]["cohort"]),
        **{key: int(row.get(key) or 0) for key in ("conversations", "resolved", "billed", "paid", "lost", "complaint")},
    } for row in cohort_rows]
    by_policy: dict[str, set[str]] = {}
    for row in cohorts:
        if row["resolved"] >= 10:
            by_policy.setdefault(row["policy_id"], set()).add(row["cohort"])
    return {
        "org_id": org_id, "status": status,
        "last_event_at": totals.get("last_event_at"),
        "unique_conversations": unique,
        "events": {str(row["_id"]): int(row["count"]) for row in event_rows},
        "funnel": {key: int(totals.get(key) or 0) for key in ("customer_reply", "commitment", "invoice_draft", "billed", "paid")},
        "outcomes": {key: int(totals.get(key) or 0) for key in ("resolved", "lost", "complaint", "handoff")},
        "rates": {
            "reply": round(int(totals.get("customer_reply") or 0) / max(1, unique), 4),
            "billed": round(int(totals.get("billed") or 0) / max(1, unique), 4),
            "paid": round(int(totals.get("paid") or 0) / max(1, unique), 4),
            "complaint": round(int(totals.get("complaint") or 0) / max(1, resolved), 4),
        },
        "policies": policies,
        "cohorts": cohorts,
        "cohort_comparison_ready": sorted(policy_id for policy_id, values in by_policy.items() if values == {"test", "control"}),
    }


async def internal_learning_reports(org_ids: list[str]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(10)

    async def report(org_id: str) -> dict[str, Any]:
        async with semaphore:
            return await internal_learning_report(org_id)

    return list(await asyncio.gather(*(report(org_id) for org_id in org_ids)))
