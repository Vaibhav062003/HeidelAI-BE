import asyncio
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
# === Internal Modules ===
from database import get_mongo_db
from core.services import services
from services.platforms import instagram_service, whatsapp_service
from services.common_service import close_conversation
from services.scheduler_service import clean_expired_archives
from services.followup_service import process_scheduled_messages as drain_scheduled_followups
from services.ai_policy_service import resume_expired_ai_pauses
from pymongo.errors import PyMongoError
# from services.websocket_service import broadcast_on_stats_update
from loguru import logger



db = get_mongo_db()
_scheduler = None
# mapping of platforms -> service instance
messaging_services = {
    "instagram": instagram_service.InstagramService(),
    "whatsapp": whatsapp_service.WhatsAppService()
}

async def process_scheduled_messages():
    try:
        await drain_scheduled_followups(messaging_services)
        from services.sales_workflow_service import resume_completed_work
        await resume_completed_work()
    except PyMongoError as exc:
        logger.warning("Scheduled follow-up polling deferred: MongoDB unavailable ({})", type(exc).__name__)


async def process_inactivity_for_org(org_id: str):
    
    if org_id in services.NO_AUTO_CLOSE_ORGS:
        logger.info(f"[ORG ...{org_id[-5:-1]}] Skipping inactivity processing (opted out)")
        return

    conv_col = db[f"conversations_{org_id}"]
    now = datetime.now(timezone.utc)

    convs = await asyncio.to_thread(lambda: list(conv_col.find({
        "status": "open",
        "last_sender": "agent",
        "next_action_at": {"$lte": now}
    })))

    if not convs:
        return 

    logger.success(f"[ORG {org_id[:6]}...] Found {len(convs)} inactive conversations to process")

    for conv in convs:
        stage = conv.get("reminder_stage", 0)
        conv_id = conv["conversation_id"]

        if stage == 0:
            await asyncio.to_thread(
                conv_col.update_one,
                {"conversation_id": conv_id},
                {"$set": {
                    "reminder_stage": 1,
                    "next_action_at": now + timedelta(hours=6),
                }},
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
            await asyncio.to_thread(
                conv_col.update_one,
                {"conversation_id": conv_id},
                {"$set": {
                    "reminder_stage": 2,
                    "next_action_at": now + timedelta(hours=6),
                }},
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
    orgs = await asyncio.to_thread(lambda: list(db.organizations.find({}, {"org_id": 1})))
    for org in orgs:
        org_id = org["org_id"]
        await process_inactivity_for_org(org_id)

async def update_ai_status(org_type: str = "all"):
    resumed = await resume_expired_ai_pauses()
    if resumed:
        logger.info(f"Re-enabled AI for {resumed} expired conversation pause(s); no customer message was sent.")
            


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    from services.cloudinary_service import (
        cleanup_expired_audio_notes,
        cleanup_expired_call_recordings,
        cleanup_orphaned_call_recording_parts,
        process_call_recording_jobs,
    )
    from services.oos_service import process_pending_oos_sends, process_expired_oos_requests, process_pending_oos_reply_steps
    from services.push_notification_service import process_pending_member_device_revocations, process_pending_push_notifications
    from services.pricing_service import process_pending_pricing_escalations
    from services.product_sheet_sync import reconcile_active_product_sheets
    from services.sales_offer_service import process_pending_sales_offer_audits
    from services.tally_service import check_tally_contact_sync_health, cleanup_expired_tally_pdfs, process_tally_payment_reminders, reconcile_stale_tally_jobs
    from services.shopify_service import process_shopify_reminders, process_shopify_syncs
    from services.shopify_growth import process_campaigns
    from services.shopify_retargeting import process_retargeting
    from services.shopify_reporting import process_weekly_reports

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
    scheduler.add_job(update_ai_status, "interval", minutes=1, next_run_time=datetime.now(timezone.utc), max_instances=1, coalesce=True)
    # OOS: send pending dealer messages when business hours start (check every 15 min)
    scheduler.add_job(process_pending_oos_sends, "interval", minutes=15)
    scheduler.add_job(process_pending_oos_reply_steps, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_sales_offer_audits, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(reconcile_stale_tally_jobs, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_tally_payment_reminders, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(check_tally_contact_sync_health, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_shopify_reminders, "interval", seconds=30, next_run_time=datetime.now(timezone.utc), max_instances=1, coalesce=True)
    scheduler.add_job(process_shopify_syncs, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_retargeting, "interval", seconds=30, max_instances=1, coalesce=True)
    scheduler.add_job(process_weekly_reports, "interval", minutes=15, max_instances=1, coalesce=True)
    scheduler.add_job(process_campaigns, "interval", seconds=30, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_pricing_escalations, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(reconcile_active_product_sheets, "interval", minutes=15, max_instances=1, coalesce=True)
    # OOS: expire stale requests and escalate (check every hour)
    scheduler.add_job(process_expired_oos_requests, "interval", hours=1)
    scheduler.add_job(process_pending_push_notifications, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(process_pending_member_device_revocations, "interval", minutes=1, max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_expired_audio_notes, "cron", hour=3, minute=30, max_instances=1, coalesce=True)
    scheduler.add_job(process_call_recording_jobs, "interval", seconds=10, max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_expired_call_recordings, "interval", minutes=5, max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_orphaned_call_recording_parts, "cron", hour=4, minute=0, max_instances=1, coalesce=True)
    scheduler.add_job(cleanup_expired_tally_pdfs, "cron", hour=3, minute=45, max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    logger.success("APScheduler started (interval = 6 hours).")
    return scheduler


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
