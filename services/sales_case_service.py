"""Durable, versioned sales context shared by Math and the inbox."""

from __future__ import annotations

import uuid
import re
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

STAGES = (
    "new", "discovering", "qualified", "evaluating", "selected",
    "negotiating", "commitment_received", "stock_confirmation",
    "quote_sent", "invoice_draft", "billed", "won", "paid",
)
SPECIAL_STAGES = frozenset({"nurture", "on_hold", "lost"})
AI_STAGE_SIGNALS = frozenset({*STAGES[:9], "nurture", "on_hold"})
MOVES = frozenset({
    "ask_discovery", "confirm_understanding", "recommend_shortlist",
    "explain_best_fit", "compare_options", "explain_tradeoff",
    "handle_objection", "offer_alternative", "propose_bundle", "negotiate",
    "ask_micro_commitment", "ask_for_order", "start_quote", "start_checkout",
    "schedule_nurture", "answer_commercial_question", "handoff",
})
ACTIONS = frozenset({
    "none", "show_products", "create_quote", "request_stock_confirmation",
    "create_invoice_draft", "create_handoff", "schedule_followup",
})
QUESTION_FIELDS = frozenset({
    "", "sales_product", "sales_quantity", "sales_timeline", "sales_budget",
    "delivery_pincode",
})
FACT_FIELDS = frozenset({
    "buyer_type", "purchase_type", "primary_use_case", "apps_or_games",
    "current_setup", "pain_point", "priority", "budget", "budget_flexibility",
    "quantity", "timeline", "location", "delivery_pincode", "decision_role",
    "selected_product_id",
})
_INDEXED_ORGS: set[str] = set()


def sales_action_job_id(case_id: str, source_message_id: str, action_type: str) -> str:
    return f"{case_id}:{source_message_id}:{action_type}"


def _norm(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).lower()).split())


def _grounded(field: str, value: Any, evidence: str, message: str) -> bool:
    if field == "selected_product_id":
        return evidence == "conversation_selection"
    evidence_norm = _norm(evidence)
    if not evidence_norm or evidence_norm not in _norm(message):
        return False
    if field == "quantity":
        numbers = [int(item.replace(",", "")) for item in re.findall(r"\b\d[\d,]{0,5}\b", evidence)]
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and float(value).is_integer() and int(value) in numbers and 0 < value <= 9999
        )
    if field == "delivery_pincode":
        return str(value) in re.findall(r"\b[1-9]\d{5}\b", evidence)
    if field == "budget":
        match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lac|l)?", evidence, re.I)
        if not match or isinstance(value, bool):
            return False
        expected = float(match.group(1).replace(",", ""))
        suffix = (match.group(2) or "").lower()
        expected *= 100000 if suffix in {"lakh", "lac", "l"} else 1000 if suffix == "k" else 1
        try:
            actual = float(str(value).replace("₹", "").replace(",", ""))
        except (TypeError, ValueError):
            return False
        return actual == expected
    values = value if isinstance(value, list) else [value]
    return all(_norm(item) in evidence_norm for item in values)


def _database():
    from database import get_async_mongo_db
    return get_async_mongo_db()


def _collection(org_id: str):
    return _database()[f"sales_cases_{org_id}"]


async def _ensure_org_index(org_id: str) -> None:
    if org_id in _INDEXED_ORGS:
        return
    await _collection(org_id).create_index(
        [("conversation_id", 1), ("status", 1)],
        unique=True,
        partialFilterExpression={"status": "active"},
        name="one_active_case_per_conversation",
    )
    _INDEXED_ORGS.add(org_id)


def _plain_value(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 500:
            raise ValueError("sales fact text is empty or too long")
        return value
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, list) and len(value) <= 20:
        return [_plain_value(item) for item in value]
    raise ValueError("sales fact value must be text, a number, or a short list")


