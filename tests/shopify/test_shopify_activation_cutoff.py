"""Run: python3 -B tests/shopify/test_shopify_activation_cutoff.py. No network."""
import asyncio
import copy
from datetime import timedelta

from test_shopify_worker_runtime import (
    FakeCollection, FakeDB, NOW, _configured_send_db, _prepare_send, _value,
    async_noop, checkout, load_service,
)


class Collection(FakeCollection):
    async def insert_one(self, document):
        self.documents.append(copy.deepcopy(document))


async def main():
    s = load_service()
    cutoff = NOW - timedelta(minutes=10)
    old = cutoff - timedelta(seconds=1)
    assert s._recovery_activity_time(NOW, cutoff, NOW) == NOW
    assert s._recovery_activity_time(cutoff, cutoff, cutoff) == cutoff
    for updated, created, enabled in [(old, old, cutoff), (NOW, old, cutoff),
                                      (NOW, None, cutoff), (NOW, NOW, None)]:
        assert s._recovery_activity_time(updated, enabled, created) is None
    assert s._recovery_activity_time(NOW.isoformat(), cutoff.isoformat(),
                                     NOW.astimezone().isoformat()) == NOW

    # Historical imports and delayed/replayed checkout webhooks stay historical,
    # including an old checkout whose latest change happened after activation.
    connection = {"org_id": "org", "shop": "shop.test", "automation_enabled": True,
                  "automation_enabled_at": cutoff, "order_notifications_enabled": True,
                  "order_notifications_enabled_at": cutoff,
                  "order_templates": {"received": {"status": "APPROVED"}}}
    payload = {"id": 1, "created_at": old.isoformat(), "updated_at": NOW.isoformat(),
               "phone": "+14155550100", "line_items": [{"title": "Board", "quantity": 1}]}
    assert s._checkout_from_webhook("org", "shop.test", payload, connection)["reminder_status"] == "historical"
    node = {"id": "cart", "createdAt": old.isoformat(), "updatedAt": NOW.isoformat()}
    document = s._checkout_from_graphql("org", "shop.test", node,
        historical=not s._recovery_activity_time(node["updatedAt"], cutoff, node["createdAt"]))
    assert document["reminder_status"] == "historical" and document["reminder_due_at"] is None
    payload.pop("created_at")
    assert s._checkout_from_webhook("org", "shop.test", payload, connection)["provider_created_at"] is None

    # Existing pending/retry/claimed jobs cannot bypass the send-time guard.
    for source in (None, "storefront_cart"):
        s = load_service()
        item = checkout("old", provider_created_at=old, provider_updated_at=NOW,
                        reminder_status="sending", claim_token="claim", source=source)
        database = _configured_send_db(FakeCollection([item]))
        database.shopify_connections.documents[0]["automation_enabled_at"] = cutoff
        _prepare_send(s, database)
        await s._send_claimed_checkout(item.copy(), "claim")
        stored = database.shopify_checkouts.documents[0]
        assert stored["reminder_status"] == "historical", stored
        assert "provider_started_at" not in stored

    # A cutoff moved while preparing a valid cart is rechecked before submission.
    s = load_service()
    item = checkout("race", reminder_status="sending", claim_token="claim")
    database = _configured_send_db(FakeCollection([item]))
    _prepare_send(s, database)
    async def refresh(item, *_args):
        database.shopify_connections.documents[0]["automation_enabled_at"] = NOW
        return item
    s._refresh_checkout_before_send = refresh
    await s._send_claimed_checkout(item.copy(), "claim")
    assert database.shopify_checkouts.documents[0]["reminder_status"] == "historical"
    assert "provider_started_at" not in database.shopify_checkouts.documents[0]

    # Both the order's original date and the event's original date must qualify.
    for created, event_time in [(old, NOW), (NOW, old), (None, NOW), (NOW, NOW)]:
        s = load_service()
        notifications = Collection()
        s.db = FakeDB(shopify_order_notifications=notifications, shopify_orders=Collection([
            {"org_id": "org", "provider_id": "order", "provider_created_at": created}]))
        await s._queue_order_notification(connection, "received", "order", event_time)
        assert bool(notifications.documents) == (created == NOW and event_time == NOW)
        if created != NOW or event_time != NOW:
            job = {"_id": "job", "org_id": "org", "order_provider_id": "order", "event": "received",
                   "event_at": event_time, "status": "sending", "claim_token": "claim"}
            notifications.documents = [job.copy()]
            s.db.shopify_connections.documents = [{**connection, "status": "connected",
                "order_templates": {"received": {"name": "order", "language": "en", "status": "APPROVED"}}}]
            s._commerce_event = async_noop
            await s._send_order_notification(job.copy(), "claim")
            assert notifications.documents[0]["status"] == "cancelled", notifications.documents
            assert "provider_started_at" not in notifications.documents[0]

    # Resetting starts only this organization now, preserves submitted messages,
    # cancels old unsent reminders/order events/follow-ups, and preserves new jobs.
    s = load_service()
    s.utcnow = lambda: NOW
    templates = {event: {"name": event, "language": "en", "status": "APPROVED", "parameter_count": 2 if event == "shipped" else 3} for event in s.ORDER_EVENTS}
    existing = {**connection, "_id": "connection", "status": "connected", "scopes": ["read_fulfillments"],
                "whatsapp_phone_id": "phone", "whatsapp_id": "waba", "order_templates": templates}
    jobs = [{"_id": "old", "org_id": "org", "status": "sending", "claim_token": "claim", "event_at": old},
            {"_id": "submitted", "org_id": "org", "status": "sending", "event_at": old, "provider_started_at": old},
            {"_id": "other", "org_id": "other", "status": "pending", "event_at": old},
            {"_id": "new", "org_id": "org", "status": "pending", "event_at": NOW}]
    carts = [checkout("old", provider_created_at=old, reminder_status="sending", claim_token="claim"),
             checkout("submitted", provider_started_at=old, reminder_status="sent"),
             checkout("other", org_id="other")]
    s.db = FakeDB(shopify_connections=Collection([existing]), shopify_checkouts=Collection(carts),
                 shopify_order_notifications=Collection(jobs),
                 commerce_retargeting_jobs=Collection([{**job, "original_accepted_at": job["event_at"]} for job in jobs]))
    s._whatsapp_connection_for_selector = lambda *_args, **_kwargs: _value({"wa_id": "waba", "phone_id": "phone"})
    s.list_compatible_templates = lambda *_args, **kwargs: _value(
        list(templates.values()) if kwargs.get("purpose") == "order" else [{"name": "cart", "language": "en"}])
    s._configuration_ready = lambda *_args, **_kwargs: _value(True)
    s._is_whatsapp_suppressed = lambda *_args, **_kwargs: _value(False)
    s._commerce_event = async_noop
    s.get_connection = lambda *_args: _value(s.db.shopify_connections.documents[0])
    settings = {"whatsapp_phone_id": "phone", "template_name": "cart", "template_language": "en",
                "automation_enabled": True, "order_notifications_enabled": True,
                "order_templates": templates, "reminder_delay_minutes": 10, "start_from_now": True}
    saved = await s.update_settings("org", settings)
    assert saved["automation_enabled_at"] == saved["order_notifications_enabled_at"] == NOW
    assert s.db.shopify_checkouts.documents[0]["reminder_status"] == "historical"
    assert "claim_token" not in s.db.shopify_checkouts.documents[0]
    assert s.db.shopify_checkouts.documents[1]["reminder_status"] == "sent"
    assert s.db.shopify_checkouts.documents[2]["reminder_status"] == "pending"
    for collection in [s.db.shopify_order_notifications, s.db.commerce_retargeting_jobs]:
        assert [job["status"] for job in collection.documents] == ["cancelled", "sending", "pending", "pending"]
        assert "claim_token" not in collection.documents[0]
    s.utcnow = lambda: NOW + timedelta(minutes=1)
    await s.update_settings("org", {**settings, "start_from_now": False})
    assert saved["automation_enabled_at"] == saved["order_notifications_enabled_at"] == NOW

    # Previously accepted recovery messages cannot revive old cart follow-ups.
    from services import shopify_retargeting as r
    r.settings = lambda *_args: _value({"enabled": True})
    s.db.shopify_checkouts.documents[0].update({"record_status": "abandoned", "provider_message_id": "original"})
    *_, reason = await r.eligible(s, {"org_id": "org", "shop": "shop.test", "checkout_id": "old"})
    assert reason == "before_automation_activation"
    print("SHOPIFY_ACTIVATION_CUTOFF_OK")


if __name__ == "__main__":
    asyncio.run(main())
