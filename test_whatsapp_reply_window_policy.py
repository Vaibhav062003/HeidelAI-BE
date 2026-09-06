"""Dependency-light checks for WhatsApp service and ad-pricing windows."""

import ast
import datetime as datetime_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from scripts.migrate_whatsapp_reply_windows import build_updates


ROOT = Path(__file__).parent


class _Logger:
    def warning(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


def _functions(path, names, namespace):
    source = (ROOT / path).read_text()
    module = ast.parse(source)
    nodes = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)
    return namespace


def test_ad_referral_does_not_extend_free_form_window():
    namespace = _functions(
        "services/webhook_service.py",
        {"calculate_whatsapp_reply_window"},
        {"datetime": datetime_module, "logger": _Logger()},
    )
    result = namespace["calculate_whatsapp_reply_window"]({
        "id": "wamid.1",
        "from": "919999999999",
        "timestamp": "1786015800",
        "referral": {"source_type": "ad", "ctwa_clid": "click"},
    })
    message_time = datetime.fromtimestamp(1786015800, timezone.utc)
    assert result["is_ad_referral"] is True
    assert result["expiry_time_utc"] == message_time + timedelta(hours=24)


def test_send_guard_and_meta_free_entry_expiry_are_independent():
    namespace = _functions(
        "services/wa_service.py",
        {"_utc_datetime", "ensure_whatsapp_reply_window_open", "whatsapp_free_entry_expiry"},
        {"Optional": Optional, "datetime": datetime, "timezone": timezone, "HTTPException": _HTTPException, "logger": _Logger()},
    )
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    guard = namespace["ensure_whatsapp_reply_window_open"]
    guard({"reply_window_ends_at": now + timedelta(seconds=1)}, "reply", now)
    try:
        guard({
            "reply_window_ends_at": now,
            "free_entry_window_ends_at": now + timedelta(hours=48),
        }, "reply", now)
    except _HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail["code"] == "whatsapp_reply_window_closed"
    else:
        raise AssertionError("closed free-form window was accepted")
    guard({"reply_window_ends_at": now}, "private", now)

    expiry = namespace["whatsapp_free_entry_expiry"]({
        "conversation": {
            "expiration_timestamp": str(int((now + timedelta(hours=72)).timestamp())),
            "origin": {"type": "referral_conversion"},
        },
        "pricing": {"category": "referral_conversion"},
    })
    assert expiry == now + timedelta(hours=72)


def test_migration_repairs_72_hour_value_and_backfills_active_ad_pricing():
    now = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
    inbound = now - timedelta(hours=2)
    reply = inbound + timedelta(minutes=10)
    updates = build_updates(
        [{
            "conversation_id": "whatsapp_customer_phone",
            "reply_window_ends_at": inbound + timedelta(hours=72),
        }],
        [{
            "conversation_id": "whatsapp_customer_phone",
            "platform": "whatsapp",
            "role": "customer",
            "timestamp": inbound,
            "raw_data": {"timestamp": str(int(inbound.timestamp())), "referral": {"source_type": "ad"}},
        }, {
            "conversation_id": "whatsapp_customer_phone",
            "platform": "whatsapp",
            "role": "agent",
            "mode": "reply",
            "status": "delivered",
            "timestamp": reply,
        }],
        now,
    )
    assert updates[0]["reply_window_ends_at"] == inbound + timedelta(hours=24)
    assert updates[0]["free_entry_window_ends_at"] == reply + timedelta(hours=72)
    repaired = {**updates[0], "reply_window_ends_at": updates[0]["reply_window_ends_at"]}
    assert build_updates([repaired], [{
        "conversation_id": "whatsapp_customer_phone",
        "role": "customer",
        "timestamp": inbound,
        "raw_data": {"timestamp": str(int(inbound.timestamp()))},
    }], now) == []


def test_status_webhook_processes_the_full_batch():
    source = (ROOT / "api/webhook_routes.py").read_text()
    status_start = source.index('statuses = value.get("statuses", [])')
    status_end = source.index('org_id = connection.get("org_id")', status_start)
    status_block = source[status_start:status_end]
    assert "status_payload=status" in status_block
    assert 'return {"status": "ok"}' not in status_block


if __name__ == "__main__":
    test_ad_referral_does_not_extend_free_form_window()
    test_send_guard_and_meta_free_entry_expiry_are_independent()
    test_migration_repairs_72_hour_value_and_backfills_active_ad_pricing()
    test_status_webhook_processes_the_full_batch()
    print("WhatsApp reply-window policy checks passed.")
