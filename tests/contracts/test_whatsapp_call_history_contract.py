import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.whatsapp_call_history import (  # noqa: E402
    CALL_HISTORY_PROJECTION,
    call_lifecycle_fields,
    decode_call_cursor,
    encode_call_cursor,
    provider_lifecycle_filter,
    recording_quota_evictions,
    recording_retention_deadline,
)


now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
ringing = call_lifecycle_fields({}, "connect", "USER_INITIATED", now=now, provider_time=now)
assert ringing["state"] == "ringing"
business_connect = call_lifecycle_fields({}, "connect", "BUSINESS_INITIATED", now=now, provider_time=now)
assert business_connect["state"] == "ringing" and "connected_at" not in business_connect

missed = call_lifecycle_fields(ringing, "FAILED", "USER_INITIATED", now=now + timedelta(seconds=20))
assert missed["state"] == "missed" and missed["ended_at"] == now + timedelta(seconds=20)

connected = call_lifecycle_fields(ringing, "ACCEPTED", "USER_INITIATED", now=now + timedelta(seconds=5), connected_event=True)
completed = call_lifecycle_fields(
    {**ringing, **connected},
    "COMPLETED",
    "USER_INITIATED",
    now=now + timedelta(seconds=65),
    duration_seconds=60,
)
assert completed["state"] == "completed" and completed["duration_seconds"] == 60

terminated_unanswered = call_lifecycle_fields(ringing, "terminate", "USER_INITIATED", now=now + timedelta(seconds=20))
assert terminated_unanswered["state"] == "missed"
authoritative_completed = call_lifecycle_fields(
    ringing,
    "COMPLETED",
    "USER_INITIATED",
    now=now + timedelta(seconds=60),
    end_time=now + timedelta(seconds=60),
    duration_seconds=45,
)
assert authoritative_completed["state"] == "completed"
assert authoritative_completed["connected_at"] == now + timedelta(seconds=15)
declined = call_lifecycle_fields(ringing, "REJECTED", "USER_INITIATED", now=now + timedelta(seconds=20))
assert declined["state"] == "declined"
assert call_lifecycle_fields({**ringing, **declined}, "FAILED", "USER_INITIATED", now=now + timedelta(seconds=20)) == {}
declined_after_failed = call_lifecycle_fields(
    {**ringing, **missed, "provider_updated_at": now + timedelta(seconds=20)},
    "REJECTED",
    "USER_INITIATED",
    now=now + timedelta(seconds=21),
    provider_time=now + timedelta(seconds=10),
)
assert declined_after_failed["state"] == "declined"
declined_filter = provider_lifecycle_filter("org", "wacid.test", now, "declined")
assert any("failed" in condition.get("state", {}).get("$in", []) for condition in declined_filter["$or"])

assert call_lifecycle_fields({**completed}, "RINGING", "USER_INITIATED", now=now + timedelta(seconds=70)) == {}
assert call_lifecycle_fields({**completed}, "ACCEPTED", "USER_INITIATED", now=now + timedelta(seconds=70), connected_event=True) == {}
ringing_filter = provider_lifecycle_filter("org", "wacid.test", now, "ringing")
assert "connected" in ringing_filter["state"]["$nin"] and "completed" in ringing_filter["state"]["$nin"]
cursor = encode_call_cursor(now, "wacid.test")
assert decode_call_cursor(cursor) == (now, "wacid.test")
assert "session" not in CALL_HISTORY_PROJECTION and "sdp" not in CALL_HISTORY_PROJECTION
assert "recording" in CALL_HISTORY_PROJECTION and "recording_storage" not in CALL_HISTORY_PROJECTION

assert recording_retention_deadline(now) == now + timedelta(days=15)
evictions, fits = recording_quota_evictions([
    {"_id": "old", "status": "ready", "size_bytes": 400},
    {"_id": "active", "status": "assembling", "size_bytes": 500},
    {"_id": "new", "status": "ready", "size_bytes": 300},
], 400, 1000)
assert evictions == ["old", "new"] and fits
evictions, fits = recording_quota_evictions([
    {"_id": "old", "status": "ready", "size_bytes": 50},
    {"_id": "active", "status": "assembling", "size_bytes": 900},
], 200, 1000)
assert evictions == ["old"] and not fits

print("WhatsApp call history check passed.")
