"""Offline failure injection against actual function bodies, without app credentials."""
import ast
import asyncio
from collections import defaultdict
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import inspect
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).parent


def functions(path, names, **scope):
    nodes = [node for node in ast.parse((ROOT / path).read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    for node in nodes:
        node.decorator_list = []
        node.returns = None
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            arg.annotation = None
    scope = dict(asyncio=asyncio, datetime=datetime, timedelta=timedelta,
                 timezone=timezone, logger=Mock(), **scope)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), path, "exec"), scope)
    return scope


class InboxOutageTests(unittest.IsolatedAsyncioTestCase):
    def websocket_scope(self, db, duplicate=False):
        socket = SimpleNamespace(headers={}, accept=AsyncMock(), close=AsyncMock(), send_json=AsyncMock())
        state = SimpleNamespace(active_clients={}, client_metadata={}, org_clients=defaultdict(set),
                                agent_clients=defaultdict(set), instagram_clients=defaultdict(set), whatsapp_clients=defaultdict(set))
        class Disconnect(Exception):
            pass
        class InvalidFrame(Exception):
            pass
        scope = functions("api/websockets_routes.py", ["main_ws", "_active_account_ids", "_reject_if_mongo_unavailable"],
                          db=db, services=state, suppress=suppress,
                          mongo_retry_allowed=lambda: True,
                          validate_websocket_identifier=Mock(),
                          HTTPAuthorizationCredentials=lambda **kw: SimpleNamespace(**kw),
                          authenticate_credentials=AsyncMock(return_value=SimpleNamespace(org_id="org", user_id="user", role="admin", full_name="Agent")),
                          websocket_client_id_in_use=Mock(side_effect=[False, duplicate]),
                          cleanup_client=AsyncMock(), InvalidWebSocketControlFrame=InvalidFrame,
                          WebSocketDisconnect=Disconnect, get_org_presence_snapshot=Mock(return_value={}),
                          start_heartbeat=AsyncMock(), receive_websocket_control_frame=AsyncMock(side_effect=Disconnect))
        return scope, socket, state

    async def test_websocket_rejects_before_database_access_during_outage(self):
        db = Mock(side_effect=AssertionError("outage websocket accessed database"))
        scope, socket, _ = self.websocket_scope(db)
        scope["mongo_retry_allowed"] = lambda: False
        await scope["main_ws"](socket, "client")
        socket.accept.assert_awaited_once()
        socket.close.assert_awaited_once_with(code=1013, reason="Service temporarily unavailable")
        scope["authenticate_credentials"].assert_not_awaited()

    async def assert_nonblocking(self, invoke, **kwargs):
        """A real thread-blocking fake must not hold the event loop until it returns."""
        loop_thread = threading.get_ident()
        threads = []
        release = threading.Event()
        def stalled(*args, **kw):
            threads.append(threading.get_ident())
            release.wait(0.5)
            raise TimeoutError("simulated database selection timeout")
        task = asyncio.create_task(invoke(stalled, **kwargs))
        await asyncio.sleep(0.01)
        pending = not task.done()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(threads, "test did not execute database operation")
        self.assertNotIn(loop_thread, threads, "database operation ran on event loop")
        self.assertTrue(pending, "event loop could not run until database returned")

    async def test_feature_access_does_not_block(self):
        async def invoke(stalled):
            scope = functions("api/entitlement_routes.py", ["get_feature_access"], access_snapshot=stalled)
            handler = scope["get_feature_access"]
            user = SimpleNamespace(org_id="org")
            # FastAPI dispatches ordinary def endpoints through its threadpool.
            return await handler(user) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, user)
        await self.assert_nonblocking(invoke)

    async def test_whatsapp_connections_does_not_block(self):
        async def invoke(stalled):
            scope = functions("api/wa_routes.py", ["list_whatsapp_connections"], whatsapp_connection_limit=stalled)
            handler = scope["list_whatsapp_connections"]
            user = SimpleNamespace(org_id="org")
            return await handler(None, user) if inspect.iscoroutinefunction(handler) else await asyncio.to_thread(handler, None, user)
        await self.assert_nonblocking(invoke)

    async def test_revocation_poll_does_not_block(self):
        async def invoke(stalled):
            collection = Mock()
            collection.find.return_value.limit.return_value.__iter__ = lambda _: stalled()
            scope = functions("services/wa_service.py", ["reconcile_pending_whatsapp_revocations"],
                              db=SimpleNamespace(whatsapp_connections=collection),
                              mongo_retry_allowed=lambda: True,
                              mark_mongo_unavailable=Mock(),
                              ConnectionFailure=TimeoutError)
            return await scope["reconcile_pending_whatsapp_revocations"]()
        await self.assert_nonblocking(invoke)

    async def test_empty_product_extraction_does_not_query_database(self):
        db = Mock(side_effect=AssertionError("empty extraction accessed database"))
        scope = functions("services/product_request_service.py", ["capture_product_requests"], get_async_mongo_db=db)
        await scope["capture_product_requests"]({}, [], [])
        db.assert_not_called()

    async def test_websocket_database_reads_do_not_block(self):
        for failing_collection in ("organizations", "instagram_connections", "whatsapp_connections"):
            with self.subTest(collection=failing_collection):
                async def invoke(stalled):
                    db = SimpleNamespace(organizations=SimpleNamespace(find_one=lambda *_: {}),
                                         instagram_connections=SimpleNamespace(find=lambda *_: []),
                                         whatsapp_connections=SimpleNamespace(find=lambda *_: []))
                    if failing_collection == "organizations":
                        db.organizations.find_one = stalled
                    else:
                        class Cursor:
                            def __iter__(self):
                                return stalled()
                        getattr(db, failing_collection).find = lambda *_: Cursor()
                    scope, socket, _ = self.websocket_scope(db)
                    await scope["main_ws"](socket, "client")
                    socket.close.assert_awaited_once_with(code=1011, reason="Internal websocket error")
                    scope["cleanup_client"].assert_not_awaited()
                await self.assert_nonblocking(invoke)

    async def test_websocket_duplicate_race_does_not_clean_up_other_connection(self):
        db = SimpleNamespace(organizations=SimpleNamespace(find_one=lambda *_: {}),
                             instagram_connections=SimpleNamespace(find=lambda *_: []),
                             whatsapp_connections=SimpleNamespace(find=lambda *_: []))
        scope, socket, state = self.websocket_scope(db, duplicate=True)
        await scope["main_ws"](socket, "client")
        socket.close.assert_awaited_once_with(code=1008, reason="Client ID already connected")
        scope["cleanup_client"].assert_not_awaited()
        self.assertFalse(state.active_clients)

    async def test_websocket_still_registers_all_lines_and_cleans_up(self):
        db = SimpleNamespace(organizations=SimpleNamespace(find_one=Mock(return_value={"wa_id": "legacy"})),
                             instagram_connections=SimpleNamespace(find=Mock(return_value=[])),
                             whatsapp_connections=SimpleNamespace(find=Mock(return_value=[{"wa_id": "one"}, {"wa_id": "two"}])))
        scope, socket, state = self.websocket_scope(db)
        presence = AsyncMock()
        with patch.dict("sys.modules", {"services.work_team_service": SimpleNamespace(record_team_presence=presence)}):
            await scope["main_ws"](socket, "client")
        self.assertEqual(state.whatsapp_clients["one"], {"client"})
        self.assertEqual(state.whatsapp_clients["two"], {"client"})
        self.assertEqual(state.client_metadata["client"]["org_id"], "org")
        self.assertEqual(db.whatsapp_connections.find.call_args.args[0]["org_id"], "org")
        presence.assert_awaited_once_with("org", "user")
        scope["cleanup_client"].assert_awaited_once_with("client")

    async def test_restock_index_failure_does_not_block_intent_startup(self):
        scope = functions("services/intent_queue_worker.py",
                          ["startup_intent_worker", "shutdown_intent_worker", "_restock_polling_loop"],
                          _polling_task=None, _restock_task=None, POLL_INTERVAL_SECONDS=15,
                          wait_for_mongo_available=AsyncMock(),
                          _ensure_indexes=Mock(), _recover_stuck_jobs=Mock())
        running = asyncio.Event()
        async def intent_loop():
            running.set()
            await asyncio.Event().wait()
        scope["_polling_loop"] = intent_loop
        product = SimpleNamespace(ensure_product_request_indexes=AsyncMock(side_effect=RuntimeError("index unavailable")),
                                  process_restock_outbox=AsyncMock())
        with patch.dict("sys.modules", {"services.product_request_service": product}):
            await scope["startup_intent_worker"]()
            try:
                await asyncio.wait_for(running.wait(), 0.5)
                await asyncio.sleep(0)
                product.ensure_product_request_indexes.assert_awaited_once()
                product.process_restock_outbox.assert_not_awaited()
            finally:
                await scope["shutdown_intent_worker"]()
        self.assertTrue(scope["_polling_task"].done())
        self.assertTrue(scope["_restock_task"].done())

    async def test_slow_restock_does_not_stop_intent_polling_and_is_cancelled(self):
        scope = functions("services/intent_queue_worker.py",
                          ["startup_intent_worker", "shutdown_intent_worker", "_restock_polling_loop", "_polling_loop"],
                          _polling_task=None, _restock_task=None, POLL_INTERVAL_SECONDS=0.001,
                          wait_for_mongo_available=AsyncMock(),
                          _ensure_indexes=Mock(), _recover_stuck_jobs=Mock())
        polled = asyncio.Event()
        blocked = asyncio.Event()
        cancelled = asyncio.Event()
        count = 0
        async def due_jobs():
            nonlocal count
            count += 1
            if count >= 3 and blocked.is_set():
                polled.set()
        async def restock():
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        scope["_process_due_jobs"] = due_jobs
        product = SimpleNamespace(ensure_product_request_indexes=AsyncMock(), process_restock_outbox=restock)
        with patch.dict("sys.modules", {"services.product_request_service": product}):
            await scope["startup_intent_worker"]()
            try:
                await asyncio.wait_for(polled.wait(), 0.5)
                product.ensure_product_request_indexes.assert_awaited_once()
            finally:
                await scope["shutdown_intent_worker"]()
        self.assertTrue(cancelled.is_set())

    async def test_restock_retries_indexes_with_backoff_before_dispatch(self):
        sleeps = []
        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
        scope = functions(
            "services/intent_queue_worker.py",
            ["_restock_polling_loop"],
            POLL_INTERVAL_SECONDS=15,
            wait_for_mongo_available=AsyncMock(),
        )
        scope["asyncio"] = SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)
        product = SimpleNamespace(ensure_product_request_indexes=AsyncMock(side_effect=[RuntimeError("index"), None]),
                                  process_restock_outbox=AsyncMock())
        with patch.dict("sys.modules", {"services.product_request_service": product}):
            with self.assertRaises(asyncio.CancelledError):
                await scope["_restock_polling_loop"]()
        self.assertEqual(sleeps, [60, 15])
        self.assertEqual(product.ensure_product_request_indexes.await_count, 2)
        product.process_restock_outbox.assert_awaited_once()


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(InboxOutageTests))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("Inbox outage regression checks passed")
