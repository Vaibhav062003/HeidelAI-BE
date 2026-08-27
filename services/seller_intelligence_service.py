"""Role-scoped seller command centre built from HeidelAI's existing source-of-truth records."""

from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable

from fastapi import HTTPException

from database import get_async_mongo_db
from services.utils import serialize_mongo


SECTORS = (
    ("close_now", "Close now"),
    ("follow_ups", "Follow-ups"),
    ("price_stock", "Price & stock"),
    ("money", "Money"),
    ("team", "Team"),
    ("watchlist", "Watchlist"),
)
ADMIN_ROLES = {"admin", "org:admin", "owner", "org:owner"}
MANAGER_ROLES = {"manager", "org:manager"}


async def ensure_seller_intelligence_indexes() -> None:
    db = get_async_mongo_db()
    await db.seller_intelligence_decisions.create_index(
        [("org_id", 1), ("user_id", 1), ("item_id", 1)], unique=True,
    )
    await db.seller_intelligence_preferences.create_index(
        [("org_id", 1), ("user_id", 1)], unique=True,
    )


def _utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _freshness(state: str, at: datetime | None = None, message: str = "") -> dict:
    return {"state": state, "updated_at": at, "message": message}


def _amount_points(amount: float | None) -> int:
    if amount is None or amount <= 0:
        return 0
    if amount >= 500_000:
        return 25
    if amount >= 100_000:
        return 20
    if amount >= 50_000:
        return 15
    if amount >= 10_000:
        return 10
    return 5


def score_action(signal: dict[str, Any]) -> tuple[int, dict[str, int], list[dict]]:
    """Deterministic 100-point score; AI evidence is ignored below 65% confidence."""
    reasons: list[dict] = []
    factors = {"urgency": 0, "revenue_exposure": 0, "buying_intent": 0, "waiting_risk": 0, "operational_risk": 0}

    def add(factor: str, points: int, code: str, label: str, source: str) -> None:
        if points <= 0:
            return
        cap = {"urgency": 30, "revenue_exposure": 25, "buying_intent": 20, "waiting_risk": 15, "operational_risk": 10}[factor]
        awarded = min(points, cap - factors[factor])
        if awarded > 0:
            factors[factor] += awarded
            reasons.append({"factor": factor, "code": code, "label": label, "points": awarded, "source": source})

    due_minutes = signal.get("followup_due_minutes")
    if isinstance(due_minutes, (int, float)):
        if due_minutes < 0:
            add("urgency", 22, "followup_overdue", "Follow-up is overdue", "follow_ups")
        elif due_minutes <= 120:
            add("urgency", 16, "followup_due_soon", "Follow-up is due within 2 hours", "follow_ups")
        elif due_minutes <= 1_440:
            add("urgency", 9, "followup_due_today", "Follow-up is due today", "follow_ups")

    reply_minutes = signal.get("reply_window_minutes")
    if isinstance(reply_minutes, (int, float)) and reply_minutes >= 0:
        if reply_minutes <= 120:
            add("urgency", 15, "reply_window_critical", "Reply window closes within 2 hours", "conversations")
        elif reply_minutes <= 300:
            add("urgency", 8, "reply_window_expiring", "Reply window closes within 5 hours", "conversations")

    amount = _number(signal.get("amount"))
    add("revenue_exposure", _amount_points(amount), "value_at_risk", "Commercial value is at risk", signal.get("amount_source") or "offers")

    if signal.get("priority") in {"high", "urgent"}:
        add("buying_intent", 12, "high_priority", "Customer is marked high intent", "conversations")
    confidence = _number(signal.get("ai_confidence"))
    if confidence is not None and confidence >= 0.65:
        stage = str(signal.get("stage") or "")
        if stage in {"selected", "negotiating", "commitment_received", "stock_confirmation", "quote_sent", "payment_pending"}:
            add("buying_intent", 15, "advanced_sales_stage", "AI-confirmed buying stage", "sales_case")
        elif stage in {"qualified", "evaluating"}:
            add("buying_intent", 8, "qualified_sales_stage", "AI-confirmed qualified opportunity", "sales_case")

    if signal.get("needs_reply"):
        add("waiting_risk", 9, "customer_waiting", "Customer is waiting for a reply", "conversations")
    if (_number(signal.get("age_hours")) or 0) >= 24:
        add("waiting_risk", 5, "conversation_aging", "Conversation has been waiting over 24 hours", "conversations")
    if (_number(signal.get("unread_count")) or 0) > 0:
        add("waiting_risk", 3, "unread_message", "Customer message is unread", "conversations")

    if signal.get("operational_issue"):
        add("operational_risk", 10, str(signal.get("operational_code") or "operational_issue"), str(signal["operational_issue"]), str(signal.get("operational_source") or "operations"))

    return sum(factors.values()), factors, reasons


