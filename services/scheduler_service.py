import asyncio
from datetime import datetime, timezone

from loguru import logger

from database import get_mongo_db


db = get_mongo_db()


async def clean_expired_archives():
    """Find and clean all expired archived data entries."""
    try:
        now = datetime.now(timezone.utc)
        expired_docs = await asyncio.to_thread(lambda: list(db.archived_tracker.find({
            "expires_at": {"$lt": now},
            "status": "active",
        })))
        if not expired_docs:
            logger.info("No expired archived data entries found.")
            return
        logger.success(f"Found {len(expired_docs)} expired archive records. Starting cleanup...")
        for doc in expired_docs:
            await asyncio.to_thread(purge_archived_data, doc)
    except Exception as exc:
        logger.error(f"Error during clean_expired_archives: {exc}")


def purge_archived_data(doc: dict):
    """Delete specific archive data safely using archive_id."""
    org_id = doc.get("org_id")
    try:
        archive_id = doc.get("archive_id")
        archived_msgs_col = f"archived_messages_{org_id}_{archive_id}"
        archived_convs_col = f"archived_conversations_{org_id}_{archive_id}"
        archived_notes_col = f"archived_private_notes_{org_id}_{archive_id}"
        deleted_counts = {}

        message_result = db[archived_msgs_col].delete_many({})
        deleted_counts["messages"] = message_result.deleted_count
        db.drop_collection(archived_msgs_col)

        conv_result = db[archived_convs_col].delete_many({})
        deleted_counts["conversations"] = conv_result.deleted_count
        db.drop_collection(archived_convs_col)

        notes_result = db[archived_notes_col].delete_many({})
        deleted_counts["private_notes"] = notes_result.deleted_count
        db.drop_collection(archived_notes_col)

        db.archived_tracker.update_one(
            {"archive_id": archive_id},
            {"$set": {
                "status": "purged",
                "cleaned_at": datetime.now(timezone.utc),
                "deleted_counts": deleted_counts,
            }},
        )
        logger.success(
            f"org_id={org_id}, platform={doc['platform']}: "
            f"deleted {deleted_counts['messages']} messages and "
            f"{deleted_counts['conversations']} conversations (marked as purged)."
        )
    except Exception as exc:
        logger.error(f"Error purging archive for org_id={org_id}, platform={doc.get('platform')}: {exc}")
