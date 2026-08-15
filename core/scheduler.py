from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from typing import Optional
# === Internal Modules ===
from database import get_mongo_db
from core.services import services
from services.platforms import instagram_service, whatsapp_service
from services.common_service import close_conversation
from services.scheduler_service import clean_expired_archives
from services.followup_service import process_scheduled_messages as drain_scheduled_followups
from services.agent_service import send_to_agent
from services.wa_service import send_whatsapp_message
# from services.websocket_service import broadcast_on_stats_update
from loguru import logger
from config.settings import LEE_DEALER_ORG_ID



db = get_mongo_db()
# mapping of platforms -> service instance
messaging_services = {
    "instagram": instagram_service.InstagramService(),
    "whatsapp": whatsapp_service.WhatsAppService()
}

async def process_scheduled_messages():
    await drain_scheduled_followups(messaging_services)

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
        # Re-verify org-level AI status before invocation (guards against org AI being disabled
        # after process_single_org passed the org check but before this background task runs)
        org_doc = await asyncio.to_thread(
            lambda: db.organizations.find_one({"org_id": org_id}, {"is_ai_enabled": 1})
        )
        if not org_doc or not org_doc.get("is_ai_enabled", False):
            logger.info(f"Skipping AI auto-activation for {conversation_id}: org-level AI is disabled for org {org_id}.")
            return

        # Re-verify conversation-level AI status to prevent race conditions
        conv_col = db[f"conversations_{org_id}"]
        latest_conv = await asyncio.to_thread(
            lambda: conv_col.find_one({"conversation_id": conversation_id}, {"is_ai_enabled": 1, "persona": 1, "location": 1, "customer_name": 1, "whatsapp_id": 1})
        )
        
        if not latest_conv or not latest_conv.get("is_ai_enabled", True):
            logger.info(f"Skipping AI auto-activation for {conversation_id}: AI is currently disabled.")
            return

        logger.info(f"Triggering AI auto-activation invocation for conversation: {conversation_id} in org: {org_id}")

        chatbot_response = await send_to_agent(
            conversation_id=conversation_id,
            user_input="hi",
            persona=latest_conv.get("persona", "fallback"),
            location=latest_conv.get("location", "india"),
            customer_name=latest_conv.get("customer_name", "Customer"),
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


# Keep strong references to background tasks to prevent GC
_background_tasks = set()


async def update_ai_status(org_type: str = "all"):
    logger.info(f"processing auto ai status for type: {org_type}")
    try:
        now = datetime.now(timezone.utc)
        
        # Build query to fetch only required organizations
        query = {}
        if org_type == "lee":
            if not LEE_DEALER_ORG_ID:
                logger.info("Skipping update_ai_status for 'lee' org type because LEE_DEALER_ORG_ID is not configured.")
                return
            query = {"org_id": LEE_DEALER_ORG_ID}
        elif org_type == "others":
            if LEE_DEALER_ORG_ID:
                query = {"org_id": {"$ne": LEE_DEALER_ORG_ID}}
            else:
                query = {}
        elif org_type == "all":
            query = {}
            
        # Also filter for orgs with AI enabled at the org level — no point processing orgs with AI off
        query["is_ai_enabled"] = True
        org_docs = list(await asyncio.to_thread(
            lambda: list(db.organizations.find(query, {"org_id": 1, "wa_id": 1, "is_ai_enabled": 1}))
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
            # is_ai_enabled is already True here because of the DB-level filter above,
            # but guard defensively in case the doc was fetched before an update.
            if not org_doc.get("is_ai_enabled", False):
                logger.info(f"Skipping auto AI status check for org {org_id} because is_ai_enabled is False.")
                # Clean up any stale auto_ai_disabled flags on conversations for this org
                conv_col_cleanup = db[f"conversations_{org_id}"]
                await asyncio.to_thread(
                    conv_col_cleanup.update_many,
                    {"auto_ai_disabled": True},
                    {"$set": {"auto_ai_disabled": False}, "$unset": {"auto_ai_disabled_until": ""}}
                )
                return 0

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

            # Atomic check-and-update for each conversation to avoid read-then-write race
            updated_conversations = []
            for conv in conversations_to_update:
                updated_doc = await asyncio.to_thread(
                    lambda: conv_col.find_one_and_update(
                        {
                            "conversation_id": conv["conversation_id"],
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
                )
                if updated_doc:
                    updated_conversations.append(conv)

            if not updated_conversations:
                return 0

            # Trigger AI auto-invocation for each conversation that was successfully updated
            wa_id_fallback = org_to_wa_id.get(org_id) or org_doc.get("wa_id")
            
            # Bounded task concurrency (limit to 10 concurrent sends)
            sem = asyncio.Semaphore(10)
            async def sem_task(conv_data):
                async with sem:
                    await trigger_ai_invocation_after_reenabling(org_id, conv_data, wa_id_fallback)

            for conv in updated_conversations:
                task = asyncio.create_task(sem_task(conv))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)

            return len(updated_conversations)

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
    from services.cloudinary_service import cleanup_expired_audio_notes
    from services.oos_service import process_pending_oos_sends, process_expired_oos_requests, process_pending_oos_reply_steps
    from services.push_notification_service import process_pending_member_device_revocations, process_pending_push_notifications
    from services.pricing_service import process_pending_pricing_escalations
    from services.sales_offer_service import process_pending_sales_offer_audits

    scheduler = AsyncIOScheduler()
    scheduler.add_job(process_time_based_tasks, "interval", hours=6, next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(
        process_scheduled_messages,
        "interval",
        seconds=30,
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(update_ai_status, "interval", minutes=5, args=["lee"], next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(update_ai_status, "interval", hours=1, args=["others"], next_run_time=datetime.now(timezone.utc))
    # OOS: send pending dealer messages when business hours start (check every 15 min)
    scheduler.add_job(process_pending_oos_sends, "interval", minutes=15)
    scheduler.add_job(process_pending_oos_reply_steps, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_sales_offer_audits, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_pricing_escalations, "interval", minutes=1, max_instances=1, coalesce=True)
    # OOS: expire stale requests and escalate (check every hour)
    scheduler.add_job(process_expired_oos_requests, "interval", hours=1)
    scheduler.add_job(process_pending_push_notifications, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_member_device_revocations, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_expired_audio_notes, "cron", hour=3, minute=30, max_instances=1, coalesce=True)
    scheduler.start()
    logger.success("APScheduler started (interval = 6 hours).")
