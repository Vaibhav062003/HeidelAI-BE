from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Internal Modules ===
from database import get_mongo_db
from services.messaging import instagram_service

db = get_mongo_db()
scheduled_collection = db["scheduled_jobs"]

# mapping of platforms -> service instance
messaging_services = {
    "instagram": instagram_service.InstagramService(),
}

async def process_scheduled_messages():
    now = datetime.now(timezone.utc)

    pending = list(
        scheduled_collection.find(
            {"status": "pending", "send_time": {"$lte": now}}
        ).sort("send_time", 1)
    )

    if not pending:
        print(f"[{now}] No scheduled messages due.")
        return

    print(f"[{now}] Found {len(pending)} messages to send.")

    sent_ids = []
    for msg in pending:
        platform = msg["platform"]
        recipient_id = msg["recipient_id"]
        message = msg["message"]

        service = messaging_services.get(platform)
        if not service:
            print(f"No service found for platform: {platform}")
            continue

        try:
            await service.send_message(recipient_id, message)
            sent_ids.append(msg["_id"])
            print(f"Sent message to {recipient_id} on {platform}")
        except Exception as e:
            print(f"Failed to send to {recipient_id} on {platform}: {e}")

    if sent_ids:
        scheduled_collection.update_many(
            {"_id": {"$in": sent_ids}},
            {"$set": {"status": "sent", "sent_at": now}}
        )
        print(f"Marked {len(sent_ids)} messages as sent.")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_scheduled_messages, "interval", hours=1)
    scheduler.start()
    print("APScheduler started (interval = 1 hour).")
