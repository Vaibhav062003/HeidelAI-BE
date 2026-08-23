"""Copy active, org-scoped legacy reminders into scheduled_messages.

Run once after deployment: python -m scripts.migrate_active_reminders
Unscoped legacy rows are deliberately skipped because tenant ownership cannot
be proven safely.
"""

import asyncio
from datetime import datetime, timezone

from database import get_async_mongo_db


ACTIVE = {"PENDING", "OVERDUE", "SNOOZED"}


async def migrate() -> tuple[int, int]:
    db = get_async_mongo_db()
    target = db.scheduled_messages
    await target.create_index("legacy_reminder_id", unique=True, sparse=True)
    migrated = skipped = 0
    cursor = db.reminders.find({"status": {"$in": list(ACTIVE)}, "org_id": {"$type": "string"}})
    async for reminder in cursor:
        org_id = reminder.get("org_id")
        conversation_id = reminder.get("conversation_id") or reminder.get("conversationId")
        conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}) if conversation_id else None
        scheduled_time = reminder.get("dateTime") or reminder.get("trigger_time")
        if not conversation or not isinstance(scheduled_time, datetime):
            skipped += 1
            continue
        title = " ".join(str(reminder.get("title") or "Sales-agent reminder").split())
        notes = " ".join(str(reminder.get("notes") or "").split())
        text = f"{title}: {notes}".strip(": ")[:600]
        now = datetime.now(timezone.utc)
        result = await target.update_one(
            {"legacy_reminder_id": str(reminder["_id"])},
            {"$setOnInsert": {
                "legacy_reminder_id": str(reminder["_id"]),
                "org_id": org_id,
                "conversation_id": conversation_id,
                "platform": conversation.get("platform") or conversation_id.split("_", 1)[0],
                "sender_id": conversation.get("whatsapp_id") or conversation.get("instagram_id"),
                "receiver_id": conversation.get("customer_id"),
                "followup_text": text,
                "scheduled_time": scheduled_time,
                "delivery_mode": "agent_reminder",
                "customer_facing": False,
                "status": "pending",
                "source": "legacy_reminder",
                "created_by": reminder.get("agent_id"),
                "created_by_name": reminder.get("agent_name") or "Team member",
                "created_at": reminder.get("created_at") or now,
                "updated_at": now,
            }},
            upsert=True,
        )
        migrated += int(result.upserted_id is not None)
    return migrated, skipped


if __name__ == "__main__":
    imported, ignored = asyncio.run(migrate())
    print(f"Imported {imported} active reminder(s); skipped {ignored} unsafe or incomplete row(s).")
