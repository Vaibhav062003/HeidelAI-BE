"""Repair WhatsApp reply windows that were incorrectly extended for ads.

Dry-run is the default:

    python -m scripts.migrate_whatsapp_reply_windows
    python -m scripts.migrate_whatsapp_reply_windows --apply
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone



def _as_utc(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _message_time(message: dict):
    raw_timestamp = (message.get("raw_data") or {}).get("timestamp")
    if raw_timestamp is not None:
        try:
            return datetime.fromtimestamp(int(raw_timestamp), timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return _as_utc(message.get("timestamp"))


def _is_ad_referral(message: dict) -> bool:
    raw = message.get("raw_data") or {}
    referral = raw.get("referral") or (raw.get("context") or {}).get("referral") or {}
    return bool(referral.get("ctwa_clid") or str(referral.get("source_type") or "").lower() in {"ad", "post"})


def build_updates(conversations, messages, now=None) -> list[dict]:
    """Build deterministic, idempotent updates without touching MongoDB."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    latest_customer = {}
    referrals = defaultdict(list)
    business_messages = defaultdict(list)

    for message in messages:
        conversation_id = message.get("conversation_id")
        event_time = _message_time(message)
        if not conversation_id or not event_time:
            continue
        role = str(message.get("role") or "").lower()
        if role == "customer":
            if event_time > latest_customer.get(conversation_id, datetime.min.replace(tzinfo=timezone.utc)):
                latest_customer[conversation_id] = event_time
            if _is_ad_referral(message):
                referrals[conversation_id].append(event_time)
        elif role in {"agent", "ai"} and message.get("mode") != "private" and message.get("status") != "failed":
            business_messages[conversation_id].append(event_time)

    updates = []
    for conversation in conversations:
        conversation_id = conversation.get("conversation_id")
        customer_time = latest_customer.get(conversation_id)
        if not conversation_id or not customer_time:
            continue
        reply_ends_at = customer_time + timedelta(hours=24)
        current_reply = _as_utc(conversation.get("reply_window_ends_at"))
        current_free_entry = _as_utc(conversation.get("free_entry_window_ends_at"))
        free_entry_ends_at = None
        replies = sorted(business_messages.get(conversation_id, []))
        for referral_time in referrals.get(conversation_id, []):
            first_reply = next((sent_at for sent_at in replies if referral_time <= sent_at <= referral_time + timedelta(hours=24)), None)
            if first_reply:
                candidate = first_reply + timedelta(hours=72)
                if candidate > now and (free_entry_ends_at is None or candidate > free_entry_ends_at):
                    free_entry_ends_at = candidate

        if current_reply == reply_ends_at and (not free_entry_ends_at or current_free_entry and current_free_entry >= free_entry_ends_at):
            continue
        updates.append({
            "conversation_id": conversation_id,
            "observed_reply_window_ends_at": conversation.get("reply_window_ends_at"),
            "reply_window_ends_at": reply_ends_at,
            "free_entry_window_ends_at": free_entry_ends_at,
        })
    return updates


def migrate(apply=False) -> dict:
    from database import get_mongo_client, get_mongo_db
    from pymongo import UpdateOne

    db = get_mongo_db()
    org_ids = sorted(str(value) for value in db.whatsapp_connections.distinct(
        "org_id", {"is_active": {"$ne": False}},
    ) if value)
    report = {"apply": apply, "organizations": {}, "errors": []}
    plans = {}

    for org_id in org_ids:
        conversations = list(db[f"conversations_{org_id}"].find(
            {"platform": "whatsapp"},
            {"conversation_id": 1, "reply_window_ends_at": 1, "free_entry_window_ends_at": 1},
        ))
        messages = db[f"messages_{org_id}"].find(
            {"platform": "whatsapp"},
            {"conversation_id": 1, "role": 1, "mode": 1, "status": 1, "timestamp": 1, "raw_data.timestamp": 1, "raw_data.referral": 1, "raw_data.context.referral": 1},
        )
        updates = build_updates(conversations, messages)
        plans[org_id] = updates
        report["organizations"][org_id] = {
            "conversations": len(conversations),
            "updates": len(updates),
            "changes": updates,
        }

    if not apply:
        return report

    with get_mongo_client().start_session() as session:
        with session.start_transaction():
            for org_id, updates in plans.items():
                operations = []
                for update in updates:
                    observed = update["observed_reply_window_ends_at"]
                    query = {"conversation_id": update["conversation_id"], "platform": "whatsapp"}
                    if observed is None:
                        query["$or"] = [
                            {"reply_window_ends_at": {"$exists": False}},
                            {"reply_window_ends_at": None},
                        ]
                    else:
                        query["reply_window_ends_at"] = observed
                    changes = {"$set": {"reply_window_ends_at": update["reply_window_ends_at"]}}
                    if update["free_entry_window_ends_at"]:
                        changes["$max"] = {"free_entry_window_ends_at": update["free_entry_window_ends_at"]}
                    operations.append(UpdateOne(query, changes))
                if operations:
                    result = db[f"conversations_{org_id}"].bulk_write(operations, ordered=False, session=session)
                    report["organizations"][org_id]["matched"] = result.matched_count
                    report["organizations"][org_id]["modified"] = result.modified_count
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = migrate(apply=args.apply)
    print(json.dumps(report, indent=2, default=str))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
