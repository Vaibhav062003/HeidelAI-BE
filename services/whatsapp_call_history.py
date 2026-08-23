import base64
import binascii
import json
import math
from datetime import datetime, timedelta, timezone


CALL_HISTORY_FIELDS = (
    "call_id",
    "conversation_id",
    "contact_name",
    "phone",
    "direction",
    "state",
    "agent_id",
    "agent_name",
    "agent_device_id",
    "handled_by",
    "created_at",
    "updated_at",
    "started_at",
    "connected_at",
    "ended_at",
    "duration_seconds",
)

CALL_HISTORY_PROJECTION = {field: 1 for field in CALL_HISTORY_FIELDS} | {"_id": 0}
TERMINAL_CALL_STATES = {"completed", "declined", "missed", "failed"}
STORED_TERMINAL_CALL_STATES = TERMINAL_CALL_STATES | {"ended", "terminate", "terminated", "rejected"}


def meta_datetime(value, fallback: datetime | None = None) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return parsed if math.isfinite(parsed.timestamp()) else fallback
    except (TypeError, ValueError, OverflowError):
        return fallback


def canonical_call_state(value: str | None) -> str:
    state = str(value or "").strip().lower()
    if state in {"started", "connect", "ringing"}:
        return "ringing"
    if state in {"pre_accept", "connecting"}:
        return "connecting"
    if state in {"accepted", "connected"}:
        return "connected"
    if state in {"rejected", "declined"}:
        return "declined"
    if state in {"terminate", "terminated", "ended", "completed"}:
        return "completed"
    if state in {"missed", "failed"}:
        return state
    return state or "ringing"


def call_state_rank(value: str | None) -> int:
    state = canonical_call_state(value)
    if state in TERMINAL_CALL_STATES:
        return {"completed": 44, "declined": 43, "missed": 42, "failed": 41}[state]
    return {"connected": 30, "connecting": 20, "ringing": 10}.get(state, 0)


def provider_lifecycle_filter(org_id: str, call_id: str, provider_time: datetime, state: str) -> dict:
    rank = call_state_rank(state)
    canonical = canonical_call_state(state)
    terminal_aliases = {
        "completed": {"completed", "ended", "terminate", "terminated"},
        "declined": {"declined", "rejected"},
        "missed": {"missed"},
        "failed": {"failed"},
    }
    blocked_states = set()
    lower_terminal_states = set()
    for stored_state in STORED_TERMINAL_CALL_STATES:
        stored_canonical = canonical_call_state(stored_state)
        if stored_canonical != canonical and call_state_rank(stored_canonical) >= rank:
            blocked_states.add(stored_state)
        elif canonical in TERMINAL_CALL_STATES and call_state_rank(stored_canonical) < rank:
            lower_terminal_states.add(stored_state)
    if canonical in terminal_aliases:
        blocked_states -= terminal_aliases[canonical]
        if canonical != "completed":
            blocked_states |= {"connected", "accepted"}
    elif canonical in {"ringing", "connecting"}:
        blocked_states |= {"connected", "accepted"}
    ordering = [
        {"provider_updated_at": {"$exists": False}},
        {"provider_updated_at": {"$lt": provider_time}},
        {
            "provider_updated_at": provider_time,
            "$or": [
                {"provider_state_rank": {"$exists": False}},
                {"provider_state_rank": {"$lt": rank}},
            ],
        },
    ]
    if lower_terminal_states:
        ordering.append({"state": {"$in": list(lower_terminal_states)}})
    query = {
        "org_id": org_id,
        "call_id": call_id,
        "state": {"$nin": list(blocked_states)},
        "$or": ordering,
    }
    return query


def call_lifecycle_fields(
    existing: dict,
    raw_state: str | None,
    direction: str,
    *,
    now: datetime,
    provider_time: datetime | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    duration_seconds=None,
    connected_event: bool = False,
) -> dict:
    current = canonical_call_state(existing.get("state"))
    was_connected = bool(existing.get("connected_at")) or current == "connected"
    raw = str(raw_state or "").strip().lower()
    try:
        supplied_duration = float(duration_seconds)
    except (TypeError, ValueError):
        supplied_duration = math.nan
    candidate = "connected" if connected_event else canonical_call_state(raw_state)
    if raw in {"terminate", "terminated", "ended"}:
        candidate = "completed" if was_connected or (math.isfinite(supplied_duration) and supplied_duration > 0) else ("missed" if direction == "USER_INITIATED" else "failed")
    if candidate == "failed":
        candidate = "completed" if was_connected else ("missed" if direction == "USER_INITIATED" else "failed")

    previous_provider_time = meta_datetime(existing.get("provider_updated_at"))
    semantic_terminal_upgrade = (
        current in TERMINAL_CALL_STATES
        and candidate in TERMINAL_CALL_STATES
        and call_state_rank(candidate) > call_state_rank(current)
    )
    if provider_time and previous_provider_time and provider_time < previous_provider_time and not semantic_terminal_upgrade:
        return {}
    if current in TERMINAL_CALL_STATES:
        if candidate != current and call_state_rank(candidate) <= call_state_rank(current):
            return {}
        if candidate == current:
            candidate = current
    elif current == "connected" and candidate in {"ringing", "connecting"}:
        candidate = current

    fields = {
        "direction": direction,
        "state": candidate,
        "updated_at": now,
    }
    if provider_time:
        fields["provider_updated_at"] = provider_time
        fields["provider_state_rank"] = call_state_rank(candidate)
    if not existing.get("created_at"):
        fields["created_at"] = start_time or provider_time or now
    if not existing.get("started_at"):
        fields["started_at"] = start_time or provider_time or now
    if candidate == "connected" and not existing.get("connected_at"):
        fields["connected_at"] = provider_time or now
    if candidate == "completed" and not existing.get("connected_at") and end_time and math.isfinite(supplied_duration) and supplied_duration > 0:
        fields["connected_at"] = end_time - timedelta(seconds=supplied_duration)
    if candidate in TERMINAL_CALL_STATES:
        ended_at = end_time or provider_time or now
        fields["ended_at"] = ended_at
        if math.isfinite(supplied_duration) and supplied_duration >= 0:
            fields["duration_seconds"] = round(supplied_duration)
        elif candidate == "completed":
            connected_at = meta_datetime(existing.get("connected_at"))
            if connected_at:
                fields["duration_seconds"] = max(0, round((ended_at - connected_at).total_seconds()))
    return fields


def encode_call_cursor(updated_at: datetime, call_id: str) -> str:
    value = json.dumps([meta_datetime(updated_at).isoformat(), call_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_call_cursor(value: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        timestamp, call_id = json.loads(raw)
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(call_id, str) or not call_id or parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc), call_id
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid call-history cursor") from exc
