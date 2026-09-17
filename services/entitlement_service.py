import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services.feature_flags import native_invoicing_enabled

from database import get_mongo_client, get_mongo_db


PRODUCTS = ("AI", "TALLY", "FOLLOW_UPS", "OFFERS", "NATIVE_INVOICING", "AUTOMATIONS", "BROADCASTING", "SEATS", "CHANNELS")
WIRE_PRODUCTS = PRODUCTS + ("CHATBOT",)
LIMIT_KEYS = {
    "AI": {"monthly_messages"},
    "AUTOMATIONS": {"active_flows", "monthly_runs"},
    "BROADCASTING": {"max_batch_recipients", "monthly_recipients"},
    "SEATS": {"seats"},
    "CHANNELS": {"whatsapp_accounts", "instagram_accounts", "email_accounts"},
}
AI_CHANNELS = ("whatsapp", "instagram", "email", "playground", "direct")
IST = ZoneInfo("Asia/Kolkata")
db = get_mongo_db()


def ensure_entitlement_indexes() -> None:
    db.feature_entitlements.create_index([("account_id", 1), ("product", 1)], unique=True)
    db.feature_usage_counters.create_index([("account_id", 1), ("period", 1)], unique=True)
    db.feature_usage_events.create_index("created_at", expireAfterSeconds=400 * 24 * 60 * 60)
    db.feature_usage_commits.create_index("created_at", expireAfterSeconds=400 * 24 * 60 * 60)
    db.feature_automation_reservations.create_index("expires_at", expireAfterSeconds=0)
    db.feature_capacity_reservations.create_index("expires_at", expireAfterSeconds=0)
    migrate_legacy_free_grace()
    if enforcement_enabled():
        gaps = ai_enforcement_gaps()
        if gaps:
            raise RuntimeError(f"AI entitlement enforcement has {len(gaps)} unresolved organization(s); synchronize grants or deploy with ENTITLEMENT_ENFORCEMENT_MODE=observe.")


def migrate_legacy_free_grace() -> int:
    marker_id = "legacy-free-seven-day-grace-v1"
    if db.migration_markers.find_one({"_id": marker_id}):
        return 0
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    defaults = {
        "OFFERS": {},
        "AUTOMATIONS": {"active_flows": 2, "monthly_runs": 300},
        "BROADCASTING": {"max_batch_recipients": 50, "monthly_recipients": 2000},
        "SEATS": {"seats": 1},
        "CHANNELS": {"whatsapp_accounts": 1, "instagram_accounts": 1, "email_accounts": 1},
    }
    migrated = 0
    for organization in db.organizations.find({"plan": "free"}, {"org_id": 1}):
        account_id = organization.get("org_id")
        if not account_id or db.feature_entitlements.find_one({"account_id": account_id, "grant_type": {"$in": ["PAID", "TRIAL"]}}):
            continue
        for product, limits in defaults.items():
            db.feature_entitlements.update_one(
                {"_id": f"{account_id}:{product}"},
                {"$setOnInsert": {"account_id": account_id, "product": product, "state": "ACTIVE", "grant_type": "GRANDFATHERED", "starts_at": now, "expires_at": expires_at, "limits": limits, "version": 1, "reason": "One-time seven-day grace for legacy free organization", "effective_at": now, "created_at": now, "updated_at": now}},
                upsert=True,
            )
        migrated += 1
    db.migration_markers.update_one({"_id": marker_id}, {"$setOnInsert": {"migrated": migrated, "completed_at": now}}, upsert=True)
    return migrated


def enforcement_enabled() -> bool:
    return os.getenv("ENTITLEMENT_ENFORCEMENT_MODE", "observe").strip().lower() == "enforce"


def entitlement_row_active(row: dict | None, now: datetime) -> bool:
    if not row or row.get("state") != "ACTIVE":
        return False
    starts_at = row.get("starts_at")
    expires_at = row.get("expires_at")
    if starts_at and starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return not ((starts_at and starts_at > now) or (expires_at and expires_at <= now))


