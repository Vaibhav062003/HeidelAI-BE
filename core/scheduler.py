from pymongo import UpdateOne
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import Optional
# === Internal Modules ===
from database import get_mongo_db
from core.services import services
from services.platforms import instagram_service, whatsapp_service
from services.common_service import close_conversation
from services.scheduler_service import clean_expired_archives
from services.agent_service import send_to_agent
from services.wa_service import send_whatsapp_message
# from services.websocket_service import broadcast_on_stats_update
from loguru import logger
from config.settings import LEE_ORG_ID



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

ENSURED_INDEX_COLLECTIONS = set()

async def trigger_ai_invocation_after_reenabling(org_id: str, conv: dict, wa_id: Optional[str]):
    conversation_id = conv["conversation_id"]
    try:
        logger.info(f"Triggering AI auto-activation invocation for conversation: {conversation_id} in org: {org_id}")

        chatbot_response = await send_to_agent(
            conversation_id=conversation_id,
            user_input="hi",
            persona=conv.get("persona", "fallback"),
            location=conv.get("location", "india"),
            customer_name=conv.get("customer_name", "Customer"),
            org_id=org_id,
            source="webhook",
        )
        final_response = chatbot_response.get("final_response")
        
        if final_response:
            resolved_wa_id = conv.get("whatsapp_id") or wa_id
            if resolved_wa_id:
                await send_whatsapp_message({
                    "conversation_id": conversation_id,
                    "content": final_response,
                    "sender_id": resolved_wa_id,
                }, org_id, "ai")
                logger.success(f"Successfully re-enabled AI and sent auto-invoked message to {conversation_id} for org {org_id}")
            else:
                logger.error(f"Cannot send WhatsApp message after re-enabling AI, WABA ID not found for org {org_id}")
        else:
            logger.warning(f"No response generated from agent for re-enabled AI conversation {conversation_id}")
    except Exception as e:
        logger.error(f"Error during AI invocation after re-enabling for {conversation_id} (org {org_id}): {e}", exc_info=True)


async def update_ai_status(org_type: str = "all"):
    logger.info(f"processing auto ai status for type: {org_type}")
    try:
        now = datetime.now(timezone.utc)
        
        # Build query to fetch only required organizations
        query = {}
        if org_type == "lee" and LEE_ORG_ID:
            query = {"org_id": LEE_ORG_ID}
        elif org_type == "others" and LEE_ORG_ID:
            query = {"org_id": {"$ne": LEE_ORG_ID}}
            
        org_docs = list(await asyncio.to_thread(
            lambda: list(db.organizations.find(query, {"org_id": 1, "wa_id": 1}))
        ))
        
        if not org_docs:
            return

        # Pre-fetch WABA IDs from all connections to avoid N database queries in loops
        org_ids = [org["org_id"] for org in org_docs]
        connections = list(await asyncio.to_thread(
            lambda: list(db.whatsapp_connections.find({"org_id": {"$in": org_ids}}, {"org_id": 1, "wa_id": 1}))
        ))
        org_to_wa_id = {conn["org_id"]: conn["wa_id"] for conn in connections if "org_id" in conn and "wa_id" in conn}

        async def process_single_org(org_doc: dict):
            org_id = org_doc["org_id"]
            conv_col = db[f"conversations_{org_id}"]
            col_name = conv_col.name
            
            # Ensure optimal query index exists (only once per runtime session)
            if col_name not in ENSURED_INDEX_COLLECTIONS:
                await asyncio.to_thread(
                    conv_col.create_index, 
                    [("auto_ai_disabled", 1), ("auto_ai_disabled_until", 1)]
                )
                ENSURED_INDEX_COLLECTIONS.add(col_name)

            # Query conversations that need AI re-enabling with selective projection
            conversations_to_update = await asyncio.to_thread(
                lambda: list(conv_col.find(
                    {
                        "auto_ai_disabled": True,
                        "auto_ai_disabled_until": {"$lte": now}
                    },
                    {
                        "conversation_id": 1,
                        "persona": 1,
                        "location": 1,
                        "customer_name": 1,
                        "whatsapp_id": 1
                    }
                ))
            )

            if not conversations_to_update:
                return 0

            conv_ids = [c["conversation_id"] for c in conversations_to_update]

            # Non-blocking update of the conversations
            result = await asyncio.to_thread(
                conv_col.update_many,
                {
                    "conversation_id": {"$in": conv_ids}
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

            # Trigger AI auto-invocation for each conversation that was updated
            wa_id_fallback = org_to_wa_id.get(org_id) or org_doc.get("wa_id")
            for conv in conversations_to_update:
                asyncio.create_task(
                    trigger_ai_invocation_after_reenabling(org_id, conv, wa_id_fallback)
                )

            return result.modified_count

        # Run organization updates concurrently
        tasks = [process_single_org(org) for org in org_docs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        total_updated = 0
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Error during parallel update: {res}")
            elif isinstance(res, int):
                total_updated += res
        
        if total_updated > 0:
            logger.info(f"updated {total_updated} conversations (type: {org_type})")
    except Exception as e:
        logger.error(f"Error updating ai status for type {org_type}: {e}")
            


def start_scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_time_based_tasks, "interval", hours=6, next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(update_ai_status, "interval", minutes=5, args=["lee"], next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(update_ai_status, "interval", hours=1, args=["others"], next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    logger.success("APScheduler started (interval = 6 hours).")

