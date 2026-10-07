"""Behavioral checks; no customer messages or external services. Run with the backend venv."""
import asyncio
import copy
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
import fastapi
import pymongo
from test_commerce_growth import DB, Collection
from test_shopify_worker_runtime import NOW, load_service, checkout, _prepare_send, _configured_send_db
from services import shopify_growth as growth, shopify_retargeting as retarget, shopify_reporting as reporting


async def main():
    modules = dict(sys.modules)
    s = load_service()
    for name, saved_module in modules.items():
        sys.modules[name] = saved_module
    s.DuplicateKeyError = pymongo.errors.DuplicateKeyError
    s.utcnow = lambda: NOW
    s.logger.warning = print
    growth.service = lambda: s
    config = {**retarget.DEFAULTS, "enabled": True, "enabled_at": NOW-timedelta(hours=1), "revision": "one", "read_template_name": "cart", "unconfirmed_template_name": "cart", "read_template_language": "en", "unconfirmed_template_language": "en"}
    parent = checkout("cart", shop="shop.test", customer_id="customer", provider_message_id="original", provider_accepted_at=NOW-timedelta(minutes=40), receipt_tracking_version=1,
                      reminder_status="sent", send_whatsapp_id="waba", send_phone_id="phone", checkout_token="journey", send_message_rate={"amount": "0.10", "currency": "INR"})
    s.db = DB()
    prepared = _configured_send_db(Collection([parent]))
    s.db.collections.update(prepared.collections)
    _prepare_send(s, s.db)
    s.list_compatible_templates = lambda *_args, **_kwargs: value([{
        "name": "cart", "language": "en", "status": "APPROVED", "category": "MARKETING", "parameter_count": 2,
    }])
    s.db.commerce_retargeting_settings.documents.append({"_id": "org", "org_id": "org", **config})
    async def graphql(connection, query, variables, **kwargs):
        if "GrowthCustomer" in query:
            return {"customer": {"id": "customer", "numberOfOrders": 1, "defaultPhoneNumber": {"phoneNumber": parent["phone"]}, "lastOrder": {"createdAt": (NOW-timedelta(days=30)).isoformat()}}}
        if "ReportShop" in query:
            return {"shop": {"ianaTimezone": "Asia/Kolkata"}}
        return {"node": {"id": "cart", "updatedAt": parent["provider_updated_at"].isoformat(), "customer": {"id": "customer", "defaultPhoneNumber": {"phoneNumber": parent["phone"]}},
                         "lineItems": {"nodes": [{"id": "line", "title": "Product", "quantity": 1}]}, "abandonedCheckoutUrl": parent["recovery_url"]}}
    s._graphql = graphql
    accepted = parent["provider_accepted_at"]
    job = {"original_accepted_at": accepted}
    stamp = lambda date: str(int(date.timestamp()))
    delivered = {"status": "delivered", "event_at": NOW-timedelta(minutes=31)}
    read = {"status": "read", "event_at": NOW-timedelta(minutes=6)}
    assert retarget.next_step(job, [delivered], config, NOW)["due_at"] == delivered["event_at"]+timedelta(minutes=30)
    assert retarget.next_step(job, [read, delivered, read], config, NOW)["due_at"] == read["event_at"]+timedelta(minutes=5)
    assert retarget.next_step(job, [{"status": "read", "event_at": None}], config, NOW)["status"] == "observing"
    assert retarget.next_step({"original_accepted_at": NOW-timedelta(days=8)}, [read], config, NOW)["reason"] == "journey_expired"
    assert retarget.engagement([delivered, read, {"status": "failed", "event_at": NOW}], accepted, NOW)["delivery_status"] == "read"
    assert retarget.receipt_time("invalid", NOW) is None
    assert retarget.receipt_time(stamp(NOW+timedelta(days=1)), NOW) is None
    # A receipt can arrive before registration; replay reads the durable ledger.
    for status, when in (("delivered", delivered["event_at"]), ("read", read["event_at"]), ("read", read["event_at"])):
        await retarget.record_receipt(s.db, "org", "original", status, stamp(when), NOW)
    assert len(s.db.commerce_delivery_receipts.documents) == 2
    await retarget.register_original(s, parent)
    await retarget.register_original(s, parent)
    assert len(s.db.commerce_retargeting_jobs.documents) == 1
    assert await s.reserve_marketing_contact("org", "whatsapp", parent["phone"].lstrip("+"), "recovery:cart")
    posts = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            posts.append(kwargs["json"])
            return SimpleNamespace(status_code=200, json=lambda: {"messages": [{"id": "followup"}]})
    retarget.httpx.AsyncClient = Client
    s.create_or_update_whatsapp_conversation = lambda **kwargs: value("conversation")
    s.store_whatsapp_message = lambda **kwargs: value(None)
    await asyncio.gather(retarget.process_retargeting(), retarget.process_retargeting())
    assert len(posts) == 1
    parameters = posts[0]["template"]["components"][0]["parameters"]
    assert len(parameters) == 2 and parameters[1]["text"].startswith("https://")
    saved = s.db.commerce_retargeting_jobs.documents[0]
    assert saved["status"] == "accepted" and saved["branch"] == "read" and saved["provider_message_id"] == "followup"
    assert saved["inbox_sync_status"] == "complete"
    assert s.db.shopify_checkouts.documents[0]["provider_message_id"] == "original"
    assert s.db.shopify_checkouts.documents[0]["delivery_status"] == "read"
    assert not await s.reserve_marketing_contact("org", "whatsapp", parent["phone"].lstrip("+"), "campaign")
    await retarget.process_retargeting()
    assert len(posts) == 1
    # A read arriving during preparation changes the branch and restarts its timer.
    pending = {**saved, "_id": "new-job", "status": "sending", "claim_token": "claim", "branch": "unconfirmed", "original_message_id": "original2"}
    pending.pop("provider_started_at", None)
    s.db.commerce_retargeting_jobs.documents.append(pending)
    s.db.shopify_checkouts.documents[0]["provider_message_id"] = "original2"
    await retarget.record_receipt(s.db, "org", "original2", "delivered", stamp(NOW-timedelta(minutes=31)), NOW)
    await retarget.record_receipt(s.db, "org", "original2", "read", stamp(NOW-timedelta(minutes=1)), NOW)
    await retarget.send_followup(s, pending, "claim")
    current = next(row for row in s.db.commerce_retargeting_jobs.documents if row["_id"] == "new-job")
    assert current["status"] == "pending" and current["branch"] == "read" and current["due_at"] > NOW
    assert len(posts) == 1
    # Customer reply on the same business line stops the journey.
    s.db["messages_org"].documents.append({"sender_id": parent["phone"].lstrip("+"), "role": "customer", "business_phone_id": "phone", "timestamp": NOW})
    assert (await retarget.eligible(s, pending))[3] == "customer_replied"
    s.db["messages_org"].documents.clear()
    s.db.shopify_checkouts.documents[0]["record_status"] = "converted"
    assert (await retarget.eligible(s, pending))[3] == "purchased_or_cart_unavailable"
    s.db.shopify_checkouts.documents[0]["record_status"] = "abandoned"
    # Interrupted submissions are unknown and are never replayed.
    unknown = {**pending, "_id": "unknown", "status": "sending", "provider_started_at": NOW-timedelta(minutes=10), "claim_expires_at": NOW-timedelta(minutes=1)}
    s.db.commerce_retargeting_jobs.documents.append(unknown)
    await retarget.process_retargeting()
    assert next(row for row in s.db.commerce_retargeting_jobs.documents if row["_id"] == "unknown")["status"] == "delivery_unknown"
    assert len(posts) == 1
    # Historical receipts and reminders cannot queue jobs when enabling later.
    before = len(s.db.commerce_retargeting_jobs.documents)
    await retarget.register_original(s, {**parent, "checkout_token": "old", "provider_message_id": "old", "provider_accepted_at": NOW-timedelta(days=1)})
    assert len(s.db.commerce_retargeting_jobs.documents) == before
    # Atomic experiment assignment survives retries and controls skip the original journey.
    s.db.commerce_experiments.documents.append({"_id": "e", "experiment_id": "e", "org_id": "org", "shop": "shop.test", "status": "running", "holdout_percent": 10, "name": "Whole journey", "created_at": NOW})
    original_rand = reporting.secrets.randbelow
    reporting.secrets.randbelow = lambda _: 0
    assert await reporting.enroll("org", parent)
    reporting.secrets.randbelow = lambda _: 99
    assert await reporting.enroll("org", parent)
    assert len(s.db.commerce_experiment_assignments.documents) == 1
    reporting.secrets.randbelow = original_rand
    experiment = s.db.commerce_experiments.documents[0]
    outcome = await reporting.experiment_report("org", experiment)
    assert outcome["groups"][0]["mature"] == 0 and outcome["groups"][0]["pending"] == 1
    assert outcome["conversion_difference_points"] is None
    s.db.commerce_experiments.documents[0]["status"] = "stopped"
    assert await reporting.enroll("org", parent)  # Stopping enrollment preserves outcomes.
    # Last follow-up gets credit once; unpaid orders and currencies remain separate.
    base = {"org_id": "org", "provider_created_at": NOW-timedelta(minutes=1), "created_at": NOW-timedelta(minutes=1), "customer_id": "customer", "record_status": "active", "payment_status": "PAID"}
    s.db.shopify_orders.documents.extend([{**base, "provider_id": "order", "order_number": "#1", "net_payment": {"amount": "80.10", "currency": "INR"}, "total": {"amount": "100.10", "currency": "INR"}},
                                         {**base, "provider_id": "cod", "payment_status": "PENDING", "net_payment": None, "total": {"amount": "900", "currency": "INR"}}])
    s.db.shopify_checkouts.documents[0].update(provider_message_id="original", converted_order_id="order")
    # Discard synthetic message IDs used above to exercise receipt races and activation.
    s.db.commerce_message_attempts.documents[:] = [row for row in s.db.commerce_message_attempts.documents if row["message_id"] == "original"]
    # Provider acceptance in this test occurred at NOW, so put follow-up earlier than the purchase.
    saved["provider_accepted_at"] = NOW-timedelta(minutes=2)
    result = await reporting.period_report("org", NOW-timedelta(days=1), NOW+timedelta(seconds=1))
    assert result["summary"]["matched_orders"] == 1 and result["summary"]["net_payments"] == [{"currency": "INR", "amount": "80.10"}]
    assert result["orders"][0]["kind"] == "retargeting"
    assert result["summary"]["converted_contacts"] == 1 and result["summary"]["conversion_denominator"] == 2
    assert result["summary"]["conversion_rate"] == 50
    assert result["summary"]["cart_conversion_denominator"] == 1
    assert result["summary"]["converted_carts"] == 1 and result["summary"]["cart_conversion_rate"] == 100
    assert reporting.totals([{"amount": "0.10", "currency": "USD"}, {"amount": "0.20", "currency": "USD"}, {"amount": "1", "currency": "INR"}]) == [{"currency": "INR", "amount": "1"}, {"currency": "USD", "amount": "0.30"}]
    assert "'=1+1" in reporting.csv_report({"orders": [{"name": "=1+1"}]}, "orders")
    assert "'  @evil" in reporting.csv_report({"orders": [{"name": "  @evil"}]}, "orders")
    low, high = reporting.conversion_interval(1, 10)
    assert low < 10 < high
    # Store-scoped link tokens never put shopper identity into the URL.
    link = await reporting.tracked_link("org", "shop.test", "campaign", "campaign:anonymous", "https://shop.test/", "customer")
    token = link.rsplit("/", 1)[-1]
    target = await reporting.resolve_link(token, "facebookexternalhit")
    assert "heidel_touch=" in target and "customer" not in link
    tracked = next(row for row in s.db.commerce_tracked_links.documents if row["token"] == token)
    assert tracked["preview_requests"] == 1 and not tracked.get("redirect_requests")
    assert await reporting.tracked_link("org", "shop.test", "campaign", "bad", "http://evil.test") == "http://evil.test"
    # Browser events are deduplicated and cannot create revenue or authorize sends.
    s.db.commerce_pixel_installations.documents.append({"_id": "org", "org_id": "org", "shop": "shop.test", "token": "installation"})
    event = {"installation_token": "installation", "event_id": "one", "client_id": "browser", "event_name": "page_viewed", "touch_token": token}
    await reporting.capture_pixel(event)
    await reporting.capture_pixel(event)
    assert len(s.db.commerce_pixel_events.documents) == 1
    # A campaign created before the period still appears if it sent during the period.
    s.db.commerce_campaigns.documents.append({"_id": "older", "campaign_id": "older", "org_id": "org", "created_at": NOW-timedelta(days=10), "channel": "whatsapp", "name": "Older campaign", "status": "completed"})
    s.db.bulk_job_results.documents.append({"_id": "older-result", "org_id": "org", "job_id": "older", "status": "sent", "created_at": NOW-timedelta(hours=1), "message_id": "campaign-message", "phone_number": parent["phone"]})
    # A guest order cannot start a customer retention cohort.
    s.db.shopify_orders.documents.append({**base, "provider_id": "guest", "customer_id": None, "customer_order_count_at_purchase": 1})
    result = await reporting.period_report("org", NOW-timedelta(days=1), NOW+timedelta(seconds=1))
    assert next(row for row in result["campaigns"] if row["campaign_id"] == "older")["accepted"] == 1
    assert all(row["pending"] == 0 for row in result["retention"])
    # A newer campaign can win revenue credit without hiding a cart recovery.
    s.db.commerce_campaigns.documents[0]["recipient_customers"] = {growth.phone_key(parent["phone"]): "customer"}
    s.db.bulk_job_results.documents[0]["created_at"] = NOW-timedelta(seconds=30)
    s.db.shopify_orders.documents[0]["provider_created_at"] = NOW-timedelta(seconds=10)
    # Same buyer, separate unmatched cart: identity alone must not manufacture recovery.
    unmatched = {**parent, "_id": "unmatched", "provider_id": "unmatched", "checkout_token": "unmatched", "provider_message_id": "unmatched-message"}
    unmatched.pop("converted_order_id", None)
    s.db.shopify_checkouts.documents.append(unmatched)
    result = await reporting.period_report("org", NOW-timedelta(days=1), NOW+timedelta(seconds=1))
    assert next(row for row in result["orders"] if row["order_id"] == "order")["kind"] == "campaign"
    assert result["summary"]["cart_conversion_denominator"] == 2
    assert result["summary"]["converted_carts"] == 1 and result["summary"]["cart_conversion_rate"] == 50
    # Alias records for one journey do not increase the denominator.
    alias = {**parent, "_id": "alias", "provider_id": "alias", "provider_message_id": "alias-message", "converted_order_id": "order"}
    s.db.shopify_checkouts.documents.append(alias)
    result = await reporting.period_report("org", NOW-timedelta(days=1), NOW+timedelta(seconds=1))
    assert result["summary"]["cart_conversion_denominator"] == 2 and result["summary"]["converted_carts"] == 1
    s.db.shopify_orders.documents[0]["cancelled_at"] = NOW
    result = await reporting.period_report("org", NOW-timedelta(days=1), NOW+timedelta(seconds=1))
    assert result["summary"]["converted_carts"] == 0 and result["summary"]["cart_conversion_rate"] == 0
    s.db.shopify_orders.documents[0].pop("cancelled_at")
    empty = await reporting.period_report("org", NOW-timedelta(days=20), NOW-timedelta(days=19))
    assert empty["summary"]["cart_conversion_denominator"] == 0 and empty["summary"]["cart_conversion_rate"] == 0
    # Finishing a pre-provider error must never restore a cancelled sending job.
    cancelled = {**saved, "_id": "cancelled", "status": "cancelled", "claim_token": "cancelled-claim"}
    s.db.commerce_retargeting_jobs.documents.append(cancelled)
    await retarget.finish(s, cancelled, "cancelled-claim", "pending", "pre_provider_failure")
    assert cancelled["status"] == "cancelled"
    # An unavailable Inbox status updater cannot stop the rest of the worker.
    replay = retarget.replay_inbox_status
    async def unavailable(*args): raise ConnectionError("Inbox unavailable")
    retarget.replay_inbox_status = unavailable
    try:
        await retarget.process_retargeting()
    finally:
        retarget.replay_inbox_status = replay
    # Weekly snapshots are idempotent and bounded to the previous calendar week.
    async def snapshot(*args, **kwargs): return {"summary": {}, "actions": [], "orders": []}
    original_report = reporting.report
    reporting.report = snapshot
    await reporting.process_weekly_reports()
    await reporting.process_weekly_reports()
    assert len(s.db.commerce_weekly_reports.documents) == 1
    assert __import__("datetime").date.fromisoformat(s.db.commerce_weekly_reports.documents[0]["week_ending"]).weekday() == 6
    reporting.report = original_report
    print("SHOPIFY_OWNER_REPORTING_OK: receipt timers, replay, concurrency, branch switches, reply/purchase suppression, unknown outcomes, holdouts, attribution, money, links, CSV, pixel and weekly snapshots")


async def value(item): return item

if __name__ == "__main__":
    asyncio.run(main())