def ai_enforcement_gaps(limit: int = 20) -> list[str]:
    now = datetime.now(timezone.utc)
    gaps = []
    for organization in db.organizations.find({"is_ai_enabled": True}, {"org_id": 1}):
        account_id = organization.get("org_id")
        row = account_id and db.feature_entitlements.find_one({"account_id": account_id, "product": {"$in": ["AI", "CHATBOT"]}})
        if account_id and not entitlement_row_active(row, now):
            gaps.append(account_id)
            if len(gaps) >= limit:
                break
    return gaps


def period_window(now: datetime | None = None) -> tuple[str, datetime]:
    local = (now or datetime.now(timezone.utc)).astimezone(IST)
    if local.month == 12:
        reset = local.replace(year=local.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        reset = local.replace(month=local.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.strftime("%Y-%m"), reset.astimezone(timezone.utc)


def recent_periods(count: int, now: datetime | None = None) -> list[str]:
    local = (now or datetime.now(timezone.utc)).astimezone(IST)
    periods = []
    year, month = local.year, local.month
    for _ in range(count):
        periods.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return periods


def normalize_product(value: str) -> str:
    return str(value or "").strip().upper()


def validate_limits(product: str, limits: dict | None) -> dict:
    limits = limits or {}
    allowed = LIMIT_KEYS.get(product, set())
    if set(limits) - allowed:
        raise HTTPException(status_code=422, detail={"code": "invalid_limits", "message": f"Unsupported limits for {product}."})
    normalized = {}
    for key, value in limits.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise HTTPException(status_code=422, detail={"code": "invalid_limits", "message": f"{key} must be a non-negative integer or null."})
        normalized[key] = value
    if product == "BROADCASTING" and all(normalized.get(key) is not None for key in ("max_batch_recipients", "monthly_recipients")):
        if normalized["max_batch_recipients"] > normalized["monthly_recipients"]:
            raise HTTPException(status_code=422, detail={"code": "invalid_limits", "message": "The broadcast batch limit cannot exceed its monthly limit."})
    return normalized


def apply_entitlement(account_id: str, payload: dict) -> dict:
    product = normalize_product(payload.get("product"))
    if product not in WIRE_PRODUCTS:
        raise HTTPException(status_code=422, detail={"code": "invalid_product", "message": "Unknown entitlement product."})
    if not db.organizations.find_one({"org_id": account_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail={"code": "organization_not_found", "message": "HeidelAI organization not found."})
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HTTPException(status_code=422, detail={"code": "invalid_version", "message": "Version must be a positive integer."})
    state = str(payload.get("state") or "").upper()
    grant_type = str(payload.get("grant_type") or "PAID").upper()
    if state not in {"ACTIVE", "SUSPENDED"} or grant_type not in {"PAID", "TRIAL", "GRANDFATHERED"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_entitlement", "message": "Invalid state or grant type."})

    def parsed(name: str, required: bool = False):
        raw = payload.get(name)
        if raw is None and not required:
            return None
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_entitlement", "message": f"{name} must be an ISO timestamp."}) from exc

    effective_at = parsed("effective_at") or datetime.now(timezone.utc)
    starts_at = parsed("starts_at") or effective_at
    expires_at = parsed("expires_at")
    if grant_type == "TRIAL" and expires_at is None:
        raise HTTPException(status_code=422, detail={"code": "invalid_entitlement", "message": "Trials require an expiry."})
    if expires_at and expires_at <= starts_at:
        raise HTTPException(status_code=422, detail={"code": "invalid_entitlement", "message": "Expiry must be after the start time."})
    limits = validate_limits(product, payload.get("limits"))
    document_id = f"{account_id}:{product}"
    comparable = {"state": state, "grant_type": grant_type, "starts_at": starts_at, "expires_at": expires_at, "limits": limits}
    now = datetime.now(timezone.utc)
    try:
        db.feature_entitlements.find_one_and_update(
            {"_id": document_id, "$or": [{"version": {"$lt": version}}, {"version": {"$exists": False}}]},
            {"$set": {"account_id": account_id, "product": product, **comparable, "version": version, "reason": str(payload.get("reason") or "")[:500], "effective_at": effective_at, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        current = db.feature_entitlements.find_one({"_id": document_id}) or {}
        if current.get("version", 0) > version:
            raise HTTPException(status_code=409, detail={"code": "stale_entitlement", "message": "A newer entitlement is already applied."})
        if current.get("version") != version or any(current.get(key) != value for key, value in comparable.items()):
            raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": "This version was already applied with different values."})
    return {"account_id": account_id, "state": state, "version": version}


def feature_access(account_id: str, product: str, now: datetime | None = None, *, strict: bool = False) -> dict:
    product = normalize_product(product)
    if product == "NATIVE_INVOICING" and not native_invoicing_enabled(account_id):
        return {"state": "locked", "grant_type": "ENV_DISABLED", "starts_at": None, "expires_at": None, "limits": {}}
    if not strict and not enforcement_enabled():
        return {"state": "active", "grant_type": "GRANDFATHERED", "starts_at": None, "expires_at": None, "limits": {}, "observed": True}
    now = now or datetime.now(timezone.utc)
    row = db.feature_entitlements.find_one({"account_id": account_id, "product": product})
    if product == "AI" and not row:
        row = db.feature_entitlements.find_one({"account_id": account_id, "product": "CHATBOT"})
    if not row:
        return {"state": "locked", "grant_type": None, "starts_at": None, "expires_at": None, "limits": {}}
    starts_at = row.get("starts_at")
    expires_at = row.get("expires_at")
    if starts_at and starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if not entitlement_row_active(row, now):
        state = "read_only" if starts_at is None or starts_at <= now else "locked"
    else:
        state = "active"
    return {"state": state, "grant_type": row.get("grant_type"), "starts_at": starts_at, "expires_at": expires_at, "limits": row.get("limits") or {}, "version": row.get("version")}


def require_feature(account_id: str, product: str, *, strict: bool = False) -> dict:
    if strict and not db.organizations.find_one({"org_id": account_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail={"code": "organization_not_found", "message": "HeidelAI organization not found."})
    access = feature_access(account_id, product, strict=strict)
    if access["state"] != "active":
        raise HTTPException(status_code=403, detail={"code": "feature_unavailable", "feature": normalize_product(product), "state": access["state"], "message": f"{normalize_product(product).replace('_', ' ').title()} is not active for this organization."})
    return access


def capacity_used(account_id: str, metric: str) -> int:
    filters = {
        "whatsapp_accounts": (db.whatsapp_connections, {"org_id": account_id, "is_active": {"$ne": False}}),
        "instagram_accounts": (db.instagram_connections, {"org_id": account_id, "is_active": {"$ne": False}}),
        "email_accounts": (db.google_connections, {"org_id": account_id, "status": "active"}),
    }
    if metric == "seats":
        organization = db.organizations.find_one({"org_id": account_id}, {"member_emails": 1}) or {}
        return 1 + len(set(organization.get("member_emails") or []))
    if metric not in filters:
        raise ValueError("unsupported capacity metric")
    collection, query = filters[metric]
    return collection.count_documents(query)


def reserve_capacity(account_id: str, product: str, metric: str, resource_key: str, used_override: int | None = None) -> str | None:
    access = require_feature(account_id, product)
    limit = access["limits"].get(metric)
    if limit is None:
        return None
    reservation_id = hashlib.sha256(f"{account_id}:{metric}:{resource_key}".encode()).hexdigest()
    now = datetime.now(timezone.utc)

    def transaction(session):
        db.feature_entitlement_locks.update_one({"_id": f"{account_id}:{metric}"}, {"$inc": {"version": 1}}, upsert=True, session=session)
        if db.feature_capacity_reservations.find_one({"_id": reservation_id, "expires_at": {"$gt": now}}, session=session):
            return
        used = capacity_used(account_id, metric) if used_override is None else used_override
        pending = db.feature_capacity_reservations.count_documents({"account_id": account_id, "metric": metric, "expires_at": {"$gt": now}}, session=session)
        if used + pending >= limit:
            raise HTTPException(status_code=409, detail={"code": "capacity_exhausted", "feature": product, "metric": metric, "used": used + pending, "limit": limit, "message": f"The {metric.replace('_', ' ')} limit has been reached."})
        db.feature_capacity_reservations.update_one({"_id": reservation_id}, {"$set": {"account_id": account_id, "metric": metric, "resource_key": resource_key, "expires_at": datetime.fromtimestamp(now.timestamp() + 600, timezone.utc)}}, upsert=True, session=session)

    with get_mongo_client().start_session() as session:
        session.with_transaction(transaction)
    return reservation_id


def release_capacity(reservation_id: str | None) -> None:
    if reservation_id:
        db.feature_capacity_reservations.delete_one({"_id": reservation_id})


def quota_status(account_id: str, product: str, metric: str) -> dict:
    access = require_feature(account_id, product)
    limit = access["limits"].get(metric)
    period, reset_at = period_window()
    counter = db.feature_usage_counters.find_one({"account_id": account_id, "period": period}, {f"usage.{metric}": 1}) or {}
    used = int((counter.get("usage") or {}).get(metric, 0))
    return {"used": used, "limit": limit, "remaining": None if limit is None else max(0, limit - used), "reset_at": reset_at}


def _quota_error(product: str, metric: str, status: dict) -> HTTPException:
    return HTTPException(status_code=429, detail={"code": "quota_exhausted", "feature": product, "metric": metric, **status, "message": f"The {metric.replace('_', ' ')} allowance is exhausted until the next monthly reset."})


def consume_quota(account_id: str, product: str, metric: str, quantity: int, event_key: str) -> dict:
    if quantity < 1:
        raise ValueError("quantity must be positive")
    product = normalize_product(product)
    access = require_feature(account_id, product)
    if metric not in LIMIT_KEYS.get(product, set()):
        raise ValueError(f"unsupported quota metric {metric}")
    period, reset_at = period_window()
    event_id = hashlib.sha256(f"{account_id}:{period}:{metric}:{event_key}".encode()).hexdigest()
    client = get_mongo_client()
    result = {}

    def transaction(session):
        nonlocal result
        existing = db.feature_usage_events.find_one({"_id": event_id}, session=session)
        counter = db.feature_usage_counters.find_one({"account_id": account_id, "period": period}, session=session) or {}
        used = int((counter.get("usage") or {}).get(metric, 0))
        limit = access["limits"].get(metric)
        if existing:
            result = {"used": used, "limit": limit, "remaining": None if limit is None else max(0, limit - used), "reset_at": reset_at, "duplicate": True}
            return
        if limit is not None and used + quantity > limit:
            raise _quota_error(product, metric, {"used": used, "limit": limit, "remaining": max(0, limit - used), "reset_at": reset_at})
        now = datetime.now(timezone.utc)
        db.feature_usage_counters.update_one({"account_id": account_id, "period": period}, {"$inc": {f"usage.{metric}": quantity}, "$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True, session=session)
        db.feature_usage_events.insert_one({"_id": event_id, "account_id": account_id, "period": period, "product": product, "metric": metric, "quantity": quantity, "event_key": event_key, "created_at": now}, session=session)
        result = {"used": used + quantity, "limit": limit, "remaining": None if limit is None else max(0, limit - used - quantity), "reset_at": reset_at, "duplicate": False}

    with client.start_session() as session:
        session.with_transaction(transaction)
    return result


def reserve_quota(account_id: str, product: str, metric: str, quantity: int, event_key: str) -> dict:
    """Atomically reserve a complete outbound operation before any provider call."""
    if quantity < 1:
        raise ValueError("quantity must be positive")
    product = normalize_product(product)
    access = require_feature(account_id, product)
    if metric not in LIMIT_KEYS.get(product, set()):
        raise ValueError(f"unsupported quota metric {metric}")
    period, reset_at = period_window()
    event_id = hashlib.sha256(f"{account_id}:{period}:{metric}:{event_key}".encode()).hexdigest()
    result = {}

    def transaction(session):
        nonlocal result
        existing = db.feature_usage_events.find_one({"_id": event_id}, session=session)
        counter = db.feature_usage_counters.find_one({"account_id": account_id, "period": period}, session=session) or {}
        used = int((counter.get("usage") or {}).get(metric, 0))
        limit = access["limits"].get(metric)
        if existing:
            if existing.get("quantity") != quantity or existing.get("event_key") != event_key:
                raise HTTPException(status_code=409, detail={"code": "usage_event_conflict", "message": "This usage key was already reserved for different usage."})
            result = {"used": used, "limit": limit, "remaining": None if limit is None else max(0, limit - used), "reset_at": reset_at, "duplicate": True, "committed": int(existing.get("committed", 0))}
            return
        if limit is not None and used + quantity > limit:
            raise _quota_error(product, metric, {"used": used, "limit": limit, "remaining": max(0, limit - used), "reset_at": reset_at})
        now = datetime.now(timezone.utc)
        db.feature_usage_counters.update_one({"account_id": account_id, "period": period}, {"$inc": {f"usage.{metric}": quantity}, "$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}}, upsert=True, session=session)
        db.feature_usage_events.insert_one({"_id": event_id, "account_id": account_id, "period": period, "product": product, "metric": metric, "quantity": quantity, "committed": 0, "status": "reserved", "event_key": event_key, "created_at": now}, session=session)
        result = {"used": used + quantity, "limit": limit, "remaining": None if limit is None else max(0, limit - used - quantity), "reset_at": reset_at, "duplicate": False, "committed": 0}

    with get_mongo_client().start_session() as session:
        session.with_transaction(transaction)
    return result


def commit_quota(account_id: str, metric: str, event_key: str, part_key: str) -> bool:
    """Commit one accepted provider bubble; part_key makes retries idempotent."""
    period, _ = period_window()
    event_id = hashlib.sha256(f"{account_id}:{period}:{metric}:{event_key}".encode()).hexdigest()
    commit_id = hashlib.sha256(f"{event_id}:{part_key}".encode()).hexdigest()
    committed = False

    def transaction(session):
        nonlocal committed
        if db.feature_usage_commits.find_one({"_id": commit_id}, session=session):
            committed = True
            return
        event = db.feature_usage_events.find_one({"_id": event_id, "status": {"$in": ["reserved", "partially_committed"]}}, session=session)
        if not event or int(event.get("committed", 0)) >= int(event.get("quantity", 0)):
            return
        now = datetime.now(timezone.utc)
        db.feature_usage_events.update_one({"_id": event_id, "committed": event.get("committed", 0)}, {"$inc": {"committed": 1}, "$set": {"status": "committed" if int(event.get("committed", 0)) + 1 == int(event.get("quantity", 0)) else "partially_committed", "updated_at": now}}, session=session)
        db.feature_usage_commits.insert_one({"_id": commit_id, "event_id": event_id, "part_key": part_key, "created_at": now}, session=session)
        committed = True

    with get_mongo_client().start_session() as session:
        session.with_transaction(transaction)
    return committed


def release_quota(account_id: str, metric: str, event_key: str) -> bool:
    period, _ = period_window()
    event_id = hashlib.sha256(f"{account_id}:{period}:{metric}:{event_key}".encode()).hexdigest()
    client = get_mongo_client()
    released = False

    def transaction(session):
        nonlocal released
        event = db.feature_usage_events.find_one({"_id": event_id}, session=session)
        if not event:
            return
        quantity = int(event.get("quantity", 0))
        committed = int(event.get("committed", 0)) if event.get("status") in {"reserved", "partially_committed", "committed", "released"} else 0
        releasable = max(0, quantity - committed)
        if event.get("status") in {"committed", "released"} or releasable == 0:
            released = event.get("status") == "released"
            return
        db.feature_usage_counters.update_one({"account_id": account_id, "period": period, f"usage.{metric}": {"$gte": releasable}}, {"$inc": {f"usage.{metric}": -releasable}, "$set": {"updated_at": datetime.now(timezone.utc)}}, session=session)
        if event.get("status") in {"reserved", "partially_committed"}:
            db.feature_usage_events.update_one({"_id": event_id}, {"$set": {"quantity": committed, "status": "committed" if committed else "released", "updated_at": datetime.now(timezone.utc)}} , session=session)
        else:
            db.feature_usage_events.delete_one({"_id": event_id}, session=session)
        released = True

    with client.start_session() as session:
        session.with_transaction(transaction)
    return released


async def consume_quota_async(*args, **kwargs) -> dict:
    return await asyncio.to_thread(consume_quota, *args, **kwargs)


async def reserve_quota_async(*args, **kwargs) -> dict:
    return await asyncio.to_thread(reserve_quota, *args, **kwargs)


async def commit_quota_async(*args, **kwargs) -> bool:
    return await asyncio.to_thread(commit_quota, *args, **kwargs)


async def release_quota_async(*args, **kwargs) -> bool:
    return await asyncio.to_thread(release_quota, *args, **kwargs)


def record_ai_usage(
    account_id: str,
    event_key: str,
    channel: str,
    quantity: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    recorded_cost_microusd: int = 0,
) -> dict:
    if channel not in AI_CHANNELS:
        raise ValueError("unsupported AI channel")
    values = (quantity, input_tokens, output_tokens, recorded_cost_microusd)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values) or quantity < 1:
        raise ValueError("AI usage values must be non-negative integers and quantity must be positive")
    period, _ = period_window()
    event_id = hashlib.sha256(f"{account_id}:{period}:ai-analytics:{event_key}".encode()).hexdigest()
    result = {"duplicate": False}

    def transaction(session):
        if db.feature_usage_events.find_one({"_id": event_id}, session=session):
            result["duplicate"] = True
            return
        now = datetime.now(timezone.utc)
        increments = {
            f"usage.ai_messages_{channel}": quantity,
            "usage.ai_input_tokens": input_tokens,
            "usage.ai_output_tokens": output_tokens,
            "usage.ai_recorded_cost_microusd": recorded_cost_microusd,
        }
        db.feature_usage_counters.update_one(
            {"account_id": account_id, "period": period},
            {"$inc": increments, "$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
            session=session,
        )
        db.feature_usage_events.insert_one({
            "_id": event_id,
            "account_id": account_id,
            "period": period,
            "product": "AI",
            "metric": "analytics",
            "quantity": quantity,
            "event_key": event_key,
            "channel": channel,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "recorded_cost_microusd": recorded_cost_microusd,
            "created_at": now,
        }, session=session)

    with get_mongo_client().start_session() as session:
        session.with_transaction(transaction)
    return result


async def record_ai_usage_async(*args, **kwargs) -> dict:
    return await asyncio.to_thread(record_ai_usage, *args, **kwargs)


def automation_active_count(account_id: str, session=None) -> int:
    return db[f"automation_flows_{account_id}"].count_documents({"org_id": account_id, "status": "published"}, session=session) + db[f"wa_bot_flows_{account_id}"].count_documents({"org_id": account_id, "status": "active"}, session=session)


def require_automation_publish(account_id: str, flow_id: str, engine: str) -> bool:
    access = require_feature(account_id, "AUTOMATIONS")
    limit = access["limits"].get("active_flows")
    if limit is None:
        return False
    collection = db[f"wa_bot_flows_{account_id}"] if engine == "wabot" else db[f"automation_flows_{account_id}"]
    live_status = "active" if engine == "wabot" else "published"
    if collection.find_one({"org_id": account_id, "flow_id": flow_id, "status": live_status}, {"_id": 1}):
        return False
    reservation_id = f"{account_id}:{engine}:{flow_id}"
    now = datetime.now(timezone.utc)
    client = get_mongo_client()
    reserved = False

    def transaction(session):
        nonlocal reserved
        db.feature_entitlement_locks.update_one({"_id": f"{account_id}:automations"}, {"$inc": {"version": 1}}, upsert=True, session=session)
        if db.feature_automation_reservations.find_one({"_id": reservation_id, "expires_at": {"$gt": now}}, session=session):
            return
        used = automation_active_count(account_id, session=session)
        pending = db.feature_automation_reservations.count_documents({"account_id": account_id, "expires_at": {"$gt": now}}, session=session)
        if used + pending >= limit:
            raise HTTPException(status_code=409, detail={"code": "active_automation_limit", "feature": "AUTOMATIONS", "metric": "active_flows", "used": used + pending, "limit": limit, "message": "Unpublish an automation or raise the active automation limit before publishing another."})
        db.feature_automation_reservations.update_one({"_id": reservation_id}, {"$set": {"account_id": account_id, "expires_at": datetime.fromtimestamp(now.timestamp() + 120, timezone.utc)}}, upsert=True, session=session)
        reserved = True

    with client.start_session() as session:
        session.with_transaction(transaction)
    return reserved


def release_automation_publish(account_id: str, flow_id: str, engine: str) -> None:
    db.feature_automation_reservations.delete_one({"_id": f"{account_id}:{engine}:{flow_id}"})


def _analytics(values: dict) -> dict:
    return {
        "input_tokens": int(values.get("ai_input_tokens", 0)),
        "output_tokens": int(values.get("ai_output_tokens", 0)),
        "recorded_cost_microusd": int(values.get("ai_recorded_cost_microusd", 0)),
        "channels": {channel: int(values.get(f"ai_messages_{channel}", 0)) for channel in AI_CHANNELS},
    }


def access_snapshot(account_id: str, history_months: int = 12) -> dict:
    features = {product.lower(): feature_access(account_id, product) for product in PRODUCTS}
    period, reset_at = period_window()
    counter = db.feature_usage_counters.find_one({"account_id": account_id, "period": period}) or {}
    usage_values = counter.get("usage") or {}

    def metric(product: str, key: str):
        limit = features[product.lower()]["limits"].get(key)
        used = automation_active_count(account_id) if key == "active_flows" else int(usage_values.get(key, 0))
        return {"used": used, "limit": limit, "remaining": None if limit is None else max(0, limit - used), "reset_at": None if key == "active_flows" else reset_at}

    periods = recent_periods(max(1, min(history_months, 24)))
    counters = db.feature_usage_counters.find({"account_id": account_id, "period": {"$in": periods}})
    by_period = {row["period"]: row.get("usage") or {} for row in counters}
    history = []
    for item_period in reversed(periods):
        values = by_period.get(item_period, {})
        history.append({
            "period": item_period,
            "ai_messages": int(values.get("monthly_messages", 0)),
            "automation_runs": int(values.get("monthly_runs", 0)),
            "broadcast_recipients": int(values.get("monthly_recipients", 0)),
            **_analytics(values),
        })

    return {
        "features": features,
        "usage": {
            "ai_messages": metric("AI", "monthly_messages"),
            "active_automations": metric("AUTOMATIONS", "active_flows"),
            "automation_runs": metric("AUTOMATIONS", "monthly_runs"),
            "broadcast_recipients": metric("BROADCASTING", "monthly_recipients"),
            "broadcast_batch_limit": features["broadcasting"]["limits"].get("max_batch_recipients"),
            **{key: {"used": (used := capacity_used(account_id, key)), "limit": (capacity_limit := features["seats" if key == "seats" else "channels"]["limits"].get(key)), "remaining": None if capacity_limit is None else max(0, capacity_limit - used), "reset_at": None} for key in ("seats", "whatsapp_accounts", "instagram_accounts", "email_accounts")},
        },
        "analytics": _analytics(usage_values),
        "history": history,
        "period": period,
    }
