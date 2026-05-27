from pymongo import UpdateOne
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Internal Modules ===
from database import get_mongo_db
from core.services import services
from services.platforms import instagram_service, whatsapp_service
from services.common_service import close_conversation
from services.scheduler_service import clean_expired_archives
# from services.websocket_service import broadcast_on_stats_update
from loguru import logger


db = get_mongo_db()
scheduled_collection = db["scheduled_messages"]

# mapping of platforms -> service instance
messaging_services = {
    "instagram": instagram_service.InstagramService(),
    "whatsapp": whatsapp_service.WhatsAppService()
}

async def process_scheduled_messages():
    now = datetime.now(timezone.utc)

    pending = list(
        scheduled_collection.find(
            {"status": "pending", "scheduled_time": {"$lte": now}}
        ).sort("scheduled_time", 1)
    )

    if not pending:
        logger.success("No scheduled messages due.")
        return

    logger.success(f"Found {len(pending)} messages to send.")

    updates = []
    for msg in pending:
        platform = msg["platform"]
        sender_id = msg["sender_id"]
        receiver_id = msg["receiver_id"]
        message = msg["followup_text"]
        conversation_id = msg["conversation_id"]
        org_id = msg["org_id"]

        service = messaging_services.get(platform)
        if not service:
            logger.error(f"No service found for platform: {platform}")
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
            send_msg_response = await service.send_message(
                sender_id=sender_id, 
                recipient_id=receiver_id, 
                message=message,
                # These kwargs are vital for WhatsApp, ignored by Instagram
                org_id=org_id,
                conversation_id=conversation_id
            )

            logger.info(f"send_message response for {receiver_id} on {platform}: {send_msg_response}")

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
            logger.success(f"Sent message to {receiver_id} on {platform}")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to send to {receiver_id} on {platform}: {error_msg}")
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
            logger.success(f"Updated {result.modified_count} message statuses")
            
            # Broadcast updates for all affected orgs
            # org_ids = {msg["org_id"] for msg in pending}
            # for org_id in org_ids:
            #     await broadcast_on_stats_update(org_id)
                
        except Exception as e:
            logger.error(f"Error updating message statuses: {e}")

async def process_inactivity_for_org(org_id: str):
    
    if org_id in services.NO_AUTO_CLOSE_ORGS:
        logger.info(f"[ORG ...{org_id[-5:-1]}] Skipping inactivity processing (opted out)")
        return

    conv_col = db[f"conversations_{org_id}"]
    now = datetime.now(timezone.utc)

    convs = list(conv_col.find({
        "status": "open",
        "last_sender": "agent",
        "next_action_at": {"$lte": now}
    }))

    if not convs:
        return 

    logger.success(f"[ORG {org_id[:6]}...] Found {len(convs)} inactive conversations to process")

    for conv in convs:
        stage = conv.get("reminder_stage", 0)
        conv_id = conv["conversation_id"]

        if stage == 0:
            conv_col.update_one(
                {"conversation_id": conv_id},
                {
                    "$set": {
                        "reminder_stage": 1,
                        "next_action_at": now + timedelta(hours=6)
                    }
                }
            )
            logger.info(f"[ORG {org_id[:6]}...] Stage 0 → 1 for {conv_id}")
            continue

            # await store_instagram_message(
            #     org_id=org_id,
            #     conversation_id=conv["conversation_id"],
            #     content=content,
            #     sender_id="system",
            #     sender_name="Auto Follow-up",
            #     sender_username="auto_followup",
            #     recipient_id=conv["instagram_id"],
            #     role="agent",
            #     is_auto_followup=True
            # )
            # await dummy_send_message_to_user()

        elif stage == 1:
            # Second 12h inactivity follow-up
            conv_col.update_one(
                {"conversation_id": conv_id},
                {
                    "$set": {
                        "reminder_stage": 2,
                        "next_action_at": now + timedelta(hours=6)
                    }
                }
            )
            logger.info(f"[ORG {org_id[:6]}...] Stage 1 → 2 for {conv_id}")
            continue
            # content = "We're still here if you need assistance! 😊"
            # await store_instagram_message(
            #     org_id=org_id,
            #     conversation_id=conv["conversation_id"],
            #     content=content,
            #     sender_id="system",
            #     sender_name="Auto Follow-up",
            #     sender_username="auto_followup",
            #     recipient_id=conv["instagram_id"],
            #     role="agent",
            #     is_auto_followup=True
            # )
            # await dummy_send_message_to_user()

        elif stage == 2:
            # Close conversation
            await close_conversation(org_id, conv_id, "Closed due to inactivity")
            
            logger.info(f"[ORG {org_id[:6]}...] Closed conversation {conv_id} due to inactivity")
            continue

async def process_time_based_tasks():

    # Process scheduled messages
    logger.info("Processing scheduled messages...")
    await process_scheduled_messages()

    logger.info("Cleaning expired archives...")
    await clean_expired_archives()

    logger.info("Processing inactivity follow-ups and closures...")
    # Process inactivity follow-ups & closures for all orgs
    orgs = db.organizations.find({}, {"org_id": 1})
    for org in orgs:
        org_id = org["org_id"]
        await process_inactivity_for_org(org_id)

import asyncio

async def update_ai_status():
    logger.info("processing auto ai status")
    try:
        now = datetime.now(timezone.utc)
        
        # 1. Non-blocking retrieval of organizations
        org_docs = list(await asyncio.to_thread(lambda: list(db.organizations.find({}, {"org_id": 1, "wa_id": 1}))))
        
        total_updated = 0

        for org in org_docs:
            org_id = org["org_id"]
            conv_col = db[f"conversations_{org_id}"]
            # Ensure optimal query index exists (idempotent, fast in MongoDB)
            await asyncio.to_thread(conv_col.create_index, [("auto_ai_disabled", 1), ("auto_ai_disabled_until", 1)])

            # Non-blocking update of the conversations
            result = await asyncio.to_thread(
                conv_col.update_many,
                {
                    "auto_ai_disabled": True,
                    "auto_ai_disabled_until": {"$lte": now}
                },
                {
                    "$set": {
                        "is_ai_enabled": True,
                        "auto_ai_disabled": False
                    },
                    "$unset": {
                        "auto_ai_disabled_until": ""
                    }
                }
            )

            total_updated += result.modified_count
        
        if total_updated > 0:
            logger.info(f"updated {total_updated} conversations")
    except Exception as e:
        logger.error(f"Error updating ai status: {e}")
            

def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_time_based_tasks, "interval", hours=6, next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(update_ai_status, "interval", minutes=1, next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    logger.success("APScheduler started (interval = 6 hours).")
