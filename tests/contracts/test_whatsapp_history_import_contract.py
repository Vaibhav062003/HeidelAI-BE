import sys
import json
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.whatsapp_history_import import ImportFailure, iter_export_messages, normalize_export  # noqa: E402


payload = {
    "919876543210@s.whatsapp.net": {
        "name": "Customer",
        "messages": {
            "one": {"from_me": False, "timestamp": 1_700_000_000, "key_id": "one", "data": "Hello", "meta": False},
            "two": {"from_me": True, "timestamp": 1_700_000_100, "key_id": "two", "data": "Hi", "meta": False},
        },
    },
    "123-456@g.us": {"messages": {"group": {"timestamp": 1_700_000_000, "data": "skip"}}},
}
rows, preview = normalize_export(payload, job_id="job", org_id="org", phone_id="line", business_phone="+100")
assert preview["message_count"] == 2 and preview["conversation_count"] == 1 and preview["skipped_count"] == 1
assert rows[0]["role"] == "customer" and rows[1]["role"] == "agent"
assert rows[0]["conversation_id"] == "whatsapp_line_919876543210"
assert rows[0]["timestamp"] == datetime.fromtimestamp(1_700_000_000, timezone.utc)
assert rows[0]["message_id"] != rows[1]["message_id"]
assert all(row["org_id"] == "org" for row in rows)

streamed = list(iter_export_messages(BytesIO(json.dumps(payload).encode())))
assert [(chat_id, source_id, message["data"]) for chat_id, _, source_id, message in streamed] == [
    ("919876543210@s.whatsapp.net", "one", "Hello"),
    ("919876543210@s.whatsapp.net", "two", "Hi"),
    ("123-456@g.us", "group", "skip"),
]

try:
    normalize_export({}, job_id="job", org_id="org", phone_id="line", business_phone="+100")
except ImportFailure:
    pass
else:
    raise AssertionError("Empty backups must be rejected")

print("WhatsApp history import check passed.")
