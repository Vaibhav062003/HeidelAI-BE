from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.whatsapp_reply_idempotency import (
    ReplyOperationConflict,
    claim_reply,
    complete_reply,
    mark_unknown,
    record_acceptance,
    release_claim,
    start_provider,
)


class FakeCollection:
    def __init__(self):
        self.docs = {}

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            actual = doc.get(key)
            if isinstance(expected, dict) and "$lte" in expected:
                boundary = expected["$lte"]
                if actual is not None and actual.tzinfo is None and boundary.tzinfo is not None:
                    boundary = boundary.replace(tzinfo=None)
                if actual is None or actual > boundary:
                    return False
            elif actual != expected:
                return False
        return True

    def find_one_and_update(self, query, update, upsert=False, return_document=True):
        doc = next((item for item in self.docs.values() if self._matches(item, query)), None)
        if doc is None and upsert:
            doc = deepcopy(update.get("$setOnInsert", {}))
            self.docs[doc["_id"]] = doc
        if doc is None:
            return None
        doc.update(deepcopy(update.get("$set", {})))
        return deepcopy(doc)

    def update_one(self, query, update):
        doc = next((item for item in self.docs.values() if self._matches(item, query)), None)
        if doc:
            doc.update(deepcopy(update.get("$set", {})))

    def delete_one(self, query):
        key = next((key for key, item in self.docs.items() if self._matches(item, query)), None)
        if key:
            del self.docs[key]


def expect_conflict(action):
    try:
        action()
    except ReplyOperationConflict:
        return
    raise AssertionError("Expected reply operation conflict")


now = datetime(2026, 8, 16, tzinfo=timezone.utc)
args = ("org-a", "escalation-1", "whatsapp_919999999999", "reply", ["I can help with that."])

# Concurrent claims allow one provider attempt only.
collection = FakeCollection()
first = claim_reply(collection, *args, now=now)
expect_conflict(lambda: claim_reply(collection, *args, now=now))

# A definitely pre-provider failure releases immediately; an expired claim changes ownership.
release_claim(collection, first)
second = claim_reply(collection, *args, now=now)
# Simulate PyMongo's default naive BSON datetime decoding.
collection.docs[second["_id"]]["claim_expires_at"] = collection.docs[second["_id"]]["claim_expires_at"].replace(tzinfo=None)
stale = claim_reply(collection, *args, now=now + timedelta(minutes=3))
assert stale["claim_id"] != second["claim_id"]
assert not start_provider(collection, second, now=now + timedelta(minutes=3))
assert start_provider(collection, stale, now=now + timedelta(minutes=3))

# Ambiguous provider delivery never becomes retryable automatically.
mark_unknown(collection, stale, now=now + timedelta(minutes=3))
expect_conflict(lambda: claim_reply(collection, *args, now=now + timedelta(minutes=4)))

# Confirmed provider acceptance survives a local storage failure and resumes without resending.
collection = FakeCollection()
accepted = claim_reply(collection, *args, now=now)
assert start_provider(collection, accepted, now=now)
reconciling = record_acceptance(collection, accepted, "wamid.123", {"messages": [{"id": "wamid.123"}]}, now=now)
assert reconciling and reconciling["status"] == "reconciling"
expect_conflict(lambda: claim_reply(collection, *args, now=now + timedelta(seconds=5)))
# A crashed local reconciler can be replaced after its lease; the old worker is fenced.
collection.docs[reconciling["_id"]]["reconcile_expires_at"] = collection.docs[reconciling["_id"]]["reconcile_expires_at"].replace(tzinfo=None)
resumed = claim_reply(collection, *args, now=now + timedelta(minutes=3))
assert resumed["action"] == "resume" and resumed["reconcile_id"] != reconciling["reconcile_id"]
result = {"message": "Messages sent", "message_id": "wamid.123", "message_ids": ["wamid.123"]}
assert not complete_reply(collection, reconciling, result, now=now + timedelta(minutes=3))
assert complete_reply(collection, resumed, result, now=now + timedelta(minutes=3))
replayed = claim_reply(collection, *args, now=now + timedelta(minutes=3, seconds=1))
assert replayed["action"] == "replay" and replayed["result"] == result

# AI replies use the same durable claim and are one provider message per inbound message.
ai_args = ("org-a", "ai:wamid.1", "whatsapp_919999999999", "ai", ["AI reply"])
ai_collection = FakeCollection()
assert claim_reply(ai_collection, *ai_args, now=now)["action"] == "claimed"
expect_conflict(lambda: claim_reply(ai_collection, *ai_args, now=now))

lifespan = Path("core/lifespan.py").read_text()
service = Path("services/wa_service.py").read_text()
assert "ensure_whatsapp_outbound_indexes" in lifespan
assert 'create_index("expires_at", expireAfterSeconds=0)' in service
assert '"delivery_state": "accepted_pending_sync"' in service
assert '"local_sync_pending": True' in service
assert 'if status == "sending":\n        return message_id' not in service
assert "await _meta_http_client().post(" in service
assert "await close_meta_http_client()" in lifespan

print("WhatsApp reply idempotency behavior passed.")
