from datetime import datetime, timedelta, timezone

import dateparser


FIRST_FOLLOWUP_DELAY = timedelta(hours=3)
SECOND_FOLLOWUP_DELAY = timedelta(hours=6)
CLAIM_TIMEOUT = timedelta(minutes=10)
REPLY_WINDOW_MARGIN = timedelta(minutes=5)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def parse_customer_timeline(expression: str | None, now: datetime | None = None) -> datetime | None:
    if not expression:
        return None
    reference = as_utc(now or datetime.now(timezone.utc))
    parsed = dateparser.parse(
        expression,
        settings={
            "TIMEZONE": "UTC",
            "TO_TIMEZONE": "UTC",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": reference,
        },
    )
    if not parsed:
        return None
    parsed = as_utc(parsed)
    return parsed if parsed > reference + timedelta(minutes=5) else None


def can_auto_send(platform: str, scheduled_time: datetime, reply_window_ends_at: datetime | None) -> bool:
    if platform not in {"whatsapp", "instagram"}:
        return False
    if not reply_window_ends_at:
        return False
    return as_utc(scheduled_time) < as_utc(reply_window_ends_at) - REPLY_WINDOW_MARGIN
