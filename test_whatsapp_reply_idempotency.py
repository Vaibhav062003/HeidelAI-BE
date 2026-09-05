from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.whatsapp_reply_idempotency import (
    ReplyOperationConflict,
    claim_reply,
    claim_reply_manifest,
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
try:
    claim_reply(collection, *args, now=now + timedelta(minutes=4))
except ReplyOperationConflict as exc:
    assert exc.status == "delivery_unknown"
else:
    raise AssertionError("uncertain provider result became retryable")

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

# The first AI bubble plan remains authoritative if a retry changes cardinality.
manifests = FakeCollection()
first_manifest = claim_reply_manifest(
    manifests, "org-a", "ai:wamid.manifest", "whatsapp_919999999999",
    "ai", ["First", "Second"], "job-1", now=now,
)
collapsed_manifest = claim_reply_manifest(
    manifests, "org-a", "ai:wamid.manifest", "whatsapp_919999999999",
    "ai", ["Combined"], "job-2", now=now,
)
assert collapsed_manifest["contents"] == first_manifest["contents"] == ["First", "Second"]
assert collapsed_manifest["sales_action_job_id"] == "job-1"
single_manifest = claim_reply_manifest(
    manifests, "org-a", "ai:wamid.single", "whatsapp_919999999999",
    "ai", ["Single"], None, now=now,
)
expanded_manifest = claim_reply_manifest(
    manifests, "org-a", "ai:wamid.single", "whatsapp_919999999999",
    "ai", ["Different", "Bubbles"], "job-3", now=now,
)
assert expanded_manifest["contents"] == single_manifest["contents"] == ["Single"]
assert expanded_manifest.get("sales_action_job_id") is None

# A completed AI delivery replays even if a fresh model call words it differently.
ai_replay = FakeCollection()
ai_claim = claim_reply(ai_replay, *ai_args, now=now)
assert start_provider(ai_replay, ai_claim, now=now)
ai_accepted = record_acceptance(
    ai_replay, ai_claim, "wamid.ai", {"messages": [{"id": "wamid.ai"}]}, now=now,
)
assert complete_reply(ai_replay, ai_accepted, {"message_id": "wamid.ai"}, now=now)
changed_ai_args = (*ai_args[:-1], ["Differently worded AI reply"])
assert claim_reply(ai_replay, *changed_ai_args, now=now)["action"] == "replay"
expect_conflict(lambda: claim_reply(collection, *(*args[:-1], ["Changed manual reply"]), now=now))

# A definitive rejection of a later bubble is retryable without resending an
# earlier accepted bubble.
multipart = FakeCollection()
part_one = ("org-a", "ai:wamid.2:part:0", "whatsapp_919999999999", "ai", ["First"])
part_two = ("org-a", "ai:wamid.2:part:1", "whatsapp_919999999999", "ai", ["Second"])
first_claim = claim_reply(multipart, *part_one, now=now)
assert start_provider(multipart, first_claim, now=now)
first_accepted = record_acceptance(
    multipart, first_claim, "wamid.first", {"messages": [{"id": "wamid.first"}]}, now=now,
)
assert complete_reply(multipart, first_accepted, {"message_id": "wamid.first"}, now=now)
second_claim = claim_reply(multipart, *part_two, now=now)
assert start_provider(multipart, second_claim, now=now)
release_claim(multipart, second_claim, provider_started=True)
assert claim_reply(multipart, *part_one, now=now)["action"] == "replay"
second_retry = claim_reply(multipart, *part_two, now=now)
assert second_retry["action"] == "claimed" and start_provider(multipart, second_retry, now=now)

lifespan = Path("core/lifespan.py").read_text()
service = Path("services/wa_service.py").read_text()
webhook = Path("api/webhook_routes.py").read_text()
common_routes = Path("api/common_routes.py").read_text()
decision_service = Path("services/pricing_decision_service.py").read_text()
assert "ensure_whatsapp_outbound_indexes" in lifespan
assert 'create_index("expires_at", expireAfterSeconds=0)' in service
assert '"delivery_state": "accepted_pending_sync"' in service
assert '"local_sync_pending": True' in service
assert 'if status == "sending":\n        return message_id' not in service
assert "await _meta_http_client().post(" in service
assert "_is_definitive_meta_rejection" in service
assert "await close_meta_http_client()" in lifespan
assert "finally:\n        # Shutdown must also run" in lifespan
rejection = service[service.index("if response.status_code != 200:"):service.index("response_data = response.json()")]
assert "if i == 0" not in rejection and "release_claim," in rejection
assert rejection.index("mark_unknown,") < rejection.index("release_claim,")
assert 'services.processed_message_ids.pop(temp_id.removeprefix("ai:"), None)' in rejection
assert 'if mode == "ai":' in rejection and '"delivery_error"' in rejection
request_error = service[service.index("except httpx.RequestError:"):service.index("if response.status_code != 200:")]
assert "processed_message_ids.pop" not in request_error and "mark_unknown," in request_error
accepted = service[service.index("response_data = response.json()"):service.index("if part_operation:", service.index("response_data = response.json()"))]
assert accepted.index("if not message_id:") < accepted.index("Meta accepted outbound request")
assert "if first_meta_accepted_ms is None:" in accepted
assert '"first_meta_accepted_ms": first_meta_accepted_ms' in service
latency = webhook[webhook.index("def _log_product_reply_latency("):webhook.index("def _apply_call_lifecycle")]
assert 'send_result.get("outbound_replay")' in latency
assert "received_at" in latency and "send_started_wall - received_at" in latency
assert "datetime.now(timezone.utc) - received_at" not in latency
assert 'response_class not in {"product_match", "product_alternative"}' in latency
assert 'meta_timings.get("first_meta_accepted_ms")' in latency
assert '"Product reply accepted org={}' in latency
assert 'idempotency_key=wa_message_id' in webhook
assert 'error.category in {"sales_action_in_progress", "outbound_in_progress"}' in webhook
assert "_finalize_whatsapp_sales_action" in webhook
assert '"sales_action_delivery_conflict"' in webhook
finalizer = webhook[webhook.index("async def _finalize_whatsapp_sales_action"):webhook.index("async def _send_quota_handoff")]
assert finalizer.index('action_job_id = action.get("job_id") if action else None') < finalizer.index("if not action:")
assert '"outbound_replay": True' in service
assert "mark_active_delivery_unknown" in service
assert 'raise AIProviderUnavailable("delivery_unknown") from e' in service
assert '"outbound_in_progress", escalation_required=False' in service
pending_marker = webhook[webhook.index("def mark_whatsapp_retry_pending"):webhook.index("now = datetime.now(timezone.utc)", webhook.index("def mark_whatsapp_retry_pending"))]
assert "manual_review_required" not in pending_marker
assert "finally:" in pending_marker
assert pending_marker.index("finally:") < pending_marker.index("processed_message_ids.pop")
assert "Sales action delivery is still being finalized" in webhook
assert "Instagram delivery is still being synchronized" in webhook
assert 'delivery_failure_required = error.category == "delivery_error"' in webhook
assert 'platform in {"whatsapp", "instagram"} and mode == "reply" and not temp_id' in common_routes
assert 'provider_state in {' in common_routes
assert '"temp_id": temp_id' in decision_service

print("WhatsApp reply idempotency behavior passed.")
