from pymongo import UpdateOne
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Internal Modules ===
from database import get_mongo_db
from services.messaging import instagram_service

db = get_mongo_db()
scheduled_collection = db["scheduled_messages"]

# mapping of platforms -> service instance
messaging_services = {
    "instagram": instagram_service.InstagramService(),
}

async def process_scheduled_messages():
    now = datetime.now(timezone.utc)

    pending = list(
        scheduled_collection.find(
            {"status": "pending", "scheduled_time": {"$lte": now}}
        ).sort("scheduled_time", 1)
    )

    if not pending:
        print(f"[{now}] No scheduled messages due.")
        return

    print(f"[{now}] Found {len(pending)} messages to send.")

    updates = []
    for msg in pending:
        platform = msg["platform"]
        sender_id = msg["sender_id"]
        receiver_id = msg["receiver_id"]
        message = msg["followup_text"]

        service = messaging_services.get(platform)
        if not service:
            print(f"No service found for platform: {platform}")
            updates.append(
                UpdateOne(
                    {"_id": msg["_id"]},
                    {
                        "$set": {
                            "status": "failed",
                            "error": f"No service found for platform: {platform}",
                            "processed_at": now
                        }
                    }
                )
            )
            continue

        try:
            await service.send_message(sender_id, receiver_id, message)
            updates.append(
                UpdateOne(
                    {"_id": msg["_id"]},
                    {
                        "$set": {
                            "status": "sent",
                            "sent_at": now,
                            "processed_at": now
                        }
                    }
                )
            )
            print(f"Sent message to {receiver_id} on {platform}")
        except Exception as e:
            error_msg = str(e)
            print(f"Failed to send to {receiver_id} on {platform}: {error_msg}")
            updates.append(
                UpdateOne(
                    {"_id": msg["_id"]},
                    {
                        "$set": {
                            "status": "failed",
                            "error": error_msg,
                            "processed_at": now
                        }
                    }
                )
            )

    if updates:
        try:
            result = scheduled_collection.bulk_write(updates)
            print(f"Updated {result.modified_count} message statuses")
        except Exception as e:
            print(f"Error updating message statuses: {e}")

def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_scheduled_messages, "interval", hours=1)
    scheduler.start()
    print("APScheduler started (interval = 1 hour).")
