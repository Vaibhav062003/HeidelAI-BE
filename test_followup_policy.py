from datetime import datetime, timedelta, timezone

from services.followup_policy import can_auto_send, parse_customer_timeline


def test_followup_timing_and_reply_window_policy():
    now = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    assert parse_customer_timeline("in 3 hours", now) == now + timedelta(hours=3)
    assert parse_customer_timeline("now", now) is None
    assert not can_auto_send("whatsapp", now + timedelta(hours=3), None)
    assert can_auto_send("whatsapp", now + timedelta(hours=3), now + timedelta(hours=24))
    assert not can_auto_send("whatsapp", now + timedelta(hours=24), now + timedelta(hours=24))
    assert can_auto_send("whatsapp", now + timedelta(hours=60), now + timedelta(hours=72))
    assert not can_auto_send("whatsapp", now + timedelta(hours=72), now + timedelta(hours=72))
    assert not can_auto_send("email", now + timedelta(hours=1), now + timedelta(hours=24))


if __name__ == "__main__":
    test_followup_timing_and_reply_window_policy()
    print("follow-up policy ok")
