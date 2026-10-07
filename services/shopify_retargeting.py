"""One receipt-aware purchase-help follow-up per recovery journey."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from services import shopify_growth as growth

DEFAULTS = {"enabled": False, "read_enabled": True, "unconfirmed_enabled": True,
            "read_delay_minutes": 5, "unconfirmed_delay_minutes": 30,
            "read_template_name": "", "read_template_language": "en_US",
            "unconfirmed_template_name": "", "unconfirmed_template_language": "en_US"}


def receipt_time(value, now):
    try:
        result = datetime.fromtimestamp(int(value), timezone.utc)
        return result if datetime(2020, 1, 1, tzinfo=timezone.utc) <= result <= now + timedelta(minutes=5) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


async def record_receipt(db, org_id, message_id, status, timestamp, now, error=None):
    if status not in {"sent", "delivered", "read", "failed"}:
        return
    event_at = receipt_time(timestamp, now)
    identifier = hashlib.sha256(f"{org_id}:{message_id}:{status}:{timestamp}".encode()).hexdigest()
    await db.commerce_delivery_receipts.update_one({"_id": identifier}, {"$setOnInsert": {
        "org_id": org_id, "message_id": message_id, "status": status, "event_at": event_at,
        "received_at": now, "error": error}}, upsert=True)


def engagement(receipts, accepted_at, now):
    """Missing read confirmation is unknown. Never manufacture a delivery time."""
    times = {status: min((growth.moment(row.get("event_at")) for row in receipts
                         if row.get("status") == status and growth.moment(row.get("event_at"))
                         and accepted_at - timedelta(minutes=1) <= growth.moment(row["event_at"]) <= now), default=None)
             for status in ("sent", "delivered", "read", "failed")}
    rank = 3 if times["read"] else 2 if times["delivered"] else 4 if times["failed"] else 1 if times["sent"] else 0
    return {"delivery_status": {0: "accepted", 1: "sent", 2: "delivered", 3: "read", 4: "failed"}[rank],
            **{f"{key}_at": value for key, value in times.items() if value}}


def next_step(job, receipts, config, now):
    accepted = growth.moment(job["original_accepted_at"])
    state = engagement(receipts, accepted, now)
    if now >= accepted + timedelta(days=7):
        return {**state, "status": "cancelled", "reason": "journey_expired"}
    branch = "read" if state.get("read_at") else "unconfirmed" if state.get("delivered_at") else None
    if not branch or not config.get(f"{branch}_enabled"):
        return {**state, "status": "observing", "reason": "awaiting_receipt" if not branch else "branch_disabled"}
    anchor = state.get("read_at") if branch == "read" else state["delivered_at"]
    return {**state, "status": "pending", "branch": branch,
            "due_at": anchor + timedelta(minutes=config[f"{branch}_delay_minutes"]),
            "reason": "read_without_purchase" if branch == "read" else "delivered_read_unconfirmed"}


async def settings(org_id):
    saved = await growth.service().db.commerce_retargeting_settings.find_one({"_id": org_id}) or {}
    return {**DEFAULTS, **{key: saved[key] for key in DEFAULTS if key in saved},
            "revision": saved.get("revision", ""), "enabled_at": saved.get("enabled_at")}


async def compatible_templates(org_id):
    connection = await growth.connected(org_id)
    return await growth.service().list_compatible_templates(org_id, connection.get("whatsapp_id"), phone_id=connection.get("whatsapp_phone_id"), raise_unavailable=True)


async def save_settings(org_id, values):
    s = growth.service()
    connection = await growth.connected(org_id)
    choices = await compatible_templates(org_id) if values["enabled"] and (values["read_enabled"] or values["unconfirmed_enabled"]) else []
    for branch in ("read", "unconfirmed"):
        if values["enabled"] and values[f"{branch}_enabled"]:
            if not any(item["name"] == values[f"{branch}_template_name"] and item["language"] == values[f"{branch}_template_language"] for item in choices):
                raise HTTPException(409, "Choose approved cart-help marketing templates for the active WhatsApp sender")
    previous = await settings(org_id)
    update = {**values, "org_id": org_id, "shop": connection["shop"], "revision": secrets.token_hex(12), "updated_at": s.utcnow()}
    if values["enabled"] and not previous["enabled"]:
        update["enabled_at"] = s.utcnow()
    await s.db.commerce_retargeting_settings.update_one({"_id": org_id}, {"$set": update}, upsert=True)
    if not values["enabled"]:
        await s.db.commerce_retargeting_jobs.update_many({"org_id": org_id, "status": {"$in": ["observing", "pending", "sending"]}, "provider_started_at": {"$exists": False}},
                                                       {"$set": {"status": "cancelled", "reason": "retargeting_paused", "updated_at": s.utcnow()}})
    return await settings(org_id)


async def register_original(s, cart):
    """Only newly submitted reminders can create jobs; migration cannot send old carts."""
    rate = cart.get("send_message_rate")
    await s.db.commerce_message_attempts.update_one({"_id": f"{cart['org_id']}:{cart['provider_message_id']}"}, {"$setOnInsert": {
        "org_id": cart["org_id"], "shop": cart["shop"], "customer_id": cart.get("customer_id"),
        "checkout_id": cart["provider_id"], "message_id": cart["provider_message_id"], "kind": "recovery",
        "accepted_at": cart["provider_accepted_at"], "message_rate": rate}}, upsert=True)
    config = await settings(cart["org_id"])
    accepted = growth.moment(cart["provider_accepted_at"])
    if not config["enabled"] or not config.get("enabled_at") or accepted < growth.moment(config["enabled_at"]):
        return
    journey = cart.get("recovery_journey_id") or cart.get("cart_token") or cart.get("checkout_token") or cart["provider_id"]
    identifier = hashlib.sha256(f"{cart['org_id']}:{cart['shop']}:{journey}".encode()).hexdigest()
    await s.db.commerce_retargeting_jobs.update_one({"_id": identifier}, {"$setOnInsert": {
        "org_id": cart["org_id"], "shop": cart["shop"], "customer_id": cart.get("customer_id"),
        "checkout_id": cart["provider_id"], "phone": cart["phone"], "original_message_id": cart["provider_message_id"],
        "original_accepted_at": accepted, "whatsapp_id": cart.get("send_whatsapp_id"), "phone_id": cart.get("send_phone_id"),
        "status": "observing", "created_at": s.utcnow(), "updated_at": s.utcnow()}}, upsert=True)


async def eligible(s, job):
    now = s.utcnow()
    connection = await s.db.shopify_connections.find_one({"org_id": job["org_id"], "shop": job["shop"], "status": "connected", "automation_enabled": True})
    config = await settings(job["org_id"])
    if not connection or not config["enabled"]:
        return None, config, None, "automation_disabled"
    cart = await s.db.shopify_checkouts.find_one({"org_id": job["org_id"], "provider_id": job["checkout_id"]})
    if not cart or cart.get("record_status") != "abandoned" or cart.get("completed_at") or cart.get("converted_order_id") or cart.get("storefront_checkout_started"):
        return cart, config, connection, "purchased_or_cart_unavailable"
    if not s._recovery_activity_time(cart.get("provider_updated_at"), connection.get("automation_enabled_at"), cart.get("provider_created_at")):
        return cart, config, connection, "before_automation_activation"
    if cart.get("provider_message_id") != job["original_message_id"] or cart.get("phone") != job["phone"]:
        return cart, config, connection, "cart_or_phone_changed"
    if job["original_accepted_at"] + timedelta(days=7) <= now:
        return cart, config, connection, "journey_expired"
    latest = await s.db.shopify_checkouts.find_one({"org_id": job["org_id"], "phone": job["phone"]}, sort=[("provider_updated_at", -1), ("provider_created_at", -1), ("provider_id", -1)])
    if latest and latest["provider_id"] != cart["provider_id"]:
        return cart, config, connection, "newer_cart"
    # A verified Shopify customer purchase stops retargeting, even without a cart token.
    if cart.get("customer_id"):
        customer = (await s._graphql(connection, growth.CUSTOMER_QUERY, {"id": cart["customer_id"]})).get("customer")
        if not customer or growth.phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber")) != growth.phone_key(job["phone"]):
            return cart, config, connection, "customer_phone_changed"
        last = growth.moment((customer.get("lastOrder") or {}).get("createdAt"))
        if last and last >= job["original_accepted_at"]:
            return cart, config, connection, "purchased"
    matches = [{"checkout_token": cart.get("checkout_token")}, {"cart_token": cart.get("checkout_token")}]
    if cart.get("checkout_token") and await s.db.shopify_orders.find_one({"org_id": job["org_id"], "$or": matches}):
        return cart, config, connection, "purchased"
    reply = await s.db[f"messages_{job['org_id']}"].find_one({"sender_id": {"$in": s._phone_candidates(job["phone"])},
             "role": "customer", "business_phone_id": job["phone_id"], "timestamp": {"$gte": job["original_accepted_at"]}})
    if reply:
        return cart, config, connection, "customer_replied"
    if await s._is_whatsapp_suppressed(job["org_id"], job["phone"]) or not await s.recovery_permission(cart):
        return cart, config, connection, "permission_revoked"
    sender = await s._selected_whatsapp_connection(connection)
    if not sender or sender["wa_id"] != job["whatsapp_id"] or sender["phone_id"] != job["phone_id"]:
        return cart, config, connection, "sender_changed"
    return cart, config, connection, None


async def finish(s, job, token, status, reason=None, **fields):
    await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"], "claim_token": token, "status": "sending"},
        {"$set": {"status": status, "reason": reason or job.get("reason"), "updated_at": s.utcnow(), **fields}, "$unset": {"claim_token": "", "claim_expires_at": ""}})


async def send_followup(s, job, token):
    started = False
    try:
        cart, config, connection, reason = await eligible(s, job)
        if reason:
            await finish(s, job, token, "cancelled", reason)
            return
        if cart.get("source") == "storefront_cart":
            from services.shopify_storefront_cart import CUSTOMER_VARIANTS_QUERY
            if cart.get("storefront_checkout_started") or not cart.get("storefront_consent"):
                await finish(s, job, token, "cancelled", "cart_no_longer_eligible")
                return
            data = await s._graphql(connection, CUSTOMER_VARIANTS_QUERY, {"customer": cart["customer_id"], "variants": [f"gid://shopify/ProductVariant/{item['id']}" for item in cart.get("storefront_items", [])]})
            nodes = [node for node in data.get("nodes", []) if node]
            if not nodes or len(nodes) != len(cart.get("storefront_items", [])) or any((node.get("product") or {}).get("status") != "ACTIVE" for node in nodes):
                await finish(s, job, token, "cancelled", "cart_product_unavailable")
                return
        else:
            node = (await s._graphql(connection, s.CHECKOUT_NODE_QUERY, {"id": cart["provider_id"]})).get("node")
            if not node or node.get("completedAt"):
                await finish(s, job, token, "cancelled", "purchased_or_cart_unavailable")
                return
            fresh = s._checkout_from_graphql(job["org_id"], job["shop"], node, False)
            if fresh["phone"] != job["phone"]:
                await finish(s, job, token, "cancelled", "customer_phone_changed")
                return
            cart = {**cart, **{key: fresh[key] for key in ("products", "recovery_url", "phone")}}
        receipts = await s.db.commerce_delivery_receipts.find({"org_id": job["org_id"], "message_id": job["original_message_id"]}).to_list(None)
        step = next_step(job, receipts, config, s.utcnow())
        if step["status"] != "pending" or step.get("branch") != job.get("branch") or step["due_at"] > s.utcnow():
            await finish(s, job, token, step.pop("status"), **step)
            return
        branch = step["branch"]
        name, language = config[f"{branch}_template_name"], config[f"{branch}_template_language"]
        if not any(item["name"] == name and item["language"] == language for item in await compatible_templates(job["org_id"])):
            await finish(s, job, token, "cancelled", "template_unavailable")
            return
        sender = await s._selected_whatsapp_connection(connection)
        values = [str(cart.get("customer_name") or "there").split(" ", 1)[0][:80], s._product_summary(cart.get("products") or []), cart.get("recovery_url", "")]
        if not values[1] or not values[2].startswith("https://"):
            await finish(s, job, token, "cancelled", "cart_content_unavailable")
            return
        from services.shopify_reporting import tracked_link
        values[2] = await tracked_link(job["org_id"], job["shop"], "retargeting", job["_id"], values[2], job.get("customer_id"))
        _, current_config, _, reason = await eligible(s, job)
        if reason or current_config["revision"] != config["revision"]:
            await finish(s, job, token, "cancelled", reason or "settings_changed")
            return
        # Re-read receipts immediately before the atomic submit fence, so read changes the branch.
        receipts = await s.db.commerce_delivery_receipts.find({"org_id": job["org_id"], "message_id": job["original_message_id"]}).to_list(None)
        final_step = next_step(job, receipts, config, s.utcnow())
        if final_step.get("branch") != branch or final_step["status"] != "pending" or final_step["due_at"] > s.utcnow():
            await finish(s, job, token, final_step.pop("status"), **final_step)
            return
        # Only the original reminder's reservation grants this single exception. Another campaign cannot grant it.
        key = f"{job['org_id']}:whatsapp:{hashlib.sha256(job['phone'].lstrip('+').encode()).hexdigest()}"
        allowance = await s.db.commerce_contact_limits.find_one_and_update(
            {"_id": key, "owner": f"recovery:{job['checkout_id']}"},
            {"$set": {"owner": f"retargeting:{job['_id']}", "next_allowed_at": s.utcnow() + timedelta(hours=24)}}, return_document=ReturnDocument.AFTER)
        if not allowance:
            await finish(s, job, token, "cancelled", "another_contact_used_allowance")
            return
        claimed = await s.db.commerce_retargeting_jobs.find_one_and_update(
            {"_id": job["_id"], "claim_token": token, "status": "sending", "provider_started_at": {"$exists": False}},
            {"$set": {"provider_started_at": s.utcnow(), "template_name": name, "template_language": language,
                      "message_rate": (await growth.settings(job["org_id"])).get("message_rate")}}, return_document=ReturnDocument.AFTER)
        if not claimed:
            return
        started = True
        body = {"messaging_product": "whatsapp", "to": job["phone"].lstrip("+"), "type": "template", "template": {
            "name": name, "language": {"code": language}, "components": [{"type": "body", "parameters": [{"type": "text", "text": value} for value in values]}]}}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"https://graph.facebook.com/{s.WA_VERSION or 'v24.0'}/{sender['phone_id']}/messages", headers={"Authorization": f"Bearer {sender['access_token']}"}, json=body)
        if response.status_code not in {200, 201}:
            error = s.normalize_whatsapp_delivery_error(response.json(), stage="submission", http_status=response.status_code)
            await finish(s, job, token, "failed", "provider_rejected", provider_error=error)
            if s._provider_reports_block(response.json()):
                await s.db.whatsapp_suppressions.update_one({"org_id": job["org_id"], "phone": growth.phone_key(job["phone"])}, {"$set": {"reason": "provider_block", "updated_at": s.utcnow()}}, upsert=True)
            return
        message_id = response.json()["messages"][0]["id"]
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Provider response has no message ID")
        await finish(s, job, token, "accepted", provider_message_id=message_id, provider_accepted_at=s.utcnow(), delivery_status="accepted", inbox_sync_status="pending")
        await s._commerce_event(job["org_id"], "checkout", job["checkout_id"])
    except Exception as exc:
        s.logger.warning("Shopify follow-up deferred: {}", type(exc).__name__)
        await finish(s, job, token, "delivery_unknown" if started else "pending", "provider_outcome_unknown" if started else "pre_provider_failure", due_at=s.utcnow() + timedelta(minutes=5), retry_after=s.utcnow() + timedelta(minutes=5))


async def reconcile_inbox(s, job):
    cart = await s.db.shopify_checkouts.find_one({"org_id": job["org_id"], "provider_id": job["checkout_id"]})
    if not cart:
        return
    message_id = job["provider_message_id"]
    content = f"Cart purchase help ({job['branch'].replace('_', ' ')}): {s._product_summary(cart.get('products') or [])}"
    conversation = await s.create_or_update_whatsapp_conversation(org_id=job["org_id"], recipient_id=job["whatsapp_id"], customer_phone_no=job["phone"].lstrip("+"), customer_name=cart.get("customer_name") or job["phone"], last_message=content, last_sender="agent", is_auto_followup=True, idempotency_key=message_id, business_phone_id=job["phone_id"], extra_updates={"last_message_source": "shopify_cart_followup"})
    await s.store_whatsapp_message(org_id=job["org_id"], content=content, conversation_id=conversation, customer_phone_no=job["phone"].lstrip("+"), type="template", payload=None, recipient_id=job["whatsapp_id"], role=s.MessageRole.AGENT, sender_name="Shopify purchase help", message_id=message_id, status=job.get("delivery_status", "sent"), business_phone_id=job["phone_id"], bot_context={"source": "shopify_cart_followup", "checkout_id": job["checkout_id"], "branch": job["branch"], "original_message_id": job["original_message_id"]})
    await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"]}, {"$set": {"inbox_sync_status": "complete"}})


async def replay_inbox_status(s, org_id, message_id, state):
    current = await s.db[f"messages_{org_id}"].find_one({"message_id": message_id, "platform": "whatsapp"})
    if current and current.get("status") != state["delivery_status"]:
        from services.wa_service import update_whatsapp_message_status
        await update_whatsapp_message_status(org_id, message_id, state["delivery_status"])


async def process_retargeting():
    s = growth.service()
    now = s.utcnow()
    await s.db.commerce_retargeting_jobs.update_many({"status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": True}}, {"$set": {"status": "delivery_unknown", "reason": "worker_interrupted_after_submit"}})
    await s.db.commerce_retargeting_jobs.update_many({"status": "sending", "claim_expires_at": {"$lte": now}, "provider_started_at": {"$exists": False}}, {"$set": {"status": "pending", "due_at": now}, "$unset": {"claim_token": "", "claim_expires_at": ""}})
    for cart in await s.db.shopify_checkouts.find({"receipt_tracking_version": 1, "provider_message_id": {"$exists": True}, "attempt_registered": {"$ne": True}}).limit(100).to_list(100):
        await register_original(s, cart)
        await s.db.shopify_checkouts.update_one({"_id": cart["_id"], "provider_message_id": cart["provider_message_id"]}, {"$set": {"attempt_registered": True}})
    # Replay receipts that arrived before the original message ID was committed.
    originals = await s.db.shopify_checkouts.find({"receipt_tracking_version": 1, "provider_message_id": {"$exists": True}}).sort("receipt_reconciled_at", 1).limit(100).to_list(100)
    for cart in originals:
        try:
            receipts = await s.db.commerce_delivery_receipts.find({"org_id": cart["org_id"], "message_id": cart["provider_message_id"]}).to_list(None)
            positive_delivery = any(row["status"] in {"delivered", "read"} for row in receipts)
            for receipt in sorted(receipts, key=lambda row: {"sent": 1, "delivered": 2, "read": 3, "failed": 4}.get(row["status"], 0)):
                if receipt["status"] == "failed" and positive_delivery:
                    continue
                event_at = growth.moment(receipt.get("event_at"))
                await s.update_shopify_reminder_delivery(cart["org_id"], cart["provider_message_id"], receipt["status"], receipt.get("error"), str(int(event_at.timestamp())) if event_at else None)
            await replay_inbox_status(s, cart["org_id"], cart["provider_message_id"], engagement(receipts, growth.moment(cart["provider_accepted_at"]), now))
        except Exception as exc:
            s.logger.warning("Shopify original receipt reconciliation deferred: {}", type(exc).__name__)
        await s.db.shopify_checkouts.update_one({"_id": cart["_id"]}, {"$set": {"receipt_reconciled_at": now}})
    rows = await s.db.commerce_retargeting_jobs.find({"status": {"$in": ["observing", "pending"]}, "$or": [{"retry_after": {"$exists": False}}, {"retry_after": {"$lte": now}}]}).sort("updated_at", 1).limit(100).to_list(100)
    for job in rows:
        try:
            _, config, _, reason = await eligible(s, job)
            receipts = await s.db.commerce_delivery_receipts.find({"org_id": job["org_id"], "message_id": job["original_message_id"]}).to_list(None)
            step = {"status": "cancelled", "reason": reason} if reason else next_step(job, receipts, config, now)
            await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"], "status": {"$in": ["observing", "pending"]}}, {"$set": {**step, "updated_at": now}})
            if step["status"] == "pending" and step["due_at"] <= s.utcnow():
                token = secrets.token_urlsafe(16)
                claimed = await s.db.commerce_retargeting_jobs.find_one_and_update({"_id": job["_id"], "status": "pending", "provider_started_at": {"$exists": False}}, {"$set": {"status": "sending", "claim_token": token, "claim_expires_at": now + timedelta(minutes=5)}}, return_document=ReturnDocument.AFTER)
                if claimed:
                    await send_followup(s, claimed, token)
        except Exception as exc:
            s.logger.warning("Shopify retarget eligibility check deferred: {}", type(exc).__name__)
            await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"]}, {"$set": {"updated_at": now}})
    for job in await s.db.commerce_retargeting_jobs.find({"status": "accepted"}).sort("updated_at", 1).limit(100).to_list(100):
        receipts = await s.db.commerce_delivery_receipts.find({"org_id": job["org_id"], "message_id": job["provider_message_id"]}).to_list(None)
        state = engagement(receipts, growth.moment(job["provider_accepted_at"]), now)
        await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"]}, {"$set": {**state, "updated_at": now}})
        try:
            await replay_inbox_status(s, job["org_id"], job["provider_message_id"], state)
            if job.get("inbox_sync_status") != "complete":
                await reconcile_inbox(s, {**job, **state})
        except DuplicateKeyError:
            await s.db.commerce_retargeting_jobs.update_one({"_id": job["_id"]}, {"$set": {"inbox_sync_status": "complete"}})
        except Exception as exc:
            s.logger.warning("Shopify follow-up Inbox reconciliation deferred: {}", type(exc).__name__)


async def jobs(org_id):
    rows = await growth.service().db.commerce_retargeting_jobs.find({"org_id": org_id}).sort("created_at", -1).limit(100).to_list(100)
    fields = ("checkout_id", "status", "reason", "branch", "due_at", "created_at", "delivery_status", "delivered_at", "read_at", "provider_accepted_at", "original_accepted_at")
    return [{"job_id": row["_id"], **{key: row.get(key) for key in fields}} for row in rows]
