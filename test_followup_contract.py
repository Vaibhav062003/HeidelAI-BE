import ast
import asyncio
from pathlib import Path


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _strings(node: ast.AST):
    return {
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def test_followup_routes_are_authenticated_and_lifecycle_is_atomic():
    routes = ast.parse(Path("api/ai_routes.py").read_text())
    for name in ["schedule_message", "get_followups", "update_followup_status", "cancel_next_followup"]:
        function = _function(routes, name)
        annotations = {argument.arg: argument.annotation for argument in function.args.args}
        assert isinstance(annotations.get("user"), ast.Name)
        assert annotations["user"].id == "CurrentUser"

    service = ast.parse(Path("services/followup_service.py").read_text())
    schedule = _function(service, "schedule_followup_message")
    assert {"org_id", "quote_id", "quote_value", "followup_text", "scheduled_time"} <= _strings(schedule)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(schedule)
    )

    transition = _function(service, "transition_scheduled_followup")
    assert {"org_id", "cancelled", "fulfilled", "pending"} <= _strings(transition)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(transition)
    )

    cancel_future = _function(service, "cancel_future_followup")
    assert {
        "org_id", "sequence_id", "sequence_step", "second_skipped_at",
        "future_cancelled_at", "cancelled",
    } <= _strings(cancel_future)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "insert_one"
        for node in ast.walk(cancel_future)
    )
    scheduler = _function(service, "_create_due_second_followups")
    for function in (cancel_future, scheduler):
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_sync_conversation_followup_status"
            for node in ast.walk(function)
        )

    cancellable = next(
        node.value for node in service.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "CANCELLABLE_FUTURE_STATUSES" for target in node.targets)
    )
    assert ast.literal_eval(cancellable) == {"pending", "failed"}

    processor = _function(service, "process_scheduled_messages")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "find_one_and_update"
        for node in ast.walk(processor)
    )
    assert "customer_facing" in _strings(processor)
    assert "Sales agent follow-up reminder" not in _strings(processor)

    customer_reply = _function(service, "handle_followup_customer_reply")
    assert {"pending", "failed", "processing", "customer_replied_before_followup"} <= _strings(customer_reply)
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_create_escalation"
        for node in ast.walk(customer_reply)
    )

    migration = Path("scripts/migrate_active_reminders.py").read_text()
    assert "python -m scripts.migrate_active_reminders" in migration
    assert '"customer_facing": False' in migration

    team = ast.parse(Path("services/team_service.py").read_text())
    assert {"org_id", "priority", "kind"} <= _strings(_function(team, "create_ai_escalation"))

    webhook = ast.parse(Path("api/webhook_routes.py").read_text())
    followup_calls = [
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "handle_followup_customer_reply"
    ]
    assert len(followup_calls) >= 5
    media_call = next(call for call in followup_calls if 980 <= call.lineno <= 1060)
    media_if = next(
        node for node in ast.walk(webhook)
        if isinstance(node, ast.If) and any(child is media_call for child in ast.walk(node.test))
    )
    assert any(isinstance(node, ast.Return) for node in media_if.body)

    background_upload = next(
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_task"
        and node.args
        and getattr(node.args[0], "id", None) == "upload_and_send_whatsapp_media_background"
    )
    assert any(call.lineno > background_upload.lineno for call in followup_calls)


def test_conversation_status_reconciles_after_cancel_scheduler_race():
    service = ast.parse(Path("services/followup_service.py").read_text())
    helper = _function(service, "_sync_conversation_followup_status")

    class Scheduled:
        active = False
        block_next_read = False

        def __init__(self):
            self.snapshot_taken = asyncio.Event()
            self.release_snapshot = asyncio.Event()

        async def find_one(self, *_args, **_kwargs):
            snapshot = self.active
            if self.block_next_read:
                self.block_next_read = False
                self.snapshot_taken.set()
                await self.release_snapshot.wait()
            return {"_id": "active"} if snapshot else None

    class Conversations:
        status = "follow-up"

        def __init__(self):
            self.writes = []

        async def update_one(self, _query, update):
            self.status = update["$set"]["status"]
            self.writes.append(self.status)

    class FakeDB:
        scheduled_messages = Scheduled()
        conversations = Conversations()

        def __getitem__(self, _name):
            return self.conversations

    fake_db = FakeDB()
    namespace = {"db": fake_db}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), "<followup-status>", "exec"), namespace)

    async def race():
        assert await namespace["_sync_conversation_followup_status"]("org", "chat") == "open"
        fake_db.scheduled_messages.active = True
        assert await namespace["_sync_conversation_followup_status"]("org", "chat") == "follow-up"

        fake_db.conversations.writes.clear()
        fake_db.scheduled_messages.block_next_read = True
        stale_scheduler = asyncio.create_task(namespace["_sync_conversation_followup_status"]("org", "chat"))
        await fake_db.scheduled_messages.snapshot_taken.wait()
        fake_db.scheduled_messages.active = False
        assert await namespace["_sync_conversation_followup_status"]("org", "chat") == "open"
        fake_db.scheduled_messages.release_snapshot.set()
        await stale_scheduler

        assert fake_db.conversations.writes[-3:] == ["open", "follow-up", "open"]
        assert fake_db.conversations.status == "open"

    asyncio.run(race())


if __name__ == "__main__":
    test_followup_routes_are_authenticated_and_lifecycle_is_atomic()
    test_conversation_status_reconciles_after_cancel_scheduler_race()
    print("Follow-up contract check passed.")