def _fingerprint(action: dict) -> str:
    material = "|".join([
        str(action.get("id") or ""), str(action.get("score") or 0), str(action.get("updated_at") or ""),
        ",".join(reason["code"] for reason in action.get("reasons") or []),
    ])
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _severity(score: int) -> str:
    return "critical" if score >= 70 else "high" if score >= 50 else "medium" if score >= 30 else "low"


async def _capture(awaitable: Awaitable[Any]) -> tuple[Any, str | None]:
    try:
        return await awaitable, None
    except Exception as exc:  # One broken source must not blank the command centre.
        return [], type(exc).__name__


async def _scope(org_id: str, user_id: str, role: str, requested: str) -> tuple[str, dict, set[str]]:
    db = get_async_mongo_db()
    if role in ADMIN_ROLES:
        scope = requested if requested in {"organization", "team", "mine"} else "organization"
    elif role in MANAGER_ROLES:
        scope = requested if requested in {"team", "mine"} else "team"
    else:
        scope = "mine"
    if scope == "organization":
        return scope, {}, set()
    if scope == "mine":
        return scope, {"$or": [{"assigned_agent.id": user_id}, {"assigned_agent_id": user_id}]}, {user_id}

    teams = await db[f"work_teams_{org_id}"].find(
        {"member_ids": user_id}, {"_id": 0, "team_id": 1, "member_ids": 1},
    ).to_list(length=100)
    team_ids = [team.get("team_id") for team in teams if team.get("team_id")]
    member_ids = {member for team in teams for member in team.get("member_ids") or []}
    if not team_ids:
        return "mine", {"$or": [{"assigned_agent.id": user_id}, {"assigned_agent_id": user_id}]}, {user_id}
    return scope, {"$or": [
        {"assigned_agent.id": {"$in": list(member_ids)}},
        {"assigned_agent_id": {"$in": list(member_ids)}},
        {"active_work.team_id": {"$in": team_ids}},
    ]}, member_ids


def _action(
    *, action_id: str, sector: str, title: str, summary: str, signal: dict,
    customer_name: str = "", conversation_id: str = "", updated_at: datetime | None = None,
    metrics: dict | None = None, target_view: str = "inbox", target_label: str = "Open",
    feature: str | None = None, requires_confirmation: bool = False,
) -> dict:
    score, breakdown, reasons = score_action(signal)
    item = {
        "id": action_id, "sector": sector, "title": title, "summary": summary,
        "customer_name": customer_name, "conversation_id": conversation_id,
        "score": score, "severity": _severity(score), "score_breakdown": breakdown,
        "reasons": reasons, "metrics": metrics or {}, "updated_at": updated_at,
        "recommended_action": {
            "label": target_label, "target_view": target_view, "feature": feature,
            "requires_confirmation": requires_confirmation,
        },
        "state": "new",
    }
    item["fingerprint"] = _fingerprint(item)
    return item


async def get_preferences(org_id: str, user_id: str, role: str) -> dict:
    row = await get_async_mongo_db().seller_intelligence_preferences.find_one(
        {"org_id": org_id, "user_id": user_id}, {"_id": 0},
    ) or {}
    default_scope = "organization" if role in ADMIN_ROLES else "team" if role in MANAGER_ROLES else "mine"
    return {
        "default_scope": row.get("default_scope") or default_scope,
        "minimum_score": int(row.get("minimum_score") or 0),
        "show_acknowledged": bool(row.get("show_acknowledged", True)),
    }


