"""Shopify sales attribution and scheduled, consented purchase campaigns."""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


CUSTOMERS_QUERY = """query GrowthCustomers($first: Int!, $after: String) {
  customers(first: $first, after: $after) {
    nodes { id displayName numberOfOrders
      defaultPhoneNumber { phoneNumber } lastOrder { createdAt } }
    pageInfo { hasNextPage endCursor }
  }
}"""
CUSTOMER_QUERY = """query GrowthCustomer($id: ID!) {
  customer(id: $id) { id displayName numberOfOrders
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
            if touch.get("kind") in {"recovery", "retargeting"}:
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
    return {"attribution_days": saved.get("attribution_days", 7), "message_rate": saved.get("message_rate")}


async def save_settings(org_id, values):
    s = service()
    await s.db.commerce_growth_settings.update_one({"_id": org_id}, {"$set": {**values, "updated_at": s.utcnow()}}, upsert=True)
    return await settings(org_id)


async def sales_report(org_id, days=30):
    from services.shopify_reporting import report
    data = await report(org_id, days=min(days, 366), compare=False)
    summary = data["summary"]
    campaigns = {row["campaign_id"]: {"matched_orders": row["matched_orders"], "sales": row["net_payments"]} for row in data["campaigns"]}
    return {**summary, "period_days": days, "attribution_days": data["attribution_days"], "sales": summary["net_payments"],
            "cost_estimate": summary["cost_estimates"][0] if len(summary["cost_estimates"]) == 1 else None,
            "campaigns": campaigns, "orders": data["orders"][:100], "coverage": data["coverage"],
            "pending_contacts": summary["pending_contacts"], "period": data["period"]}


async def connected(org_id):
    connection = await service().db.shopify_connections.find_one({"org_id": org_id, "status": "connected"})
    if not connection:
        raise HTTPException(409, "Connect Shopify first")
    return connection


async def customer_consent(org_id, customer, channel):
    s = service()
    record = await s.db.commerce_marketing_consents.find_one({"_id": f"{org_id}:{customer['id']}:{channel}"})
    if channel != "whatsapp":
        return False
    address = (customer.get("defaultPhoneNumber") or {}).get("phoneNumber", "")
    if not address:
        return False
    from services.wa_service import get_broadcast_suppressed_phone_keys
    if phone_key(address) in await asyncio.to_thread(get_broadcast_suppressed_phone_keys, org_id, [address]):
        return False
    # Cart-reminder consent does not authorize retention or promotional campaigns.
    return bool(record and record.get("granted") and record.get("address_key") == phone_key(address))


async def audience(org_id, payload, include_ids=False):
    if payload.get("channel") != "whatsapp":
        raise HTTPException(422, "Only WhatsApp campaigns are supported")
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
            address = phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
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
    return [{"name": row["name"], "language": row["language"], **({"tracking_button_index": row["tracking_button_index"]} if row.get("tracking_button_index") is not None else {})} for row in rows]


async def create_campaign(org_id, payload, request_id):
    if payload.get("channel") != "whatsapp":
        raise HTTPException(422, "Only WhatsApp campaigns are supported")
    s = service()
    from services.entitlement_service import require_feature, feature_access
    await asyncio.to_thread(require_feature, org_id, "BROADCASTING")
    identifier = hashlib.sha256(f"{org_id}:{request_id}".encode()).hexdigest()[:32]
    existing = await s.db.commerce_campaigns.find_one({"_id": identifier, "org_id": org_id})
    if existing:
        return public_campaign(existing)
    selected = next((item for item in await campaign_templates(org_id) if item["name"] == payload["template_name"] and item["language"] == payload["template_language"]), None)
    if not selected:
        raise HTTPException(422, "Choose an approved compatible marketing template")
    preview = await audience(org_id, payload, True)
    if not preview["eligible"]:
        raise HTTPException(409, "No customers in this segment have permission for this channel")
    access = await asyncio.to_thread(feature_access, org_id, "BROADCASTING")
    limit = access["limits"].get("max_batch_recipients")
    if limit is not None and preview["eligible"] > limit:
        raise HTTPException(422, f"Your plan allows at most {limit} recipients per campaign")
    connection = await connected(org_id)
    row = {**payload, "_id": identifier, "campaign_id": identifier, "org_id": org_id, "shop": connection["shop"],
           "tracking_button_index": selected.get("tracking_button_index"), "recipient_customers": preview["recipients"], "total": preview["eligible"], "status": "scheduled", "created_at": s.utcnow(), "message_rate": (await settings(org_id)).get("message_rate")}
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
    rows = await s.db.commerce_campaigns.find({"org_id": org_id, "channel": "whatsapp"}).sort("created_at", -1).limit(50).to_list(50)
    oldest = min((moment(row["created_at"]) for row in rows), default=s.utcnow())
    report = await sales_report(org_id, max(90, (s.utcnow() - oldest).days + 1))
    result = []
    for row in rows:
        item = public_campaign(row)
        job = await s.db.bulk_jobs.find_one({"org_id": org_id, "job_id": row["campaign_id"]}) or {}
        item.update({key: job.get(key, 0) for key in ("sent", "failed", "skipped")})
        item.update(report["campaigns"].get(row["campaign_id"], {"matched_orders": 0, "sales": []}))
        identifiers = await s.db.bulk_job_results.distinct("message_id", {"org_id": org_id, "job_id": row["campaign_id"], "status": "sent"})
        item["delivered"] = await s.db[f"messages_{org_id}"].count_documents({"message_id": {"$in": identifiers}, "status": {"$in": ["delivered", "read"]}})
        item["read"] = await s.db[f"messages_{org_id}"].count_documents({"message_id": {"$in": identifiers}, "status": "read"})
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
    if not campaign or campaign.get("channel") != "whatsapp":
        return False
    connection = await connected(org_id)
    if connection["shop"] != campaign["shop"]:
        return False
    customer_id = (campaign.get("recipient_customers") or {}).get(address)
    if not customer_id:
        return False
    if await s.db.commerce_experiment_assignments.find_one({"org_id": org_id, "customer_id": customer_id, "outcome_ends_at": {"$gt": s.utcnow()}}):
        return False
    customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": customer_id})).get("customer")
    if not customer or not segment_matches(customer, campaign["segment"], campaign["lapsed_days"], s.utcnow()):
        return False
    last = moment((customer.get("lastOrder") or {}).get("createdAt"))
    if last and last >= moment(campaign["created_at"]):
        return False
    current_address = phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
    if current_address != address or not await customer_consent(org_id, customer, campaign["channel"]):
        return False
    if campaign["channel"] == "whatsapp" and await s.db.shopify_checkouts.find_one({"org_id": org_id, "phone": f"+{address}",
                                                                               "provider_started_at": {"$gte": s.utcnow() - timedelta(hours=24)}}):
        return False
    if not reserve:
        return True
    return await s.reserve_marketing_contact(org_id, campaign["channel"], address, identifier)


async def recipient_components(org_id, campaign_id, address):
    s = service()
    row = await s.db.commerce_campaigns.find_one({"org_id": org_id, "campaign_id": campaign_id, "status": "running"})
    if not row or row.get("tracking_button_index") is None:
        return None
    from services.shopify_reporting import tracked_link
    reference = f"{campaign_id}:{hashlib.sha256(address.encode()).hexdigest()[:20]}"
    link = await tracked_link(org_id, row["shop"], "campaign", reference, f"https://{row['shop']}/", (row.get("recipient_customers") or {}).get(address))
    return [{"type": "button", "sub_type": "url", "index": str(row["tracking_button_index"]), "parameters": [{"type": "text", "text": link.rsplit("/", 1)[-1]}]}]


async def process_campaigns():
    s = service()
    now = s.utcnow()
    # Cancel any campaigns queued before email sending was removed.
    await s.db.commerce_campaigns.update_many({"channel": {"$ne": "whatsapp"}, "status": {"$in": ["scheduled", "running"]}},
                                            {"$set": {"status": "cancelled", "error": "Only WhatsApp campaigns are supported"}})
    # Interrupted provider sends are never automatically replayed.
    await s.db.commerce_campaigns.update_many({"status": "running", "started_at": {"$lt": now - timedelta(hours=6)}},
                                            {"$set": {"status": "interrupted", "error": "Worker interrupted; inspect accepted sends before creating another campaign"}})
    rows = await s.db.commerce_campaigns.find({"channel": "whatsapp", "status": "scheduled", "scheduled_for": {"$lte": now}}).limit(5).to_list(5)
    for row in rows:
        claim = await s.db.commerce_campaigns.update_one({"_id": row["_id"], "status": "scheduled"},
                                                       {"$set": {"status": "running", "started_at": s.utcnow(), "message_rate": (await settings(row["org_id"])).get("message_rate")}})
        if not claim.modified_count:
            continue
        try:
            from services.entitlement_service import consume_quota_async
            org_id, identifier = row["org_id"], row["campaign_id"]
            connection = await connected(org_id)
            if connection["shop"] != row["shop"]:
                raise HTTPException(409, "The store connected to this campaign has changed")
            await consume_quota_async(org_id, "BROADCASTING", "monthly_recipients", row["total"], f"broadcast:{identifier}")
            from services.wa_service import create_bulk_job, run_bulk_send_job
            if not any(item["name"] == row["template_name"] and item["language"] == row["template_language"] for item in await campaign_templates(org_id)):
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
            await s.db.commerce_campaigns.update_one({"_id": identifier, "status": "running"}, {"$set": {"status": "completed", "completed_at": s.utcnow()}})
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else (detail.get("message") if isinstance(detail, dict) else None)
            await s.db.commerce_campaigns.update_one({"_id": row["_id"], "status": "running"},
                                                   {"$set": {"status": "failed", "error": str(message or "Campaign could not finish; inspect its send report")[:500]}})


async def capture_marketing_consent(query, raw):
    from services.shopify_storefront_cart import authenticated_customer
    import json
    s = service()
    shop, customer_id = authenticated_customer(query, raw, s.SHOPIFY_CLIENT_SECRET, s.utcnow().timestamp())
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Invalid consent payload") from None
    if not isinstance(payload, dict) or set(payload) != {"channel", "granted"} or payload["channel"] != "whatsapp" or type(payload["granted"]) is not bool:
        raise HTTPException(422, "Choose a channel and explicit consent")
    connection = await s.db.shopify_connections.find_one({"shop": shop, "status": "connected"})
    if not connection:
        raise HTTPException(409, "Store integration is disabled")
    customer = (await s._graphql(connection, CUSTOMER_QUERY, {"id": customer_id})).get("customer")
    if not customer:
        raise HTTPException(401, "Store customer could not be verified")
    channel = payload["channel"]
    address = phone_key((customer.get("defaultPhoneNumber") or {}).get("phoneNumber"))
    if not address:
        raise HTTPException(422, f"Add your {channel} address to your store account first")
    org_id = connection["org_id"]
    await s.db.commerce_marketing_consents.update_one({"_id": f"{org_id}:{customer_id}:{channel}"}, {"$set": {
        "org_id": org_id, "customer_id": customer_id, "channel": channel, "granted": payload["granted"],
        "address_key": address, "source": "storefront_marketing_checkbox_v1", "updated_at": s.utcnow()}}, upsert=True)
    # An existing STOP remains authoritative; a checkbox cannot silently clear it.
    return {"status": "subscribed" if payload["granted"] else "unsubscribed"}


async def redact_customer(org_id, customer_ids):
    s = service()
    from services.shopify_reporting import COLLECTIONS
    attempts = await s.db.commerce_message_attempts.find({"org_id": org_id, "customer_id": {"$in": customer_ids}}).to_list(None)
    jobs = await s.db.commerce_retargeting_jobs.find({"org_id": org_id, "customer_id": {"$in": customer_ids}}).to_list(None)
    links = await s.db.commerce_tracked_links.find({"org_id": org_id, "customer_id": {"$in": customer_ids}}).to_list(None)
    ids = [row["message_id"] for row in attempts] + [row.get("provider_message_id") for row in jobs]
    await s.db.commerce_delivery_receipts.delete_many({"org_id": org_id, "message_id": {"$in": ids}})
    await s.db.commerce_pixel_events.delete_many({"org_id": org_id, "touch_reference": {"$in": [row["reference"] for row in links]}})
    for name in COLLECTIONS:
        await s.db[name].delete_many({"org_id": org_id, "customer_id": {"$in": customer_ids}})
    # Weekly aggregate exports can contain customer drilldowns. Remove snapshots on redaction.
    await s.db.commerce_weekly_reports.delete_many({"org_id": org_id})
    # Retain deletion of historical email records for customer privacy.
    receipts = await s.db.commerce_email_sends.find({"org_id": org_id, "customer_id": {"$in": customer_ids}}, {"address_key": 1}).to_list(None)
    await s.db.commerce_email_suppressions.delete_many({"org_id": org_id, "_id": {"$in": [f"{org_id}:{row['address_key']}" for row in receipts if row.get("address_key")]}})
    await s.db.commerce_marketing_consents.delete_many({"org_id": org_id, "customer_id": {"$in": customer_ids}})
    await s.db.commerce_email_sends.delete_many({"org_id": org_id, "customer_id": {"$in": customer_ids}})
    for campaign in await s.db.commerce_campaigns.find({"org_id": org_id}).to_list(None):
        recipients = {address: identifier for address, identifier in campaign.get("recipient_customers", {}).items() if identifier not in customer_ids}
        await s.db.commerce_campaigns.update_one({"org_id": org_id, "campaign_id": campaign["campaign_id"]}, {"$set": {"recipient_customers": recipients}})


async def remove_store_data(org_id):
    s = service()
    from services.shopify_reporting import COLLECTIONS
    for name in (*COLLECTIONS, "commerce_campaigns", "commerce_marketing_consents", "commerce_email_sends", "commerce_email_suppressions", "commerce_contact_limits", "commerce_knowledge_sources"):
        await s.db[name].delete_many({"org_id": org_id})
    await s.db.commerce_growth_settings.delete_one({"_id": org_id})
    await s.db.commerce_knowledge_settings.delete_one({"_id": org_id})
