"""Shopify sales attribution and scheduled, consented purchase campaigns."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


CUSTOMERS_QUERY = """query GrowthCustomers($first: Int!, $after: String) {
  customers(first: $first, after: $after) {
    nodes { id displayName numberOfOrders email emailMarketingConsent { marketingState }
      defaultPhoneNumber { phoneNumber } lastOrder { createdAt } }
    pageInfo { hasNextPage endCursor }
  }
}"""
CUSTOMER_QUERY = """query GrowthCustomer($id: ID!) {
  customer(id: $id) { id displayName numberOfOrders email emailMarketingConsent { marketingState }
    defaultPhoneNumber { phoneNumber } lastOrder { createdAt } }
}"""


def service():
    from services import shopify_service
    return shopify_service


def money(value) -> Decimal:
    try:
        result = Decimal(str(value))
        return max(result, Decimal(0)) if result.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def moment(value):
    if not value:
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=result.tzinfo or timezone.utc)
    except (TypeError, ValueError):
        return None


def phone_key(value):
    return re.sub(r"\D", "", str(value or ""))


def email_key(value):
    return hashlib.sha256(str(value or "").strip().lower().encode()).hexdigest()


def segment_matches(customer, segment, lapsed_days, now):
    count = int(customer.get("numberOfOrders") or 0)
    last = moment((customer.get("lastOrder") or {}).get("createdAt"))
    if segment == "first_time":
        return count == 1
    if segment == "repeat":
        return count >= 2
    return count > 0 and last is not None and last <= now - timedelta(days=lapsed_days)


def attribution(orders, touches, now, days=7):
    """Last eligible contact wins once per order; cart links outrank identity guesses."""
    matched = []
    values = {}
    seen = set()
    for order in sorted(orders, key=lambda row: moment(row.get("provider_updated_at")) or now, reverse=True):
        identifier = order.get("provider_id")
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        created = moment(order.get("provider_created_at") or order.get("created_at"))
        if not created or order.get("cancelled_at") or order.get("record_status") == "deleted":
            continue
        candidates = []
        for touch in touches:
            sent = moment(touch.get("sent_at"))
            if (touch.get("status") in {"failed", "bounced", "suppressed", "complained"}
                    or touch.get("org_id") != order.get("org_id") or not sent
                    or not sent <= created <= min(now, sent + timedelta(days=days))):
                continue
            if touch.get("kind") == "recovery":
                eligible = touch.get("order_id") == identifier
            else:
                eligible = bool(touch.get("customer_id") and touch["customer_id"] == order.get("customer_id"))
            if eligible:
                candidates.append(touch)
        if not candidates:
            continue
        touch = max(candidates, key=lambda row: moment(row["sent_at"]))
        net = order.get("net_payment")
        if net is None and order.get("payment_status") == "PAID":
            net = order.get("total")
        # Unpaid COD and legacy partial refunds are matched, but never counted as cash sales.
        amount = money((net or {}).get("amount"))
        currency = (net or {}).get("currency")
        if currency and net is not None:
            values[currency] = values.get(currency, Decimal(0)) + amount
        matched.append({"order_id": identifier, "order_number": order.get("order_number", ""),
                        "campaign_id": touch.get("campaign_id"), "kind": touch["kind"],
                        "touch_id": touch.get("touch_id") or f"{touch['kind']}:{touch.get('campaign_id') or touch.get('order_id')}:{touch['sent_at']}",
                        "sent_at": moment(touch["sent_at"]).isoformat(), "created_at": created.isoformat(),
                        "net_payment": {"amount": str(amount), "currency": currency} if net is not None else None})
    return {"matched_orders": len(matched), "converted_contacts": len({row["touch_id"] for row in matched}), "sales": [{"currency": key, "amount": str(value)} for key, value in sorted(values.items())],
            "orders": matched, "value_unavailable": sum(row["net_payment"] is None for row in matched)}


async def settings(org_id):
    s = service()
    saved = await s.db.commerce_growth_settings.find_one({"_id": org_id}) or {}
    return {"attribution_days": saved.get("attribution_days", 7), "message_rate": saved.get("message_rate"),
            "email_recovery_enabled": saved.get("email_recovery_enabled", False),
            "email_subject": saved.get("email_subject", "Your cart is waiting"),
            "email_body": saved.get("email_body", "You left something in your cart. Continue shopping using the link below."),
            "email_ready": bool(os.getenv("RESEND_API_KEY") and os.getenv("COMMERCE_EMAIL_FROM") and s.SHOPIFY_PUBLIC_BASE_URL),
            "email_sender": os.getenv("COMMERCE_EMAIL_FROM", "")}


async def save_settings(org_id, values):
    s = service()
    previous = await s.db.commerce_growth_settings.find_one({"_id": org_id}) or {}
    if values.get("email_recovery_enabled") and not (await settings(org_id))["email_ready"]:
        raise HTTPException(409, "Configure RESEND_API_KEY, COMMERCE_EMAIL_FROM and the public backend URL first")
    activation = previous.get("email_enabled_at") if previous.get("email_recovery_enabled") else s.utcnow()
    await s.db.commerce_growth_settings.update_one({"_id": org_id}, {"$set": {**values, "email_enabled_at": activation, "updated_at": s.utcnow()}}, upsert=True)
    return await settings(org_id)


async def sales_report(org_id, days=30):
    s = service()
    now = s.utcnow()
    config = await settings(org_id)
    cutoff = now - timedelta(days=days + config["attribution_days"])
    touches = []
    carts = await s.db.shopify_checkouts.find({"org_id": org_id, "provider_accepted_at": {"$gte": cutoff}}).to_list(None)
    for cart in carts:
        touches.append({"org_id": org_id, "kind": "recovery", "order_id": cart.get("converted_order_id"),
                        "touch_id": f"whatsapp:{cart.get('provider_message_id') or cart['provider_id']}",
                        "sent_at": cart["provider_accepted_at"], "status": cart.get("delivery_status", "accepted")})
    jobs = await s.db.commerce_campaigns.find({"org_id": org_id, "scheduled_for": {"$gte": cutoff}}).to_list(None)
    by_job = {row["campaign_id"]: row for row in jobs}
    results = await s.db.bulk_job_results.find({"org_id": org_id, "job_id": {"$in": list(by_job)}, "status": "sent"}).to_list(None)
    messages = await s.db[f"messages_{org_id}"].find({"message_id": {"$in": [row.get("message_id") for row in results]}}).to_list(None)
    delivery = {row.get("message_id"): row.get("status", "sent") for row in messages}
    for result in results:
        campaign = by_job[result["job_id"]]
        customer = (campaign.get("recipient_customers") or {}).get(phone_key(result["phone_number"]))
        touches.append({"org_id": org_id, "kind": "campaign", "campaign_id": result["job_id"], "customer_id": customer,
                        "touch_id": f"whatsapp:{result.get('message_id') or result['_id']}",
                        "sent_at": result["created_at"], "status": delivery.get(result.get("message_id"), "accepted")})
    emails = await s.db.commerce_email_sends.find({"org_id": org_id, "status": "accepted", "sent_at": {"$gte": cutoff}}).to_list(None)
    for row in emails:
        cart = await s.db.shopify_checkouts.find_one({"org_id": org_id, "provider_id": row.get("checkout_id")}) if row.get("checkout_id") else None
        touches.append({"org_id": org_id, "kind": "campaign" if row.get("campaign_id") else "recovery",
                        "touch_id": f"email:{row['provider_id']}",
                        "campaign_id": row.get("campaign_id"), "customer_id": row.get("customer_id"),
                        "order_id": (cart or {}).get("converted_order_id"), "sent_at": row["sent_at"], "status": row.get("delivery_status", "email_accepted"), "channel": "email"})
    touches = list({row["touch_id"]: row for row in touches}.values())
    orders = await s.db.shopify_orders.find({"org_id": org_id, "provider_created_at": {"$gte": now - timedelta(days=days)}}).to_list(None)
    # ponytail: bounded reporting period is scanned in memory; move matching to aggregation when monthly volume warrants it.
    report = attribution(orders, touches, now, config["attribution_days"])
    recent = [row for row in touches if moment(row["sent_at"]) >= now - timedelta(days=days)]
    delivered = sum(row.get("channel") != "email" and row["status"] in {"delivered", "read"} for row in recent)
    accepted = sum(row.get("channel") != "email" for row in recent)
    rate = config.get("message_rate")
    campaign_reports = {}
    for entry in report["orders"]:
        if not entry.get("campaign_id"):
            continue
        item = campaign_reports.setdefault(entry["campaign_id"], {"matched_orders": 0, "sales": {}})
        item["matched_orders"] += 1
        net = entry.get("net_payment")
        if net and net.get("currency"):
            item["sales"][net["currency"]] = item["sales"].get(net["currency"], Decimal(0)) + money(net["amount"])
    for item in campaign_reports.values():
        item["sales"] = [{"currency": key, "amount": str(value)} for key, value in sorted(item["sales"].items())]
    report.update({"period_days": days, "attribution_days": config["attribution_days"], "accepted": accepted,
                   "delivered": delivered, "read": sum(row["status"] == "read" for row in recent),
                   "email_accepted": sum(row.get("channel") == "email" for row in recent),
                   "email_delivered": sum(row.get("channel") == "email" and row["status"] in {"delivered", "opened", "clicked"} for row in recent),
                   "cost_estimate": {"amount": str(money(rate["amount"]) * delivered), "currency": rate["currency"]} if rate else None,
                   "conversion_rate": round(100 * report["converted_contacts"] / len(touches), 2) if touches else 0,
                   "conversion_denominator": len(touches), "campaigns": campaign_reports,
                   "orders": sorted(report["orders"], key=lambda row: row["created_at"], reverse=True)[:100]})
    return report


async def connected(org_id):
    connection = await service().db.shopify_connections.find_one({"org_id": org_id, "status": "connected"})
    if not connection:
        raise HTTPException(409, "Connect Shopify first")
    return connection


async def customer_consent(org_id, customer, channel):
    s = service()
    record = await s.db.commerce_marketing_consents.find_one({"_id": f"{org_id}:{customer['id']}:{channel}"})
    address = customer.get("email", "") if channel == "email" else (customer.get("defaultPhoneNumber") or {}).get("phoneNumber", "")
    if not address:
        return False
    if channel == "email":
        if await s.db.commerce_email_suppressions.find_one({"_id": f"{org_id}:{email_key(address)}"}):
            return False
        return bool(record and record.get("granted") and record.get("address_key") == email_key(address)) or bool(
            not record and (customer.get("emailMarketingConsent") or {}).get("marketingState") == "SUBSCRIBED")
    from services.wa_service import get_broadcast_suppressed_phone_keys
    if phone_key(address) in await asyncio.to_thread(get_broadcast_suppressed_phone_keys, org_id, [address]):
        return False
    # Cart-reminder consent does not authorize retention or promotional campaigns.
    return bool(record and record.get("granted") and record.get("address_key") == phone_key(address))


async def audience(org_id, payload, include_ids=False):
    s = service()
    connection = await connected(org_id)
    after = None
    recipients = {}
    excluded = 0
    now = s.utcnow()
    for _ in range(100):
        data = await s._graphql(connection, CUSTOMERS_QUERY, {"first": 100, "after": after})
        page = data.get("customers") or {}
        for customer in page.get("nodes") or []:
            if not segment_matches(customer, payload["segment"], payload["lapsed_days"], now):
                continue
            if not await customer_consent(org_id, customer, payload["channel"]):
                excluded += 1
                continue
            address = customer.get("email", "").strip().lower() if payload["channel"] == "email" else phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
            recipients[address] = customer["id"]
            if len(recipients) > 2000:
                raise HTTPException(422, "This audience exceeds the 2,000-recipient campaign limit")
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return {"eligible": len(recipients), "excluded_without_consent": excluded,
                    **({"recipients": recipients} if include_ids else {})}
        after = info.get("endCursor")
    raise HTTPException(422, "Audience preview exceeded 10,000 customers; use a smaller store audience")


async def campaign_templates(org_id):
    s = service()
    connection = await connected(org_id)
    rows = await s.list_compatible_templates(org_id, connection.get("whatsapp_id"), phone_id=connection.get("whatsapp_phone_id"), purpose="campaign", raise_unavailable=True)
    return [{"name": row["name"], "language": row["language"]} for row in rows]


async def create_campaign(org_id, payload, request_id):
    s = service()
    from services.entitlement_service import require_feature, feature_access
    await asyncio.to_thread(require_feature, org_id, "BROADCASTING")
    identifier = hashlib.sha256(f"{org_id}:{request_id}".encode()).hexdigest()[:32]
    existing = await s.db.commerce_campaigns.find_one({"_id": identifier, "org_id": org_id})
    if existing:
        return public_campaign(existing)
    if payload["channel"] == "whatsapp" and {"name": payload["template_name"], "language": payload["template_language"]} not in await campaign_templates(org_id):
        raise HTTPException(422, "Choose an approved static marketing template")
    if payload["channel"] == "email" and not (await settings(org_id))["email_ready"]:
        raise HTTPException(409, "Email sending is not configured")
    preview = await audience(org_id, payload, True)
    if not preview["eligible"]:
        raise HTTPException(409, "No customers in this segment have permission for this channel")
    access = await asyncio.to_thread(feature_access, org_id, "BROADCASTING")
    limit = access["limits"].get("max_batch_recipients")
    if limit is not None and preview["eligible"] > limit:
        raise HTTPException(422, f"Your plan allows at most {limit} recipients per campaign")
    connection = await connected(org_id)
    row = {**payload, "_id": identifier, "campaign_id": identifier, "org_id": org_id, "shop": connection["shop"],
           "recipient_customers": preview["recipients"], "total": preview["eligible"], "status": "scheduled", "created_at": s.utcnow()}
    try:
        await s.db.commerce_campaigns.insert_one(row)
    except DuplicateKeyError:
        row = await s.db.commerce_campaigns.find_one({"_id": identifier, "org_id": org_id})
    return public_campaign(row)


def public_campaign(row):
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()
            if key not in {"_id", "org_id", "shop", "recipient_customers", "claim_token"}}


async def list_campaigns(org_id):
    s = service()
    rows = await s.db.commerce_campaigns.find({"org_id": org_id}).sort("created_at", -1).limit(50).to_list(50)
    oldest = min((moment(row["created_at"]) for row in rows), default=s.utcnow())
    report = await sales_report(org_id, max(90, (s.utcnow() - oldest).days + 1))
    result = []
    for row in rows:
        item = public_campaign(row)
        job = await s.db.bulk_jobs.find_one({"org_id": org_id, "job_id": row["campaign_id"]}) or {}
        item.update({key: job.get(key, 0) for key in ("sent", "failed", "skipped")})
        if row["channel"] == "email":
            for key, status in (("sent", "accepted"), ("failed", "failed"), ("skipped", "skipped")):
                item[key] = await s.db.commerce_email_sends.count_documents({"org_id": org_id, "campaign_id": row["campaign_id"], "status": status})
        item.update(report["campaigns"].get(row["campaign_id"], {"matched_orders": 0, "sales": []}))
        identifiers = await s.db.bulk_job_results.distinct("message_id", {"org_id": org_id, "job_id": row["campaign_id"], "status": "sent"})
        item["delivered"] = await s.db[f"messages_{org_id}"].count_documents({"message_id": {"$in": identifiers}, "status": {"$in": ["delivered", "read"]}})
        item["read"] = await s.db[f"messages_{org_id}"].count_documents({"message_id": {"$in": identifiers}, "status": "read"})
        if row["channel"] == "email":
            item["delivered"] = await s.db.commerce_email_sends.count_documents({"org_id": org_id, "campaign_id": row["campaign_id"], "delivery_status": {"$in": ["delivered", "opened", "clicked"]}})
            item["bounced"] = await s.db.commerce_email_sends.count_documents({"org_id": org_id, "campaign_id": row["campaign_id"], "delivery_status": {"$in": ["bounced", "suppressed", "complained", "failed"]}})
        result.append(item)
    return result


async def cancel_campaign(org_id, identifier):
    result = await service().db.commerce_campaigns.update_one({"org_id": org_id, "campaign_id": identifier, "status": {"$in": ["scheduled", "running"]}},
                                                            {"$set": {"status": "cancelled"}})
    if not result.matched_count:
        raise HTTPException(409, "This campaign has already finished")


async def recipient_eligible(org_id, identifier, address, reserve=True):
    s = service()
    campaign = await s.db.commerce_campaigns.find_one({"org_id": org_id, "campaign_id": identifier, "status": "running"})
    if not campaign:
        return False
    connection = await connected(org_id)
    if connection["shop"] != campaign["shop"]:
        return False
    customer_id = (campaign.get("recipient_customers") or {}).get(address)
    if not customer_id:
        return False
    customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": customer_id})).get("customer")
    if not customer or not segment_matches(customer, campaign["segment"], campaign["lapsed_days"], s.utcnow()):
        return False
    last = moment((customer.get("lastOrder") or {}).get("createdAt"))
    if last and last >= moment(campaign["created_at"]):
        return False
    current_address = customer.get("email", "").strip().lower() if campaign["channel"] == "email" else phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
    if current_address != address or not await customer_consent(org_id, customer, campaign["channel"]):
        return False
    if campaign["channel"] == "whatsapp" and await s.db.shopify_checkouts.find_one({"org_id": org_id, "phone": f"+{address}",
                                                                               "provider_started_at": {"$gte": s.utcnow() - timedelta(hours=24)}}):
        return False
    if not reserve:
        return True
    return await s.reserve_marketing_contact(org_id, campaign["channel"], address, identifier)


async def process_campaigns():
    s = service()
    now = s.utcnow()
    # Interrupted provider sends are never automatically replayed.
    await s.db.commerce_campaigns.update_many({"status": "running", "started_at": {"$lt": now - timedelta(hours=6)}},
                                            {"$set": {"status": "interrupted", "error": "Worker interrupted; inspect accepted sends before creating another campaign"}})
    rows = await s.db.commerce_campaigns.find({"status": "scheduled", "scheduled_for": {"$lte": now}}).limit(5).to_list(5)
    for row in rows:
        claim = await s.db.commerce_campaigns.update_one({"_id": row["_id"], "status": "scheduled"},
                                                       {"$set": {"status": "running", "started_at": s.utcnow()}})
        if not claim.modified_count:
            continue
        try:
            from services.entitlement_service import consume_quota_async
            org_id, identifier = row["org_id"], row["campaign_id"]
            connection = await connected(org_id)
            if connection["shop"] != row["shop"]:
                raise HTTPException(409, "The store connected to this campaign has changed")
            await consume_quota_async(org_id, "BROADCASTING", "monthly_recipients", row["total"], f"broadcast:{identifier}")
            if row["channel"] == "whatsapp":
                from services.wa_service import create_bulk_job, run_bulk_send_job
                if {"name": row["template_name"], "language": row["template_language"]} not in await campaign_templates(org_id):
                    raise HTTPException(409, "The campaign template is no longer approved")
                sender = await s._selected_whatsapp_connection(await connected(org_id))
                if not sender:
                    raise HTTPException(409, "WhatsApp sender disconnected")
                await asyncio.to_thread(create_bulk_job, org_id, identifier, row["template_name"], row["template_language"], row["total"])
                await run_bulk_send_job(sender["wa_id"], identifier, org_id, sender["phone_id"], sender["access_token"],
                                        row["template_name"], row["template_language"],
                                        [SimpleNamespace(phone_number=address) for address in row["recipient_customers"]],
                                        campaign_id=identifier, max_concurrent=10)
                job = await s.db.bulk_jobs.find_one({"org_id": org_id, "job_id": identifier}) or {}
                if job.get("status") != "completed":
                    raise HTTPException(409, "Sending interrupted; inspect the delivery report")
            else:
                for address, customer_id in row["recipient_customers"].items():
                    eligible = await recipient_eligible(org_id, identifier, address)
                    await send_email(org_id, f"campaign:{identifier}:{email_key(address)}", address, customer_id,
                                     row["email_subject"], row["email_body"], campaign_id=identifier, eligible=eligible)
            await s.db.commerce_campaigns.update_one({"_id": identifier, "status": "running"}, {"$set": {"status": "completed", "completed_at": s.utcnow()}})
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else (detail.get("message") if isinstance(detail, dict) else None)
            await s.db.commerce_campaigns.update_one({"_id": row["_id"], "status": "running"},
                                                   {"$set": {"status": "failed", "error": str(message or "Campaign could not finish; inspect its send report")[:500]}})


async def send_email(org_id, send_id, address, customer_id, subject, body, *, campaign_id=None, eligible=True, recovery_url=None, checkout_id=None):
    s = service()
    token = secrets.token_urlsafe(32)
    now = s.utcnow()
    row = {"_id": f"{org_id}:{send_id}", "org_id": org_id, "campaign_id": campaign_id, "customer_id": customer_id, "checkout_id": checkout_id,
           "address_key": email_key(address), "unsubscribe_hash": hashlib.sha256(token.encode()).hexdigest(),
           "status": "sending" if eligible else "skipped", "created_at": now}
    try:
        await s.db.commerce_email_sends.insert_one(row)
    except DuplicateKeyError:
        return
    if not eligible:
        return
    api_key, sender = os.getenv("RESEND_API_KEY"), os.getenv("COMMERCE_EMAIL_FROM")
    if not api_key or not sender or not s.SHOPIFY_PUBLIC_BASE_URL:
        await s.db.commerce_email_sends.update_one({"_id": row["_id"]}, {"$set": {"status": "failed", "error": "Email sender is not configured"}})
        return
    unsubscribe = f"{s.SHOPIFY_PUBLIC_BASE_URL.rstrip('/')}/api/commerce/email/unsubscribe/{token}"
    text = body + (f"\n\nContinue shopping: {recovery_url}" if recovery_url else "") + f"\n\nUnsubscribe: {unsubscribe}"
    try:
        connection = await connected(org_id)
        if campaign_id:
            still_eligible = await recipient_eligible(org_id, campaign_id, address, reserve=False)
        else:
            cart = await s.db.shopify_checkouts.find_one({"org_id": org_id, "provider_id": checkout_id, "record_status": "abandoned"})
            customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": customer_id})).get("customer")
            config = await settings(org_id)
            last = moment(((customer or {}).get("lastOrder") or {}).get("createdAt"))
            still_eligible = bool(config["email_recovery_enabled"] and cart and customer
                                  and (not last or last < moment(cart["provider_created_at"]))
                                  and await customer_consent(org_id, customer, "email")
                                  and customer.get("email", "").lower() == address.lower())
        if not still_eligible:
            await s.db.commerce_email_sends.update_one({"_id": row["_id"]}, {"$set": {"status": "skipped"}})
            return
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {api_key}",
                                          "Idempotency-Key": hashlib.sha256(row["_id"].encode()).hexdigest()},
                                         json={"from": sender, "to": [address], "subject": subject, "text": text,
                                               "headers": {"List-Unsubscribe": f"<{unsubscribe}>"}})
            response.raise_for_status()
            identifier = response.json().get("id")
            if not identifier:
                raise ValueError("Missing email provider ID")
        await s.db.commerce_email_sends.update_one({"_id": row["_id"]}, {"$set": {"status": "accepted", "provider_id": identifier, "sent_at": s.utcnow()}})
    except Exception:
        await s.db.commerce_email_sends.update_one({"_id": row["_id"]}, {"$set": {"status": "failed", "error": "Provider outcome could not be confirmed; automatic retry disabled"}})


async def unsubscribe_email(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,60}", token):
        raise HTTPException(404, "Unsubscribe link not found")
    s = service()
    row = await s.db.commerce_email_sends.find_one({"unsubscribe_hash": hashlib.sha256(token.encode()).hexdigest()})
    if not row:
        raise HTTPException(404, "Unsubscribe link not found")
    await s.db.commerce_email_suppressions.update_one({"_id": f"{row['org_id']}:{row['address_key']}"},
                                                   {"$set": {"org_id": row["org_id"], "updated_at": s.utcnow()}}, upsert=True)


async def process_email_recovery():
    s = service()
    configs = await s.db.commerce_growth_settings.find({"email_recovery_enabled": True}).to_list(None)
    for config in configs:
        org_id = config["_id"]
        connection = await s.db.shopify_connections.find_one({"org_id": org_id, "status": "connected"})
        if not connection or not (await settings(org_id))["email_ready"]:
            continue
        carts = s.db.shopify_checkouts.find({"org_id": org_id, "record_status": "abandoned", "email_recovery_attempted": {"$ne": True}, "customer_id": {"$nin": [None, ""]},
                                                 "provider_created_at": {"$gte": config.get("email_enabled_at", config["updated_at"])},
                                                 "provider_updated_at": {"$lte": s.utcnow() - timedelta(minutes=connection.get("reminder_delay_minutes", 10))}})
        attempted = 0
        async for cart in carts:
            if attempted >= 20:
                break
            send_id = f"recovery:{cart['provider_id']}"
            if await s.db.commerce_email_sends.find_one({"_id": f"{org_id}:{send_id}"}):
                await s.db.shopify_checkouts.update_one({"org_id": org_id, "provider_id": cart["provider_id"]}, {"$set": {"email_recovery_attempted": True}})
                continue
            customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": cart["customer_id"]})).get("customer")
            if not customer or not await customer_consent(org_id, customer, "email"):
                continue
            last = moment((customer.get("lastOrder") or {}).get("createdAt"))
            if last and last >= moment(cart["provider_created_at"]):
                continue
            current = await s.db.shopify_checkouts.find_one({"org_id": org_id, "provider_id": cart["provider_id"], "record_status": "abandoned"})
            if not current or not cart.get("recovery_url", "").startswith(f"https://{connection['shop']}/"):
                continue
            if not await s.reserve_marketing_contact(org_id, "email", customer["email"].lower(), send_id):
                continue
            from services.entitlement_service import consume_quota_async
            await consume_quota_async(org_id, "BROADCASTING", "monthly_recipients", 1, f"email:{org_id}:{send_id}")
            await send_email(org_id, send_id, customer["email"], customer["id"], config["email_subject"], config["email_body"], recovery_url=cart["recovery_url"], checkout_id=cart["provider_id"])
            await s.db.shopify_checkouts.update_one({"org_id": org_id, "provider_id": cart["provider_id"]}, {"$set": {"email_recovery_attempted": True}})
            attempted += 1


async def process_email_statuses():
    s = service()
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        return
    now = s.utcnow()
    await s.db.commerce_email_sends.update_many({"status": "sending", "created_at": {"$lt": now - timedelta(hours=1)}},
                                              {"$set": {"status": "failed", "error": "Worker interrupted; provider outcome unknown and automatic retry disabled"}})
    # ponytail: poll ten recent receipts per minute; replace with signed webhooks if volume needs faster reconciliation.
    rows = await s.db.commerce_email_sends.find({"status": "accepted", "sent_at": {"$gte": now - timedelta(days=14)},
        "delivery_status": {"$nin": ["bounced", "complained", "suppressed", "failed"]},
        "$or": [{"next_status_check_at": {"$lte": now}}, {"next_status_check_at": {"$exists": False}}]}).sort("next_status_check_at", 1).limit(10).to_list(10)
    async with httpx.AsyncClient(timeout=15) as client:
        for row in rows:
            claim = await s.db.commerce_email_sends.update_one({"_id": row["_id"], "$or": [{"next_status_check_at": {"$lte": now}}, {"next_status_check_at": {"$exists": False}}]},
                                                             {"$set": {"next_status_check_at": now + timedelta(minutes=30)}})
            if not claim.modified_count:
                continue
            try:
                response = await client.get(f"https://api.resend.com/emails/{row['provider_id']}", headers={"Authorization": f"Bearer {api_key}"})
                response.raise_for_status()
                status = response.json().get("last_event")
                if status not in {"sent", "delivered", "opened", "clicked", "delivery_delayed", "bounced", "complained", "suppressed", "failed"}:
                    continue
                await s.db.commerce_email_sends.update_one({"_id": row["_id"]}, {"$set": {"delivery_status": status, "status_checked_at": now}})
                if status in {"bounced", "complained", "suppressed", "failed"}:
                    await s.db.commerce_email_suppressions.update_one({"_id": f"{row['org_id']}:{row['address_key']}"},
                        {"$set": {"org_id": row["org_id"], "reason": status, "updated_at": now}}, upsert=True)
            except (httpx.HTTPError, ValueError, KeyError):
                continue


async def capture_marketing_consent(query, raw):
    from services.shopify_storefront_cart import authenticated_customer
    import json
    s = service()
    shop, customer_id = authenticated_customer(query, raw, s.SHOPIFY_CLIENT_SECRET, s.utcnow().timestamp())
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid consent payload") from None
    if not isinstance(payload, dict) or set(payload) != {"channel", "granted"} or payload["channel"] not in {"whatsapp", "email"} or type(payload["granted"]) is not bool:
        raise HTTPException(422, "Choose a channel and explicit consent")
    connection = await s.db.shopify_connections.find_one({"shop": shop, "status": "connected"})
    if not connection:
        raise HTTPException(409, "Store integration is disabled")
    customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": customer_id})).get("customer")
    if not customer:
        raise HTTPException(401, "Store customer could not be verified")
    channel = payload["channel"]
    address = customer.get("email") if channel == "email" else phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
    if not address:
        raise HTTPException(422, f"Add your {channel} address to your store account first")
    org_id = connection["org_id"]
    await s.db.commerce_marketing_consents.update_one({"_id": f"{org_id}:{customer_id}:{channel}"}, {"$set": {
        "org_id": org_id, "customer_id": customer_id, "channel": channel, "granted": payload["granted"],
        "address_key": email_key(address) if channel == "email" else address, "source": "storefront_marketing_checkbox_v1", "updated_at": s.utcnow()}}, upsert=True)
    # An existing STOP or email unsubscribe remains authoritative; a checkbox cannot silently clear it.
    return {"status": "subscribed" if payload["granted"] else "unsubscribed"}


async def redact_customer(org_id, customer_ids):
    s = service()
    receipts = await s.db.commerce_email_sends.find({"org_id": org_id, "customer_id": {"$in": customer_ids}}, {"address_key": 1}).to_list(None)
    await s.db.commerce_email_suppressions.delete_many({"org_id": org_id, "_id": {"$in": [f"{org_id}:{row['address_key']}" for row in receipts if row.get("address_key")]}})
    await s.db.commerce_marketing_consents.delete_many({"org_id": org_id, "customer_id": {"$in": customer_ids}})
    await s.db.commerce_email_sends.delete_many({"org_id": org_id, "customer_id": {"$in": customer_ids}})
    for campaign in await s.db.commerce_campaigns.find({"org_id": org_id}).to_list(None):
        recipients = {address: identifier for address, identifier in campaign.get("recipient_customers", {}).items() if identifier not in customer_ids}
        await s.db.commerce_campaigns.update_one({"org_id": org_id, "campaign_id": campaign["campaign_id"]}, {"$set": {"recipient_customers": recipients}})


async def remove_store_data(org_id):
    s = service()
    for name in ("commerce_campaigns", "commerce_marketing_consents", "commerce_email_sends", "commerce_email_suppressions", "commerce_contact_limits", "commerce_knowledge_sources"):
        await s.db[name].delete_many({"org_id": org_id})
    await s.db.commerce_growth_settings.delete_one({"_id": org_id})
    await s.db.commerce_knowledge_settings.delete_one({"_id": org_id})