async def save_preferences(org_id: str, user_id: str, role: str, values: dict) -> dict:
    allowed_scopes = {"organization", "team", "mine"} if role in ADMIN_ROLES else {"team", "mine"} if role in MANAGER_ROLES else {"mine"}
    if values.get("default_scope") not in allowed_scopes:
        raise HTTPException(status_code=422, detail="That scope is not available for your role")
    document = {
        "default_scope": values["default_scope"],
        "minimum_score": int(values.get("minimum_score") or 0),
        "show_acknowledged": bool(values.get("show_acknowledged", True)),
        "updated_at": datetime.now(timezone.utc),
    }
    await get_async_mongo_db().seller_intelligence_preferences.update_one(
        {"org_id": org_id, "user_id": user_id}, {"$set": document}, upsert=True,
    )
    return await get_preferences(org_id, user_id, role)


async def build_snapshot(
    org_id: str, user_id: str, role: str, requested_scope: str = "", *,
    limit: int = 60, cursor: str = "0", include_suppressed: bool = False,
) -> dict:
    db = get_async_mongo_db()
    now = datetime.now(timezone.utc)
    preferences = await get_preferences(org_id, user_id, role)
    scope_name, conversation_scope, _ = await _scope(
        org_id, user_id, role, requested_scope or preferences["default_scope"],
    )
    conversation_query: dict = {"type": {"$ne": "team"}, "status": {"$in": ["open", "follow-up", "waiting", None]}}
    if conversation_scope:
        conversation_query = {"$and": [conversation_query, conversation_scope]}
    conversations, conversation_error = await _capture(
        db[f"conversations_{org_id}"].find(conversation_query, {
            "_id": 0, "conversation_id": 1, "customer_name": 1, "customer_id": 1,
            "last_message": 1, "last_message_timestamp": 1, "created_at": 1, "status": 1,
            "priority": 1, "unread_count": 1, "needs_reply": 1, "last_sender": 1,
            "is_ai_enabled": 1, "reply_window_ends_at": 1, "assigned_agent": 1,
            "assigned_agent_id": 1, "active_work": 1,
        }).sort("last_message_timestamp", -1).limit(500).to_list(length=500)
    )
    conversation_ids = [row.get("conversation_id") for row in conversations if row.get("conversation_id")]
    related = {"conversation_id": {"$in": conversation_ids}} if conversation_ids else {"_id": {"$exists": False}}

    cases_result, offers_result, followups_result, products_result, work_result = await asyncio.gather(
        _capture(db[f"sales_cases_{org_id}"].find({**related, "status": "active"}, {"_id": 0}).to_list(length=500)),
        _capture(db[f"sales_offers_{org_id}"].find({**related, "status": {"$in": ["open", "accepted"]}, "delivery_status": {"$ne": "failed"}}, {"org_id": 0}).sort("created_at", -1).to_list(length=500)),
        _capture(db.scheduled_messages.find({"org_id": org_id, **related, "status": {"$in": ["pending", "failed", "delivery_unknown"]}}, {"org_id": 0}).sort("scheduled_time", 1).to_list(length=500)),
        _capture(db[f"products_{org_id}"].find({}, {"_id": 1, "product_name": 1, "model_name": 1, "model_no": 1, "sku": 1, "unique_identifier": 1, "price": 1, "stock": 1, "quantity": 1, "in_stock": 1, "updated_at": 1, "source": 1}).limit(5_000).to_list(length=5_000)),
        _capture(db[f"conversation_work_{org_id}"].find({**related, "status": {"$in": ["pending", "in_progress", "needs_review"]}}, {"_id": 0}).to_list(length=500)),
    )
    cases, case_error = cases_result
    offers, offer_error = offers_result
    followups, followup_error = followups_result
    products, product_error = products_result
    work_items, work_error = work_result

    tally_integration, tally_error = await _capture(db.tally_integrations.find_one({"org_id": org_id}, {"_id": 0}))
    tally_integration = tally_integration if isinstance(tally_integration, dict) else None
    tally_seen = _utc((tally_integration or {}).get("last_seen_at"))
    tally_sync = _utc((tally_integration or {}).get("last_sync_at"))
    tally_connected = bool(tally_seen and tally_seen >= now - timedelta(seconds=90))
    tally_rows, tally_stock_error = await _capture(
        db.tally_stock_items.find({"org_id": org_id}, {"_id": 0, "master_id": 1, "name": 1, "model_number": 1, "closing_quantity": 1, "standard_rate": 1, "synced_at": 1}).limit(5_000).to_list(length=5_000)
    ) if tally_integration else ([], None)

    invoice_query: dict = {
        "org_id": org_id, "transaction_type": {"$ne": "credit_note"},
        "lifecycle_status": {"$nin": ["cancelled", "deleted", "optional", "post_dated"]},
        "payment_status": {"$in": ["outstanding", "overdue"]},
    }
    if scope_name != "organization":
        invoice_query["billing_contact.conversation_id"] = {"$in": conversation_ids}
    invoices, invoice_error = await _capture(
        db.tally_invoices.find(invoice_query, {"_id": 0, "invoice_id": 1, "voucher_number": 1, "party_ledger": 1, "outstanding_amount": 1, "due_date": 1, "payment_status": 1, "billing_contact": 1, "synced_at": 1}).sort("due_date", 1).limit(300).to_list(length=300)
    ) if tally_integration else ([], None)

    cases_by_conversation = {row.get("conversation_id"): row for row in cases}
    offers_by_conversation: dict[str, dict] = {}
    for row in offers:
        offers_by_conversation.setdefault(str(row.get("conversation_id") or ""), row)
    followups_by_conversation: dict[str, dict] = {}
    for row in followups:
        followups_by_conversation.setdefault(str(row.get("conversation_id") or ""), row)
    work_by_conversation: dict[str, dict] = {}
    for row in work_items:
        work_by_conversation.setdefault(str(row.get("conversation_id") or ""), row)

    actions: list[dict] = []
    for row in conversations:
        conversation_id = str(row.get("conversation_id") or "")
        last_at = _utc(row.get("last_message_timestamp") or row.get("created_at"))
        case = cases_by_conversation.get(conversation_id) or {}
        offer = offers_by_conversation.get(conversation_id) or {}
        followup = followups_by_conversation.get(conversation_id) or {}
        work = work_by_conversation.get(conversation_id) or {}
        followup_at = _utc(followup.get("scheduled_time"))
        reply_at = _utc(row.get("reply_window_ends_at"))
        fact_confidences = [
            _number(value.get("confidence")) for value in (case.get("facts") or {}).values()
            if isinstance(value, dict) and _number(value.get("confidence")) is not None
        ]
        ai_confidence = _number(case.get("ai_confidence") or case.get("confidence"))
        if ai_confidence is None and fact_confidences:
            ai_confidence = max(fact_confidences)
        needs_reply = bool(row.get("needs_reply")) or (row.get("last_sender") == "customer" and row.get("is_ai_enabled") is not True)
        signal = {
            "followup_due_minutes": (followup_at - now).total_seconds() / 60 if followup_at else None,
            "reply_window_minutes": (reply_at - now).total_seconds() / 60 if reply_at else None,
            "amount": offer.get("total_amount"), "amount_source": "offers",
            "priority": row.get("priority"), "stage": case.get("stage"), "ai_confidence": ai_confidence,
            "needs_reply": needs_reply, "unread_count": row.get("unread_count"),
            "age_hours": (now - last_at).total_seconds() / 3600 if last_at else None,
            "operational_issue": "Team review is waiting" if work.get("status") == "needs_review" else None,
            "operational_code": "team_review_waiting", "operational_source": "team",
        }
        is_unassigned = not row.get("assigned_agent_id") and not (row.get("assigned_agent") or {}).get("id")
        if is_unassigned and scope_name == "organization":
            sector, title, target_label = "team", "Assign an owner", "Open & assign"
        elif followup_at and followup_at <= now + timedelta(days=1):
            sector, title, target_label = "follow_ups", "Complete the scheduled follow-up", "Open follow-up"
        elif offer or row.get("priority") in {"high", "urgent"} or case.get("stage") in {"selected", "negotiating", "commitment_received", "quote_sent", "payment_pending"}:
            sector, title, target_label = "close_now", "Move this opportunity forward", "Open conversation"
        else:
            sector, title, target_label = "watchlist", "Review this conversation", "Open conversation"
        relevant = needs_reply or offer or followup_at or work or row.get("priority") in {"high", "urgent"} or ((_number(signal["age_hours"]) or 0) >= 24)
        if not relevant:
            continue
        customer = str(row.get("customer_name") or "Customer")
        summary = str(row.get("last_message") or offer.get("message_preview") or "Open conversation")[:240]
        actions.append(_action(
            action_id=f"conversation:{conversation_id}", sector=sector, title=title, summary=summary,
            signal=signal, customer_name=customer, conversation_id=conversation_id, updated_at=last_at,
            metrics={"amount": _number(offer.get("total_amount")), "due_at": followup_at, "reply_window_ends_at": reply_at},
            target_view="follow-ups" if sector == "follow_ups" else "inbox", target_label=target_label,
            feature="follow_ups" if sector == "follow_ups" else None,
        ))

    stock_rows: list[tuple[dict, str]] = [(row, "inventory") for row in products]
    if tally_rows:
        stock_rows = [(row, "tally") for row in tally_rows]
    for row, source in stock_rows:
        quantity = _number(row.get("closing_quantity") if source == "tally" else row.get("stock", row.get("quantity")))
        if quantity is None and isinstance(row.get("in_stock"), bool):
            quantity = 1 if row["in_stock"] else 0
        if quantity is None or quantity > 1:
            continue
        name = str(row.get("model_number") or row.get("name") or row.get("product_name") or "Stock item")
        action_id = f"stock:{row.get('master_id') or row.get('_id') or name}"
        actions.append(_action(
            action_id=action_id, sector="price_stock", title="Restock or confirm availability",
            summary=f"{name} has {quantity:g} remaining.",
            signal={"operational_issue": "Stock is depleted" if quantity <= 0 else "Stock is critically low", "operational_code": "out_of_stock" if quantity <= 0 else "low_stock", "operational_source": source},
            updated_at=_utc(row.get("synced_at") or row.get("updated_at")), metrics={"quantity": quantity},
            target_view="stock", target_label="Review stock", feature="tally",
        ))

    for row in invoices:
        amount = _number(row.get("outstanding_amount"))
        due_date = str(row.get("due_date") or "")
        overdue = row.get("payment_status") == "overdue" or bool(due_date and due_date < now.date().isoformat())
        conversation_id = str((row.get("billing_contact") or {}).get("conversation_id") or "")
        actions.append(_action(
            action_id=f"invoice:{row.get('invoice_id') or row.get('voucher_number')}", sector="money",
            title="Collect overdue payment" if overdue else "Review outstanding payment",
            summary=f"{row.get('party_ledger') or 'Customer'} · invoice {row.get('voucher_number') or '—'}",
            customer_name=str(row.get("party_ledger") or "Customer"), conversation_id=conversation_id,
            signal={"amount": amount, "amount_source": "tally", "followup_due_minutes": -1 if overdue else None, "operational_issue": "Payment is overdue" if overdue else "Payment remains outstanding", "operational_code": "payment_overdue" if overdue else "payment_outstanding", "operational_source": "tally"},
            updated_at=_utc(row.get("synced_at")), metrics={"amount": amount, "due_date": due_date},
            target_view="tally", target_label="Review in Tally", feature="tally", requires_confirmation=True,
        ))

    decisions = await db.seller_intelligence_decisions.find(
        {"org_id": org_id, "user_id": user_id, "item_id": {"$in": [item["id"] for item in actions]}}, {"_id": 0},
    ).to_list(length=len(actions) or 1)
    decisions_by_id = {row.get("item_id"): row for row in decisions}
    visible: list[dict] = []
    for item in actions:
        decision = decisions_by_id.get(item["id"]) or {}
        same_trigger = decision.get("fingerprint") == item["fingerprint"]
        snoozed_until = _utc(decision.get("snoozed_until"))
        if not include_suppressed and same_trigger and (decision.get("state") == "dismissed" or (snoozed_until and snoozed_until > now)):
            continue
        if same_trigger and decision.get("state") == "acknowledged":
            item["state"] = "acknowledged"
            if not preferences["show_acknowledged"] and not include_suppressed:
                continue
        if item["score"] >= preferences["minimum_score"] or include_suppressed:
            visible.append(item)
    visible.sort(key=lambda item: (-item["score"], str(item.get("updated_at") or ""), item["id"]))

    offset = int(cursor) if str(cursor).isdigit() else 0
    page = visible[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(visible) else None
    sector_summaries = []
    for sector_id, label in SECTORS:
        rows = [item for item in visible if item["sector"] == sector_id]
        sector_summaries.append({"id": sector_id, "label": label, "count": len(rows), "max_score": max((item["score"] for item in rows), default=None)})

    latest_conversation = max((value for row in conversations if (value := _utc(row.get("last_message_timestamp")))), default=None)
    latest_offer = max((value for row in offers if (value := _utc(row.get("updated_at") or row.get("created_at")))), default=None)
    latest_followup = max((value for row in followups if (value := _utc(row.get("updated_at") or row.get("created_at")))), default=None)
    latest_product = max((value for row in products if (value := _utc(row.get("updated_at")))), default=None)
    tally_state = "error" if tally_error or tally_stock_error or invoice_error else "unavailable" if not tally_integration else "fresh" if tally_connected else "stale"
    freshness = {
        "conversations": _freshness("error" if conversation_error else "live", latest_conversation, conversation_error or "Live messaging data"),
        "offers": _freshness("error" if offer_error else "fresh", latest_offer, offer_error or "Offer records"),
        "follow_ups": _freshness("error" if followup_error else "fresh", latest_followup, followup_error or "Scheduled follow-ups"),
        "inventory": _freshness("error" if product_error else "fresh" if products else "unavailable", latest_product, product_error or ("Google Sheet / manual catalogue" if products else "No catalogue connected")),
        "tally": _freshness(tally_state, tally_sync or tally_seen, tally_error or tally_stock_error or invoice_error or ("Connector online" if tally_connected else "No Tally connection" if not tally_integration else "Last synced snapshot")),
        "team": _freshness("error" if work_error else "fresh", None, work_error or "Work-team records"),
        "sales_cases": _freshness("error" if case_error else "fresh", None, case_error or "AI signals at 65%+ confidence only"),
    }
    top = visible[0] if visible else None
    return serialize_mongo({
        "generated_at": now, "scope": {"kind": scope_name, "label": scope_name.replace("_", " ").title()},
        "preferences": preferences,
        "core": {
            "action_id": top.get("id") if top else None,
            "headline": top.get("title") if top else "No urgent seller action",
            "score": top.get("score") if top else None,
            "total_actions": len(visible),
            "critical_count": sum(item["severity"] == "critical" for item in visible),
        },
        "metrics": {
            "active_offer_value": sum(value for value in (_number(row.get("total_amount")) for row in offers) if value is not None) if not offer_error else None,
            "receivables": sum(value for value in (_number(row.get("outstanding_amount")) for row in invoices) if value is not None) if tally_integration and not invoice_error else None,
        },
        "sectors": sector_summaries, "actions": page, "freshness": freshness,
        "next_cursor": next_cursor,
    })


async def get_item(org_id: str, user_id: str, role: str, item_id: str, scope: str = "") -> dict:
    snapshot = await build_snapshot(org_id, user_id, role, scope, limit=1_000, include_suppressed=True)
    item = next((row for row in snapshot["actions"] if row["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Seller action not found")
    return item


async def update_item_state(org_id: str, user_id: str, role: str, item_id: str, action: str, snooze_until: datetime | None = None) -> dict:
    item = await get_item(org_id, user_id, role, item_id)
    now = datetime.now(timezone.utc)
    if action == "snooze":
        snooze_until = _utc(snooze_until)
        if not snooze_until or not now < snooze_until <= now + timedelta(days=30):
            raise HTTPException(status_code=422, detail="Snooze must end within the next 30 days")
    state = {"acknowledge": "acknowledged", "dismiss": "dismissed", "snooze": "snoozed"}[action]
    document = {
        "org_id": org_id, "user_id": user_id, "item_id": item_id, "state": state,
        "fingerprint": item["fingerprint"], "snoozed_until": snooze_until if action == "snooze" else None,
        "updated_at": now,
    }
    await get_async_mongo_db().seller_intelligence_decisions.update_one(
        {"org_id": org_id, "user_id": user_id, "item_id": item_id}, {"$set": document}, upsert=True,
    )
    return {"item_id": item_id, "state": state, "snoozed_until": document["snoozed_until"]}
