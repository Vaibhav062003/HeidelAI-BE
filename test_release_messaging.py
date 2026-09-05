"""Offline behavior checks: actual function ASTs, no application imports/providers."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import AsyncMock, patch

from services.whatsapp_connection_service import whatsapp_conversation_id, whatsapp_conversation_scope

ROOT = Path(__file__).parent


def functions(path, names, **scope):
    nodes = [node for node in ast.parse((ROOT / path).read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    scope.update(asyncio=asyncio, datetime=datetime, timedelta=timedelta, timezone=timezone,
                 Optional=Optional, Dict=dict)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), scope)
    return scope


def matches(row, query):
    for key, value in query.items():
        if key == "$or":
            if not any(matches(row, part) for part in value):
                return False
        elif isinstance(value, dict):
            if "$ne" in value and row.get(key) == value["$ne"]:
                return False
            if "$exists" in value and (key in row) != value["$exists"]:
                return False
        elif row.get(key) != value:
            return False
    return True


class Collection:
    def __init__(self, rows=(), unique_conversations=False):
        self.rows = deepcopy(list(rows))
        self.failures = 0
        self.lost_acknowledgements = 0
        self.before_update = None
        self.unique_conversations = unique_conversations

    def find_one(self, query, *args, **kwargs):
        return next((deepcopy(row) for row in self.rows if matches(row, query)), None)

    def count_documents(self, query):
        return sum(matches(row, query) for row in self.rows)

    def insert_many(self, rows, **kwargs):
        self.rows.extend(deepcopy(rows))

    def update_one(self, query, update, upsert=False):
        if self.before_update:
            callback, self.before_update = self.before_update, None
            callback()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("synthetic persistence outage")
        row = next((row for row in self.rows if matches(row, query)), None)
        inserted = row is None
        if inserted:
            if not upsert:
                return SimpleNamespace(modified_count=0)
            row = {key: value for key, value in query.items() if not isinstance(value, dict)}
        candidate = deepcopy(row)
        if inserted:
            candidate.update(update.get("$setOnInsert", {}))
        candidate.update(update.get("$set", {}))
        for key, value in update.get("$inc", {}).items():
            candidate[key] = candidate.get(key, 0) + value
        if self.unique_conversations and any(
            other is not row and all(other.get(key) == candidate.get(key)
                                     for key in ("platform", "business_phone_id", "customer_id"))
            for other in self.rows
        ):
            raise RuntimeError("duplicate phone/customer conversation")
        row.update(deepcopy(candidate))
        if inserted:
            self.rows.append(row)
        if self.lost_acknowledgements:
            self.lost_acknowledgements -= 1
            raise RuntimeError("synthetic acknowledgement lost after write")
        return SimpleNamespace(modified_count=1)

    def bulk_write(self, operations, **kwargs):
        for operation in operations:
            self.update_one(*operation[:2], upsert=operation[2])


class DB:
    def __init__(self):
        self.bulk_jobs = Collection([{"job_id": "job", "org_id": "org", "status": "pending", "sent": 0}])
        self.bulk_job_results = Collection()
        self.whatsapp_connections = Collection([{
            "org_id": "org", "wa_id": "waba-B", "phone_id": "phone-B", "slot": 1, "phone": "+1",
        }])
        self.collections = {"conversations_org": Collection([{
            "conversation_id": "whatsapp_919876543210", "customer_id": "919876543210",
            "business_phone_id": "phone-B", "platform": "whatsapp", "is_ai_enabled": False,
        }], unique_conversations=True), "messages_org": Collection()}

    def __getitem__(self, key):
        return self.collections[key]


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, db, missing_id=False):
        posts = []
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                posts.append(kwargs)
                return SimpleNamespace(status_code=200, json=lambda: {"messages": [{} if missing_id else {"id": "wamid-1"}]})
        logger = SimpleNamespace(**{name: lambda *args, **kwargs: None for name in ("info", "warning", "error", "success", "debug")})
        scope = functions("services/wa_service.py", {"run_bulk_send_job", "_persist_bulk_send_batch"},
            db=db, logger=logger, httpx=SimpleNamespace(AsyncClient=Client), IG_VERSION="test", json=__import__("json"),
            uuid=__import__("uuid"),
            MessageRole=SimpleNamespace(AI="ai"), whatsapp_conversation_id=whatsapp_conversation_id,
            get_broadcast_suppressed_phone_keys=lambda *args: set(), broadcast_phone_key=str,
            UpdateOne=lambda query, update, upsert=False: (query, update, upsert))
        return scope["run_bulk_send_job"], posts

    async def run_worker(self, worker):
        await worker("waba-B", "job", "org", "phone-B", "fake", "demo", "en",
                     [SimpleNamespace(phone_number="919876543210")], delay_seconds=0)

    async def test_migrated_history_uses_existing_id(self):
        db = DB()
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)
        self.assertEqual(db["messages_org"].rows[0]["conversation_id"], "whatsapp_919876543210")
        self.assertEqual(len(db["conversations_org"].rows), 1)
        self.assertFalse(db["conversations_org"].rows[0]["is_ai_enabled"])
        self.assertEqual(db.bulk_jobs.rows[0]["status"], "completed")
        self.assertNotIn("919876543210", db.bulk_job_results.rows[0]["_id"])

    async def test_partial_tracking_retries_without_provider_resend(self):
        db = DB()
        db["messages_org"].failures = 1
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(db["messages_org"].rows), 1)
        self.assertEqual(len(db.bulk_job_results.rows), 1)
        self.assertEqual(db.bulk_jobs.rows[0]["sent"], 1)
        self.assertEqual(db.bulk_jobs.rows[0]["status"], "completed")

    async def test_written_message_retry_preserves_one_message_and_count(self):
        db = DB()
        db["messages_org"].lost_acknowledgements = 1
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(db["messages_org"].rows), 1)
        self.assertEqual(len(db.bulk_job_results.rows), 1)
        self.assertEqual(db.bulk_jobs.rows[0]["sent"], 1)
        self.assertEqual(db.bulk_jobs.rows[0]["status"], "completed")

    async def test_secondary_line_does_not_take_primary_legacy_conversation(self):
        db = DB()
        del db["conversations_org"].rows[0]["business_phone_id"]
        db.whatsapp_connections.rows[0]["slot"] = 2
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(db["conversations_org"].rows), 2)
        self.assertEqual(db["messages_org"].rows[0]["conversation_id"], "whatsapp_phone-B_919876543210")
        self.assertEqual(len(posts), 1)

    async def test_primary_reuses_unmigrated_legacy_conversation(self):
        db = DB()
        del db["conversations_org"].rows[0]["business_phone_id"]
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(db["conversations_org"].rows), 1)
        self.assertEqual(db["messages_org"].rows[0]["conversation_id"], "whatsapp_919876543210")

    async def test_retry_does_not_overwrite_newer_inbound_summary(self):
        db = DB()
        db["conversations_org"].rows[0].update(last_message="new incoming message",
            last_message_timestamp=datetime.now(timezone.utc) + timedelta(minutes=1))
        worker, _ = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(db["conversations_org"].rows[0]["last_message"], "new incoming message")
        self.assertEqual(len(db["messages_org"].rows), 1)

    async def test_inbound_racing_existing_summary_wins(self):
        db = DB()
        collection = db["conversations_org"]
        collection.before_update = lambda: collection.rows[0].update(
            last_message="racing incoming message", last_message_timestamp=datetime.now(timezone.utc))
        worker, _ = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(collection.rows[0]["last_message"], "racing incoming message")
        self.assertEqual(len(db["messages_org"].rows), 1)

    async def test_inbound_racing_new_conversation_wins(self):
        db = DB()
        collection = db["conversations_org"]
        collection.rows.clear()
        collection.before_update = lambda: collection.rows.append({
            "conversation_id": "whatsapp_phone-B_919876543210", "platform": "whatsapp",
            "business_phone_id": "phone-B", "customer_id": "919876543210",
            "last_message": "racing first message", "last_message_timestamp": datetime.now(timezone.utc),
        })
        worker, _ = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(collection.rows), 1)
        self.assertEqual(collection.rows[0]["last_message"], "racing first message")
        self.assertEqual(len(db["messages_org"].rows), 1)

    async def test_exhausted_tracking_failure_is_not_completed(self):
        db = DB()
        db["conversations_org"].failures = 10
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)
        self.assertEqual(db.bulk_jobs.rows[0]["status"], "failed")
        self.assertEqual(db.bulk_jobs.rows[0]["sent"], 1)
        self.assertEqual(db["messages_org"].rows, [])
        self.assertEqual(len(db.bulk_job_results.rows), 1)

    async def test_worker_reexecution_does_not_resend(self):
        db = DB()
        worker, posts = self.worker(db)
        await self.run_worker(worker)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)

    async def test_unknown_provider_acceptance_is_not_sent(self):
        db = DB()
        worker, posts = self.worker(db, missing_id=True)
        await self.run_worker(worker)
        self.assertEqual(len(posts), 1)
        self.assertEqual(db["messages_org"].rows, [])
        self.assertNotEqual(db.bulk_job_results.rows[0]["status"], "sent")

    async def test_phone_scope_is_nonblocking_and_legacy_safe(self):
        async def find(*args, **kwargs):
            await asyncio.sleep(.02)
            return {"phone_id": "phone-B"}
        scope = functions("services/common_service.py", {"_business_phone_scope"},
            get_async_mongo_db=lambda: SimpleNamespace(whatsapp_connections=SimpleNamespace(find_one=find)),
            whatsapp_conversation_scope=whatsapp_conversation_scope)
        self.assertTrue(asyncio.iscoroutinefunction(scope["_business_phone_scope"]))
        scope_task = asyncio.create_task(scope["_business_phone_scope"]("org", "phone-B"))
        await asyncio.sleep(.001)
        self.assertFalse(scope_task.done(), "Scope lookup must yield while database I/O is pending")
        result = await scope_task
        self.assertEqual(result, whatsapp_conversation_scope("phone-B", include_legacy=True))
        tree = ast.parse((ROOT / "services/common_service.py").read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_business_phone_scope"]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(isinstance(parents[node], ast.Await) for node in calls))

    async def test_bulk_route_uses_explicit_secondary_waba(self):
        class HTTPError(Exception):
            def __init__(self, status_code, detail): self.status_code = status_code
        class Recipient:
            phone_number = "919876543210"
            def model_copy(self, update): return SimpleNamespace(**update)
        jobs = []
        resolver_calls = []
        def resolve(org, wa_id, phone_id):
            resolver_calls.append((org, wa_id, phone_id))
            return {"phone_id": "phone-B", "wa_id": "waba-B", "access_token": "fake"} if wa_id is None else None
        entitlement = SimpleNamespace(consume_quota_async=AsyncMock(), release_quota_async=AsyncMock(),
            feature_access=lambda *args: {"limits": {}}, require_feature=lambda *args: None)
        scope = functions("api/wa_routes.py", {"bulk_send_whatsapp_message"},
            Request=object, BulkMessageRequest=object, BackgroundTasks=object, CurrentUser=object,
            db=SimpleNamespace(organizations=Collection([{"org_id": "org", "wa_id": "waba-A"}])),
            HTTPException=HTTPError, resolve_outbound_whatsapp_connection=resolve,
            get_broadcast_suppressed_phone_keys=lambda *args: set(),
            logger=SimpleNamespace(info=lambda *args: None, success=lambda *args: None, error=lambda *args: None),
            uuid=SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="job")), create_bulk_job=lambda **kwargs: None,
            run_bulk_send_job=object())
        req = SimpleNamespace(recipients=[Recipient()], phone_number_id="phone-B", template_name="demo",
                              language_code="en", components=[], delay_seconds=0)
        with patch.dict("sys.modules", {"services.entitlement_service": entitlement}):
            response = await scope["bulk_send_whatsapp_message"](None, req,
                SimpleNamespace(add_task=lambda task, **kwargs: jobs.append(kwargs)), SimpleNamespace(org_id="org"))
        self.assertEqual(response["status"], "accepted")
        self.assertEqual(resolver_calls, [("org", None, "phone-B")])
        self.assertEqual(jobs[0]["wa_id"], "waba-B")


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MessagingTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("RELEASE_MESSAGING_OK")