def normalize_sales_decision(raw: Any, source_message_text: str = "") -> dict[str, Any]:
    """Fail closed at the service boundary; unknown AI commands are dropped."""
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("unsupported sales decision")
    stage = str(raw.get("stage_signal") or "discovering")
    if stage not in STAGES and stage not in SPECIAL_STAGES:
        raise ValueError("invalid sales stage")
    if stage not in AI_STAGE_SIGNALS:
        stage = "discovering"  # Verified billing/payment outcomes are backend-owned.
    move = dict(raw.get("sales_move") or {})
    move_type = str(move.get("type") or "confirm_understanding")
    if move_type not in MOVES:
        raise ValueError("invalid sales move")
    action_type = str((raw.get("requested_action") or {}).get("type") or "none")
    if action_type not in ACTIONS:
        raise ValueError("invalid requested action")
    question_field = str(raw.get("question_field") or "")
    if question_field not in QUESTION_FIELDS:
        raise ValueError("invalid sales question")
    requested = raw.get("requested_action") if isinstance(raw.get("requested_action"), dict) else {}
    source_message_ids = []
    for value in requested.get("source_message_ids") or []:
        value = str(value).strip()
        if value and len(value) <= 240 and value not in source_message_ids:
            source_message_ids.append(value)
        if len(source_message_ids) == 20:
            break

    updates = []
    for item in raw.get("fact_updates") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        if field not in FACT_FIELDS:
            continue
        try:
            confidence = max(0.0, min(float(item.get("confidence", 0.0)), 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        evidence = str(item.get("evidence") or "customer_message")[:80]
        value = _plain_value(item.get("value"))
        if source_message_text and not _grounded(field, value, evidence, source_message_text):
            continue
        updates.append({
            "field": field,
            "value": value,
            "evidence": evidence,
            "confidence": confidence,
        })
    signals = raw.get("signals") if isinstance(raw.get("signals"), dict) else {}
    return {
        "version": 1,
        "expected_case_version": max(0, int(raw.get("expected_case_version") or 0)),
        "fact_updates": list({item["field"]: item for item in updates}.values()),
        "stage_signal": stage,
        "sales_move": {
            "type": move_type,
            "reason_code": str(move.get("reason_code") or "")[:120],
        },
        "requested_action": {"type": action_type, "source_message_ids": source_message_ids},
        "question_field": question_field,
        # Catalogue ownership is derived from the organisation by the backend.
        "catalogue_org_id": "",
        "learning_trace": {
            "policy_version": str((raw.get("learning_trace") or {}).get("policy_version") or "default")[:80],
            "cohort": str((raw.get("learning_trace") or {}).get("cohort") or "control")[:20],
        } if isinstance(raw.get("learning_trace"), dict) else {},
        "signals": {
            key: str(signals.get(key) or default)[:120]
            for key, default in {
                "buying_signal": "none", "objection": "none",
                "urgency": "none", "decision_role": "unknown",
            }.items()
        },
    }


def _later_stage(current: str, proposed: str) -> str:
    if current in {"won", "paid"}:
        return "paid" if proposed == "paid" else current
    if current == "lost":
        return current
    if proposed in SPECIAL_STAGES:
        return proposed
    if current in SPECIAL_STAGES:
        return proposed
    try:
        return STAGES[max(STAGES.index(current), STAGES.index(proposed))]
    except ValueError:
        return proposed


def sales_context(case: dict[str, Any] | None) -> dict[str, Any]:
    if not case:
        return {"version": 0, "stage": "new", "facts": {}, "signals": {}, "last_move": {}, "next_action": "none"}
    return {
        "version": int(case.get("version") or 0) if str(case.get("version") or 0).isdigit() else 0,
        "stage": case.get("stage") or "new",
        "facts": {
            key: value.get("value") if isinstance(value, dict) and "value" in value else value
            for key, value in (case.get("facts") or {}).items()
        },
        "signals": dict(case.get("signals") or {}),
        "last_move": dict(case.get("last_move") or {}),
        "next_action": case.get("next_action") or "none",
        "question_field": case.get("question_field") or "",
        "catalogue_org_id": case.get("catalogue_org_id") or "",
        "action_status": case.get("action_status") or "idle",
        "quote_id": case.get("quote_id"),
        "invoice_draft_id": case.get("invoice_draft_id"),
    }


async def ensure_sales_case_indexes() -> None:
    db = _database()
    for name in await db.list_collection_names(filter={"name": {"$regex": "^sales_cases_"}}):
        await _ensure_org_index(name.removeprefix("sales_cases_"))


async def get_active_sales_case(org_id: str, conversation_id: str) -> dict[str, Any] | None:
    return await _collection(org_id).find_one({
        "org_id": org_id, "conversation_id": conversation_id, "status": "active",
    })


async def get_sales_context(org_id: str, conversation_id: str) -> dict[str, Any]:
    return sales_context(await get_active_sales_case(org_id, conversation_id))


async def apply_sales_decision(
    *, org_id: str, conversation_id: str, source_message_id: str,
    customer_name: str, decision: Any, source_message_text: str = "",
) -> dict[str, Any]:
    checked = normalize_sales_decision(decision, source_message_text)
    await _ensure_org_index(org_id)
    collection = _collection(org_id)
    now = datetime.now(timezone.utc)
    case = await get_active_sales_case(org_id, conversation_id)
    if not case:
        case = {
            "_id": str(uuid.uuid4()), "case_id": str(uuid.uuid4()),
            "org_id": org_id, "conversation_id": conversation_id,
            "customer_name": customer_name, "status": "active", "stage": "new",
            "version": 0, "facts": {}, "signals": {}, "last_move": {},
            "catalogue_org_id": "", "question_field": "",
            "next_action": "none", "processed_events": [], "action_receipts": [],
            "stage_history": [],
            "created_at": now, "updated_at": now,
        }
        try:
            await collection.insert_one(case)
        except DuplicateKeyError:
            case = await get_active_sales_case(org_id, conversation_id)

    if case.get("workflow_version") == 2:
        # V2 requirements and actions are owned by the version-checked workflow.
        return {"case": case, "duplicate": False, "conflict": True, "rebased": False}

    expected = checked["expected_case_version"]
    rebased = False
    for _ in range(3):
        if source_message_id in (case.get("processed_events") or []):
            receipt = next((
                item for item in reversed(case.get("action_receipts") or [])
                if isinstance(item, dict)
                and item.get("source_message_id") == source_message_id
            ), None)
            return {
                "case": case, "duplicate": True, "conflict": False,
                "rebased": rebased, "action_receipt": receipt,
            }
        current_version = int(case.get("version") or 0)
        rebased = rebased or expected != current_version
        next_stage = _later_stage(str(case.get("stage") or "new"), checked["stage_signal"])
        signals = dict(case.get("signals") or {})
        signals.update(checked["signals"])
        facts_snapshot = dict(case.get("facts") or {})
        action_type = checked["requested_action"]["type"]
        action_receipt = None
        updates: dict[str, Any] = {
            "stage": next_stage, "signals": signals,
            "last_move": checked["sales_move"], "next_action": action_type,
            "question_field": checked["question_field"],
            "customer_name": customer_name or case.get("customer_name") or "",
            "updated_at": now,
        }
        if action_type not in {"none", "show_products"}:
            action_receipt = {
                "source_message_id": source_message_id,
                "action_type": action_type,
                "job_id": sales_action_job_id(
                    str(case["case_id"]), source_message_id, action_type
                ),
            }
            updates.update({
                "action_status": "pending", "action_error": "",
                "action_type": action_type,
                "action_source_message_id": source_message_id,
                "action_source_message_ids": checked["requested_action"]["source_message_ids"],
            })
        if checked["learning_trace"]:
            updates["learning_trace"] = checked["learning_trace"]
        for item in checked["fact_updates"]:
            fact = {
                "value": item["value"], "evidence": item["evidence"],
                "confidence": item["confidence"],
                "source_message_id": source_message_id, "updated_at": now,
            }
            updates[f"facts.{item['field']}"] = fact
            facts_snapshot[item["field"]] = fact
        if action_receipt:
            action_receipt["case_snapshot"] = {
                "case_id": str(case["case_id"]),
                "org_id": org_id,
                "conversation_id": conversation_id,
                "customer_name": customer_name or case.get("customer_name") or "",
                "facts": facts_snapshot,
                "action_source_message_ids": list(
                    checked["requested_action"]["source_message_ids"]
                ),
                "quote_id": case.get("quote_id"),
            }
        pushed = {
            "processed_events": {"$each": [source_message_id], "$slice": -200},
            "stage_history": {"$each": [{
                "from": case.get("stage") or "new", "to": next_stage,
                "source_message_id": source_message_id,
                "move": checked["sales_move"]["type"], "at": now,
            }], "$slice": -200},
        }
        if action_receipt:
            pushed["action_receipts"] = {
                "$each": [action_receipt], "$slice": -200,
            }
        updated = await collection.find_one_and_update(
            {
                "_id": case["_id"], "version": current_version, "status": "active", "workflow_version": {"$ne": 2},
                "processed_events": {"$ne": source_message_id},
            },
            {
                "$set": updates, "$inc": {"version": 1},
                "$push": pushed,
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return {
                "case": updated, "duplicate": False, "conflict": False,
                "rebased": rebased, "action_receipt": action_receipt,
            }
        case = await get_active_sales_case(org_id, conversation_id) or case
    return {"case": case, "duplicate": False, "conflict": True, "rebased": rebased}


async def set_sales_action_state(
    *, org_id: str, case_id: str, source_message_id: str, action_type: str,
    status: str, error: str = "", fields: dict[str, Any] | None = None,
    stage: str | None = None,
) -> dict[str, Any] | None:
    """Record a real action result without letting AI claim it happened."""
    collection = _collection(org_id)
    case = await collection.find_one({"case_id": case_id, "status": "active"})
    if not case:
        return None
    updates = {
        "next_action": "none" if status == "completed" else action_type,
        "action_status": status,
        "action_error": error[:300], "action_source_message_id": source_message_id,
        "updated_at": datetime.now(timezone.utc), **(fields or {}),
    }
    if stage:
        updates["stage"] = _later_stage(str(case.get("stage") or "new"), stage)
    return await collection.find_one_and_update(
        {
            "case_id": case_id,
            "status": "active",
            "action_source_message_id": source_message_id,
            "$or": [
                {"action_type": action_type},
                {"action_type": {"$exists": False}},
            ],
        },
        {"$set": updates},
        return_document=ReturnDocument.AFTER,
    )


async def set_verified_sales_outcome(
    org_id: str, case_id: str, outcome: str,
) -> dict[str, Any] | None:
    """Apply a backend-verified billing outcome without changing action ownership."""
    if outcome not in {"billed", "paid"}:
        raise ValueError("invalid verified sales outcome")
    collection = _collection(org_id)
    for _ in range(3):
        case = await collection.find_one({"case_id": case_id, "status": "active"})
        if not case:
            return None
        verified_outcome = (
            "paid" if outcome == "paid" or case.get("billing_status") == "paid"
            else "billed"
        )
        updated = await collection.find_one_and_update(
            {
                "case_id": case_id,
                "status": "active",
                "stage": case.get("stage"),
                "billing_status": case.get("billing_status"),
            },
            {"$set": {
                "billing_status": verified_outcome,
                "stage": _later_stage(
                    str(case.get("stage") or "new"), verified_outcome
                ),
                "updated_at": datetime.now(timezone.utc),
            }},
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            return updated
    raise RuntimeError("verified sales outcome changed repeatedly")
