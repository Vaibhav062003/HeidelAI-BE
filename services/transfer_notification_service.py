from database import get_mongo_db


def ensure_transfer_notification_indexes() -> None:
    notifications = get_mongo_db().transfer_notifications
    notifications.create_index("notification_id", unique=True)
    notifications.create_index([("org_id", 1), ("recipient_id", 1), ("created_at", -1)])
    notifications.create_index([("org_id", 1), ("recipient_id", 1), ("conversation_id", 1), ("read_at", 1)])
    notifications.create_index("expires_at", expireAfterSeconds=0)
