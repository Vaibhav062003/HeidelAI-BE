"""Owner reports, journey holdouts and consented browser measurement."""
import csv
import hashlib
import io
import json
import math
import secrets
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from services import shopify_growth as growth

COLLECTIONS = ("commerce_delivery_receipts", "commerce_message_attempts", "commerce_retargeting_jobs", "commerce_retargeting_settings",
               "commerce_experiments", "commerce_experiment_assignments", "commerce_tracked_links", "commerce_pixel_events", "commerce_weekly_reports", "commerce_pixel_installations")


def identifier(*parts):
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def totals(items):
    result = defaultdict(Decimal)
    for item in items:
        if item and item.get("currency"):
            result[item["currency"]] += growth.money(item.get("amount"))
    return [{"currency": key, "amount": str(value)} for key, value in sorted(result.items())]


def net_payment(order):
    return order.get("net_payment") or (order.get("total") if order.get("payment_status") == "PAID" else None)


async def shop_timezone(org_id):
    s = growth.service()
    connection = await growth.connected(org_id)
    value = connection.get("iana_timezone")
    if not value:
        try:
            value = ((await s._graphql(connection, "query ReportShop { shop { ianaTimezone } }", {})).get("shop") or {}).get("ianaTimezone")
            if value:
                ZoneInfo(value)
                await s.db.shopify_connections.update_one({"_id": connection["_id"]}, {"$set": {"iana_timezone": value}})
        except (HTTPException, ZoneInfoNotFoundError):
            value = None
    try:
        return ZoneInfo(value or "UTC"), not bool(value)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC"), True


async def date_range(org_id, start=None, end=None, days=30):
    zone, fallback = await shop_timezone(org_id)
    now = growth.service().utcnow()
    today = now.astimezone(zone).date()
    first = start or today - timedelta(days=days - 1)
    last = end or today
    if first > last or (last - first).days > 365 or last > today:
        raise HTTPException(422, "Choose up to 366 days ending today or earlier")
    return datetime.combine(first, time.min, zone).astimezone(timezone.utc), datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(timezone.utc), str(zone), fallback


async def touches(org_id, start, end):
    s = growth.service()
    carts = await s.db.shopify_checkouts.find({"org_id": org_id, "provider_accepted_at": {"$gte": start, "$lt": end}}).to_list(None)
    attempts = await s.db.commerce_message_attempts.find({"org_id": org_id, "accepted_at": {"$gte": start, "$lt": end}}).to_list(None)
    by_message = {row["message_id"]: row for row in attempts}
    cart_map = {row["provider_id"]: row for row in carts}
    # Preserve accepted attempts even when a failed delivery clears the checkout's current provider ID.
    for attempt in attempts:
        if attempt["checkout_id"] not in cart_map:
            cart = await s.db.shopify_checkouts.find_one({"org_id": org_id, "provider_id": attempt["checkout_id"]})
            if cart:
                cart_map[attempt["checkout_id"]] = cart
    rows = []
    originals = {cart.get("provider_message_id"): {"message_id": cart.get("provider_message_id"), "checkout_id": cart["provider_id"], "accepted_at": cart["provider_accepted_at"]}
                 for cart in carts if cart.get("provider_message_id")}
    originals.update(by_message)
    for attempt in originals.values():
        cart = cart_map.get(attempt["checkout_id"], {})
        rows.append({"org_id": org_id, "kind": "recovery", "order_id": cart.get("converted_order_id"), "checkout_id": attempt["checkout_id"],
                     "customer_id": cart.get("customer_id"), "touch_id": f"whatsapp:{attempt['message_id']}", "message_id": attempt["message_id"],
                     "sent_at": attempt["accepted_at"], "status": cart.get("delivery_status", "accepted") if cart.get("provider_message_id") == attempt["message_id"] else "accepted",
                     "message_rate": attempt.get("message_rate"), "rate_recorded": attempt["message_id"] in by_message})
    jobs = await s.db.commerce_retargeting_jobs.find({"org_id": org_id, "status": "accepted", "provider_accepted_at": {"$gte": start, "$lt": end}}).to_list(None)
    for job in jobs:
        cart = cart_map.get(job["checkout_id"]) or await s.db.shopify_checkouts.find_one({"org_id": org_id, "provider_id": job["checkout_id"]}) or {}
        rows.append({"org_id": org_id, "kind": "retargeting", "branch": job.get("branch"), "order_id": cart.get("converted_order_id"),
                     "customer_id": job.get("customer_id"), "checkout_id": job["checkout_id"], "touch_id": f"whatsapp:{job['provider_message_id']}", "message_id": job["provider_message_id"],
                     "sent_at": job["provider_accepted_at"], "status": job.get("delivery_status", "accepted"), "message_rate": job.get("message_rate"), "rate_recorded": True})
    campaigns = await s.db.commerce_campaigns.find({"org_id": org_id, "channel": "whatsapp"}).to_list(None)
    by_campaign = {row["campaign_id"]: row for row in campaigns}
    results = await s.db.bulk_job_results.find({"org_id": org_id, "job_id": {"$in": list(by_campaign)}, "status": "sent", "created_at": {"$gte": start, "$lt": end}}).to_list(None)
    messages = await s.db[f"messages_{org_id}"].find({"message_id": {"$in": [row.get("message_id") for row in results]}}).to_list(None)
    delivery = {row.get("message_id"): row.get("status", "sent") for row in messages}
    for row in results:
        campaign = by_campaign[row["job_id"]]
        rows.append({"org_id": org_id, "kind": "campaign", "campaign_id": row["job_id"], "customer_id": (campaign.get("recipient_customers") or {}).get(growth.phone_key(row["phone_number"])),
                     "touch_id": f"whatsapp:{row.get('message_id') or row['_id']}", "message_id": row.get("message_id"), "sent_at": row["created_at"], "status": delivery.get(row.get("message_id"), "accepted"),
                     "message_rate": campaign.get("message_rate"), "rate_recorded": "message_rate" in campaign})
    receipts = await s.db.commerce_delivery_receipts.find({"org_id": org_id, "message_id": {"$in": [row["message_id"] for row in rows]}}).to_list(None)
    from services.shopify_retargeting import engagement
    for row in rows:
        receipt_rows = [receipt for receipt in receipts if receipt["message_id"] == row["message_id"]]
        if receipt_rows:
            row.update(engagement(receipt_rows, growth.moment(row["sent_at"]), s.utcnow()))
    return list({row["touch_id"]: row for row in rows}.values())


def conversion_interval(successes, count):
    if not count:
        return None
    z = 1.96
    p = successes / count
    center = (p + z*z / (2*count)) / (1 + z*z/count)
    radius = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / (1 + z*z/count)
    return [round(100*max(0, center-radius), 2), round(100*min(1, center+radius), 2)]


async def enroll(org_id, cart):
    s = growth.service()
    prior = await s.db.commerce_experiment_assignments.find_one({"org_id": org_id, "customer_id": cart.get("customer_id"), "outcome_ends_at": {"$gt": s.utcnow()}})
    if prior:
        return prior["group"] == "control"
    experiment = await s.db.commerce_experiments.find_one({"org_id": org_id, "status": "running"})
    if not experiment or not cart.get("customer_id"):
        return False
    if experiment["shop"] != cart.get("shop"):
        return False
    key = identifier(org_id, experiment["experiment_id"], cart["customer_id"])
    assignment = await s.db.commerce_experiment_assignments.find_one({"_id": key})
    if not assignment:
        # Persist random assignment once. Duplicate inserts keep the existing winner.
        value = {"_id": key, "org_id": org_id, "experiment_id": experiment["experiment_id"], "customer_id": cart["customer_id"],
                 "group": "control" if secrets.randbelow(100) < experiment["holdout_percent"] else "treatment",
                 "enrolled_at": s.utcnow(), "outcome_ends_at": s.utcnow() + timedelta(days=7)}
        try:
            await s.db.commerce_experiment_assignments.insert_one(value)
            assignment = value
        except DuplicateKeyError:
            assignment = await s.db.commerce_experiment_assignments.find_one({"_id": key})
    return assignment["group"] == "control"


def window_payment(order, start, end):
    net = net_payment(order)
    if not net:
        return None
    captured = []
    for tx in order.get("payment_transactions") or []:
        when = growth.moment(tx.get("processed_at"))
        bag = tx.get("amount") or {}
        if tx.get("kind") in {"SALE", "CAPTURE"} and tx.get("status") == "SUCCESS" and when and bag.get("currencyCode") == net.get("currency"):
            captured.append((when, growth.money(bag.get("amount"))))
    if not captured:
        return None
    within = sum((amount for when, amount in captured if start <= when <= end), Decimal(0))
    all_paid = sum((amount for _, amount in captured), Decimal(0))
    refunds = max(Decimal(0), all_paid-growth.money(net["amount"]))
    return {"amount": str(max(Decimal(0), within-refunds)), "currency": net["currency"]} if within else None


async def experiment_report(org_id, experiment):
    s = growth.service()
    rows = await s.db.commerce_experiment_assignments.find({"org_id": org_id, "experiment_id": experiment["experiment_id"]}).to_list(None)
    orders = await s.db.shopify_orders.find({"org_id": org_id, "customer_id": {"$in": [row["customer_id"] for row in rows]}}).to_list(None)
    groups = []
    for group in ("control", "treatment"):
        assigned = [row for row in rows if row["group"] == group]
        mature = [row for row in assigned if growth.moment(row["outcome_ends_at"]) <= s.utcnow()]
        purchased = 0
        payments = []
        unknown = 0
        for row in mature:
            eligible_orders = [order for order in orders if order.get("customer_id") == row["customer_id"] and not order.get("cancelled_at") and order.get("record_status") != "deleted"
                               and growth.moment(order.get("provider_created_at") or order.get("created_at")) and row["enrolled_at"] <= growth.moment(order.get("provider_created_at") or order.get("created_at")) <= row["outcome_ends_at"]]
            paid = [order for order in eligible_orders if growth.moment(order.get("paid_at")) and row["enrolled_at"] <= growth.moment(order["paid_at"]) <= row["outcome_ends_at"] and net_payment(order) is not None]
            purchased += bool(paid)
            unknown += sum(net_payment(order) is not None and not order.get("paid_at") for order in eligible_orders)
            payments.extend(window_payment(order, row["enrolled_at"], row["outcome_ends_at"]) for order in paid)
            unknown += sum(window_payment(order, row["enrolled_at"], row["outcome_ends_at"]) is None for order in paid)
        groups.append({"group": group, "enrolled": len(assigned), "mature": len(mature), "pending": len(assigned)-len(mature), "paid_purchasers": purchased,
                       "conversion_rate": round(100*purchased/len(mature), 2) if mature else None, "conversion_interval": conversion_interval(purchased, len(mature)),
                       "net_payments": totals(payments), "payment_timing_unavailable": unknown,
                       "payments_per_enrolled": [{**value, "amount": str(Decimal(value["amount"])/len(mature))} for value in totals(payments)] if mature else []})
    control, treatment = groups
    difference = treatment["conversion_rate"] - control["conversion_rate"] if control["conversion_rate"] is not None and treatment["conversion_rate"] is not None else None
    # Conservative interval for the difference of two Wilson intervals.
    interval = [round(treatment["conversion_interval"][0]-control["conversion_interval"][1], 2), round(treatment["conversion_interval"][1]-control["conversion_interval"][0], 2)] if difference is not None else None
    return {**{key: value for key, value in experiment.items() if key != "_id"}, "groups": groups, "conversion_difference_points": difference,
            "difference_interval": interval, "outcome_days": 7, "interim": experiment["status"] == "running" or any(group["pending"] for group in groups),
            "definition": "Entire recovery journey versus no recovery messages; seven-day paid purchase outcome. No automatic winner."}


async def experiments(org_id):
    rows = await growth.service().db.commerce_experiments.find({"org_id": org_id}).sort("created_at", -1).limit(50).to_list(50)
    return [await experiment_report(org_id, row) for row in rows]


async def create_experiment(org_id, name, percent, request_id):
    s = growth.service()
    connection = await growth.connected(org_id)
    existing = await s.db.commerce_experiments.find_one({"_id": identifier(org_id, request_id)})
    if existing:
        return await experiment_report(org_id, existing)
    row = {"_id": identifier(org_id, request_id), "experiment_id": identifier(org_id, request_id), "org_id": org_id,
           "shop": connection["shop"], "name": name, "holdout_percent": percent, "status": "running", "created_at": s.utcnow()}
    try:
        await s.db.commerce_experiments.insert_one(row)
    except DuplicateKeyError:
        existing = await s.db.commerce_experiments.find_one({"_id": row["_id"]})
        if existing:
            return await experiment_report(org_id, existing)
        raise HTTPException(409, "An experiment is already running") from None
    return await experiment_report(org_id, row)


async def tracked_link(org_id, shop, kind, reference, destination, customer_id=None):
    s = growth.service()
    parsed = urlsplit(destination)
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname != shop:
        # Existing Shopify custom checkout domains remain usable, without tracking when unverified.
        return destination
    key = identifier(org_id, kind, reference)
    token = secrets.token_urlsafe(24)
    await s.db.commerce_tracked_links.update_one({"_id": key}, {"$setOnInsert": {"org_id": org_id, "shop": shop, "kind": kind, "reference": reference,
              "token": token, "destination": destination, "customer_id": customer_id, "created_at": s.utcnow(), "expires_at": s.utcnow() + timedelta(days=30)}}, upsert=True)
    row = await s.db.commerce_tracked_links.find_one({"_id": key})
    return f"{s.SHOPIFY_PUBLIC_BASE_URL}/api/commerce/shopify/link/{row['token']}"


async def resolve_link(token, user_agent, preview=False):
    s = growth.service()
    row = await s.db.commerce_tracked_links.find_one({"token": token, "expires_at": {"$gt": s.utcnow()}})
    if not row or not await s.db.shopify_connections.find_one({"org_id": row["org_id"], "shop": row["shop"], "status": "connected"}):
        raise HTTPException(404, "This recovery link has expired")
    bot = preview or any(word in user_agent.lower() for word in ("bot", "crawler", "preview", "facebookexternalhit", "whatsapp"))
    await s.db.commerce_tracked_links.update_one({"_id": row["_id"]}, {"$inc": {"preview_requests" if bot else "redirect_requests": 1}})
    parsed = urlsplit(row["destination"])
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "heidel_touch"]
    query.append(("heidel_touch", token))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


async def install_pixel(org_id):
    s = growth.service()
    connection = await growth.connected(org_id)
    scopes = str(connection.get("scope") or connection.get("scopes") or "")
    if not all(scope in scopes for scope in ("write_pixels", "read_customer_events")):
        raise HTTPException(409, "Reconnect Shopify with write_pixels and read_customer_events after deploying the pixel extension")
    token = secrets.token_urlsafe(32)
    existing = await s.db.commerce_pixel_installations.find_one({"_id": org_id})
    if existing:
        token = existing["token"]
    config = {"collectorUrl": f"{s.SHOPIFY_PUBLIC_BASE_URL}/api/commerce/shopify/pixel", "installationToken": token}
    await s.db.commerce_pixel_installations.update_one({"_id": org_id}, {"$setOnInsert": {"org_id": org_id, "shop": connection["shop"], "token": token, "status": "pending"}}, upsert=True)
    saved = await s.db.commerce_pixel_installations.find_one({"_id": org_id})
    config["installationToken"] = saved["token"]
    current = (await s._graphql(connection, "query OwnerPixel { webPixel { id } }", {})).get("webPixel")
    if current:
        data = await s._graphql(connection, 'mutation OwnerPixelUpdate($id: ID!, $pixel: WebPixelInput!) { webPixelUpdate(id: $id, webPixel: $pixel) { webPixel { id } userErrors { message } } }', {"id": current["id"], "pixel": {"settings": json.dumps(config)}})
        result = data.get("webPixelUpdate") or {}
    else:
        data = await s._graphql(connection, 'mutation OwnerPixel($pixel: WebPixelInput!) { webPixelCreate(webPixel: $pixel) { webPixel { id } userErrors { message } } }', {"pixel": {"settings": json.dumps(config)}})
        result = data.get("webPixelCreate") or {}
    if result.get("userErrors") or not (result.get("webPixel") or {}).get("id"):
        raise HTTPException(409, str((result.get("userErrors") or [{"message": "Pixel installation failed"}])[0].get("message")))
    await s.db.commerce_pixel_installations.update_one({"_id": org_id}, {"$set": {"status": "installed", "pixel_id": result["webPixel"]["id"], "installed_at": s.utcnow()}})
    return {"status": "installed"}


async def capture_pixel(payload):
    s = growth.service()
    install = await s.db.commerce_pixel_installations.find_one({"token": payload["installation_token"]})
    if not install or not await s.db.shopify_connections.find_one({"org_id": install["org_id"], "shop": install["shop"], "status": "connected"}):
        raise HTTPException(403, "Pixel installation unavailable")
    # Browser observations are untrusted and never identify buyers, send messages or create paid orders.
    touch = await s.db.commerce_tracked_links.find_one({"org_id": install["org_id"], "shop": install["shop"], "token": payload.get("touch_token"), "expires_at": {"$gt": s.utcnow()}}) if payload.get("touch_token") else None
    await s.db.commerce_pixel_events.update_one({"_id": identifier(install["org_id"], payload["event_id"])}, {"$setOnInsert": {
        "org_id": install["org_id"], "shop": install["shop"], "event_name": payload["event_name"], "received_at": s.utcnow(),
        "touch_reference": touch.get("reference") if touch else None, "touch_kind": touch.get("kind") if touch else None,
        "variant_id": payload.get("variant_id"), "client_key": identifier(install["org_id"], payload["client_id"])}}, upsert=True)
    return {"status": "recorded"}


def csv_report(report, view):
    rows = report.get(view, [])
    if isinstance(rows, dict):
        rows = [{"metric": key, "value": value} for key, value in rows.items()]
    if not isinstance(rows, list):
        rows = [{"value": rows}]
    keys = list(dict.fromkeys(key for row in rows if isinstance(row, dict) for key in row))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        safe = {}
        for key in keys:
            value = row.get(key)
            value = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value if value is not None else "")
            # Prevent spreadsheet formulas, including leading whitespace and control characters.
            safe[key] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
        writer.writerow(safe)
    return output.getvalue()


async def period_report(org_id, start, end, zone="UTC"):
    s = growth.service()
    now = s.utcnow()
    # ponytail: scan the bounded date range in memory; use Mongo aggregations when store volume makes this slow.
    config = await growth.settings(org_id)
    window = timedelta(days=config["attribution_days"])
    all_touches = await touches(org_id, start-window, min(end, now)+window)
    cohort = [row for row in all_touches if start <= growth.moment(row["sent_at"]) < end and growth.moment(row["sent_at"]) <= now]
    orders = await s.db.shopify_orders.find({"org_id": org_id, "provider_created_at": {"$gte": start, "$lt": min(end+window, now+timedelta(milliseconds=1))}}).to_list(None)
    period_orders = [order for order in orders if growth.moment(order.get("provider_created_at") or order.get("created_at")) < end]
    attribution = growth.attribution(period_orders, [row for row in all_touches if growth.moment(row["sent_at"]) < end], now, config["attribution_days"])
    cohort_attribution = growth.attribution(orders, all_touches, now, config["attribution_days"])
    cohort_ids = {row["touch_id"] for row in cohort}
    conversions = {row["touch_id"] for row in cohort_attribution["orders"] if row["touch_id"] in cohort_ids}
    mature = [row for row in cohort if growth.moment(row["sent_at"]) + window <= now]
    delivered = [row for row in cohort if row["status"] in {"delivered", "read"}]
    cost = totals({"amount": row["message_rate"]["amount"], "currency": row["message_rate"]["currency"]} for row in delivered if row.get("message_rate"))
    carts = await s.db.shopify_checkouts.find({"org_id": org_id, "provider_created_at": {"$gte": start, "$lt": end}}).to_list(None)
    jobs = await s.db.commerce_retargeting_jobs.find({"org_id": org_id, "original_accepted_at": {"$gte": start, "$lt": end}}).to_list(None)
    events = await s.db.commerce_pixel_events.find({"org_id": org_id, "received_at": {"$gte": start, "$lt": end}}).to_list(None)
    blockers = Counter(cart.get("failure_category") or cart.get("reminder_status") for cart in carts if cart.get("reminder_status") in {"permission_missing", "phone_missing", "configuration_failure", "failed", "opted_out", "superseded", "delivery_unknown"})
    actions = [{"issue": key, "count": count, "action": "Review recovery records and permissions" if "permission" in key or "opt" in key else "Review delivery errors and Shopify settings"} for key, count in blockers.most_common()]
    sources = []
    for kind in ("recovery", "retargeting", "campaign"):
        selected = [row for row in cohort if row["kind"] == kind]
        matched = [row for row in attribution["orders"] if row["kind"] == kind]
        sources.append({"source": kind, "accepted": len(selected), "delivered": sum(row["status"] in {"delivered", "read"} for row in selected), "read": sum(row["status"] == "read" for row in selected), "matched_orders": len(matched), "net_payments": totals(row["net_payment"] for row in matched)})
    recovery = [{"stage": "Observed carts", "count": len(carts)}, {"stage": "Permission available", "count": 0}]
    permission_count = 0
    for cart in carts:
        permission_count += bool(await s.recovery_permission(cart))
    recovery[1]["count"] = permission_count
    recovery.extend([{"stage": "Original accepted", "count": sum(row["kind"] == "recovery" for row in cohort)}, {"stage": "Confirmed delivered", "count": sum(row["kind"] == "recovery" for row in delivered)},
                     {"stage": "Confirmed read", "count": sum(row["kind"] == "recovery" and row["status"] == "read" for row in cohort)}, {"stage": "Follow-up accepted", "count": sum(row["kind"] == "retargeting" for row in cohort)},
                     {"stage": "Matched cart purchases", "count": sum(row["kind"] in {"recovery", "retargeting"} for row in attribution["orders"])}])
    branch_performance = []
    for branch in ("read", "unconfirmed"):
        selected = [row for row in cohort if row.get("branch") == branch]
        matched = [row for row in cohort_attribution["orders"] if row["touch_id"] in {touch["touch_id"] for touch in selected}]
        branch_performance.append({"branch": branch, "accepted": len(selected), "matched_orders": len(matched), "net_payments": totals(row["net_payment"] for row in matched),
                                   "average_minutes_to_purchase": round(sum((growth.moment(row["created_at"])-growth.moment(row["sent_at"])).total_seconds()/60 for row in matched)/len(matched), 1) if matched else None})
    product_rows = {}
    attributed_ids = {row["order_id"] for row in attribution["orders"]}
    missing_products = 0
    for kind, records in (("abandoned", carts), ("recovered", [row for row in period_orders if row["provider_id"] in attributed_ids])):
        for record in records:
            for product in record.get("products") or []:
                key = product.get("variant_id") or product.get("product_id")
                if not key:
                    missing_products += 1
                    continue
                row = product_rows.setdefault(key, {"product_id": product.get("product_id"), "variant_id": product.get("variant_id"), "name": product.get("name"), "sku": product.get("sku"), "abandoned_quantity": 0, "recovered_quantity": 0, "abandoned_values": [], "recovered_values": [], "refunded_quantity": 0, "refunded_values": []})
                row[f"{kind}_quantity"] += int(product.get("quantity") or 0)
                row[f"{kind}_values"].append(product.get("total"))
                if kind == "recovered":
                    row["refunded_quantity"] += int(product.get("refunded_quantity") or 0)
                    row["refunded_values"].append(product.get("refunded_total"))
    for row in product_rows.values():
        for key in ("abandoned_values", "recovered_values", "refunded_values"):
            row[key] = totals(row[key])
    all_orders = await s.db.shopify_orders.find({"org_id": org_id, "record_status": {"$ne": "deleted"}, "cancelled_at": None}).to_list(None)
    customer_rows = defaultdict(list)
    for order in all_orders:
        if order.get("customer_id"):
            customer_rows[order["customer_id"]].append(order)
    customers = []
    retention = []
    first_count = repeat_count = returned_count = 0
    for customer_id, history in customer_rows.items():
        history.sort(key=lambda row: growth.moment(row.get("provider_created_at") or row.get("created_at")) or now)
        selected = [row for row in history if start <= growth.moment(row.get("provider_created_at") or row.get("created_at")) < end]
        if not selected:
            continue
        known = max((int(row.get("customer_order_count") or 0) for row in selected), default=0)
        first_count += known == 1
        repeat_count += known > 1
        selected_start = growth.moment(selected[0].get("provider_created_at") or selected[0].get("created_at"))
        prior = [row for row in history if growth.moment(row.get("provider_created_at") or row.get("created_at")) < selected_start]
        returned = bool(prior and selected_start-growth.moment(prior[-1].get("provider_created_at") or prior[-1].get("created_at")) >= timedelta(days=30))
        returned_count += returned
        customers.append({"customer_id": customer_id, "observed_orders": len(history), "shopify_purchase_count": known or None, "lapsed_returned": returned, "observed_net_payments": totals(net_payment(row) for row in history),
                          "orders": [{"order_id": row["provider_id"], "number": row.get("order_number"), "created_at": row.get("provider_created_at"), "net_payment": net_payment(row)} for row in history]})
    for days in (30, 60):
        # Only authoritative first orders can start a retention cohort; partial imports cannot invent acquisition dates.
        firsts = [order for order in all_orders if order.get("customer_id") and order.get("customer_order_count_at_purchase") == 1 and start <= growth.moment(order["provider_created_at"]) < end]
        mature_firsts = [row for row in firsts if growth.moment(row["provider_created_at"])+timedelta(days=days) <= now]
        retained = sum(any(growth.moment(row["provider_created_at"]) < growth.moment(later["provider_created_at"]) <= growth.moment(row["provider_created_at"])+timedelta(days=days) for later in customer_rows[row["customer_id"]]) for row in mature_firsts)
        retention.append({"days": days, "mature_customers": len(mature_firsts), "repeat_customers": retained, "pending": len(firsts)-len(mature_firsts), "rate": round(100*retained/len(mature_firsts), 2) if mature_firsts else None})
    links = await s.db.commerce_tracked_links.find({"org_id": org_id, "created_at": {"$gte": start, "$lt": end}}).to_list(None)
    suppressions = await s.db.whatsapp_suppressions.find({"org_id": org_id, "updated_at": {"$gte": start, "$lt": end}}).to_list(None)
    campaign_rows = []
    active_campaign_ids = {row["campaign_id"] for row in cohort + attribution["orders"] if row.get("campaign_id")}
    campaigns = await s.db.commerce_campaigns.find({"org_id": org_id, "$or": [{"created_at": {"$gte": start, "$lt": end}}, {"campaign_id": {"$in": list(active_campaign_ids)}}]}).to_list(None)
    for campaign in campaigns:
        selected = [row for row in cohort if row.get("campaign_id") == campaign["campaign_id"]]
        matched = [row for row in attribution["orders"] if row.get("campaign_id") == campaign["campaign_id"]]
        campaign_orders = [row for row in period_orders if row["provider_id"] in {entry["order_id"] for entry in matched}]
        recipient_keys = set((campaign.get("recipient_customers") or {}).keys())
        campaign_links = [row for row in links if row.get("kind") == "campaign" and str(row.get("reference", "")).startswith(campaign["campaign_id"]+":")]
        campaign_rows.append({"campaign_id": campaign["campaign_id"], "name": campaign.get("name", "Campaign"), "status": campaign["status"], "selected": campaign.get("total", 0), "accepted": len(selected), "delivered": sum(row["status"] in {"delivered", "read"} for row in selected), "read": sum(row["status"] == "read" for row in selected), "matched_orders": len(matched), "net_payments": totals(row["net_payment"] for row in matched),
                              "cost_estimates": totals(row.get("message_rate") for row in selected if row["status"] in {"delivered", "read"}),
                              "refunded_values": totals(product.get("refunded_total") for order in campaign_orders for product in order.get("products") or []),
                              "opted_out_recipients": sum(growth.phone_key(row.get("phone")) in recipient_keys for row in suppressions),
                              "redirect_requests": sum(row.get("redirect_requests", 0) for row in campaign_links), "preview_requests": sum(row.get("preview_requests", 0) for row in campaign_links),
                              "click_tracking_available": campaign.get("tracking_button_index") is not None,
                              "confirmed_visits": len({row["client_key"] for row in events if row.get("touch_kind") == "campaign" and str(row.get("touch_reference", "")).startswith(campaign["campaign_id"]+":") and row["event_name"] == "page_viewed"})})
    trends = []
    local_day = start.astimezone(ZoneInfo(zone)).date()
    last_day = min(end, now).astimezone(ZoneInfo(zone)).date()
    while local_day <= last_day:
        day = datetime.combine(local_day, time.min, ZoneInfo(zone)).astimezone(timezone.utc)
        next_day = datetime.combine(local_day+timedelta(days=1), time.min, ZoneInfo(zone)).astimezone(timezone.utc)
        if day < min(end, now):
            daily_orders = [row for row in attribution["orders"] if day <= growth.moment(row["created_at"]) < next_day]
            trends.append({"date": local_day.isoformat(), "accepted": sum(day <= growth.moment(row["sent_at"]) < next_day for row in cohort), "matched_orders": len(daily_orders), "net_payments": totals(row["net_payment"] for row in daily_orders)})
        local_day += timedelta(days=1)
    summary = {"accepted": len(cohort), "delivered": len(delivered), "read": sum(row["status"] == "read" for row in cohort), "matched_orders": attribution["matched_orders"], "paid_orders": sum(row["net_payment"] is not None and growth.money(row["net_payment"].get("amount")) > 0 for row in attribution["orders"]), "net_payments": attribution["sales"], "cost_estimates": cost,
               "cost_unknown_deliveries": sum(not row.get("message_rate") for row in delivered), "conversion_denominator": len(cohort), "converted_contacts": len(conversions), "conversion_rate": round(100*len(conversions)/len(cohort), 2) if cohort else 0, "mature_contacts": len(mature), "pending_contacts": len(cohort)-len(mature), "value_unavailable": attribution["value_unavailable"],
               "confirmed_visitors": len({row["client_key"] for row in events if row.get("touch_reference") and row["event_name"] == "page_viewed"})}
    return {"summary": summary, "sources": sources, "recovery": recovery, "branches": branch_performance, "blockers": [{"reason": key, "count": value} for key, value in blockers.items()], "actions": actions, "orders": attribution["orders"], "campaigns": campaign_rows, "customers": customers, "customer_summary": {"first_time": first_count, "repeat": repeat_count, "lapsed_returned": returned_count, "purchase_count_unavailable": sum(row["shopify_purchase_count"] is None for row in customers)}, "retention": retention, "products": list(product_rows.values()), "trends": trends,
            "coverage": {"product_identity_missing": missing_products, "carts_with_missing_values": sum(not cart.get("total") for cart in carts), "line_items_incomplete": sum(row.get("line_items_incomplete", False) for row in carts+period_orders), "refund_lines_incomplete": sum(row.get("refund_lines_incomplete", False) for row in period_orders), "observed_history_start": min((growth.moment(row.get("provider_created_at")) for row in all_orders if growth.moment(row.get("provider_created_at"))), default=None), "browser_events": len(events), "retargeting_jobs": len(jobs)}}


async def report(org_id, start=None, end=None, days=30, compare=True):
    first, stop, zone, fallback = await date_range(org_id, start, end, days)
    current = await period_report(org_id, first, stop, zone)
    local_first = first.astimezone(ZoneInfo(zone))
    calendar_days = (stop.astimezone(ZoneInfo(zone)).date()-local_first.date()).days
    prior_start = datetime.combine(local_first.date()-timedelta(days=calendar_days), time.min, ZoneInfo(zone)).astimezone(timezone.utc)
    prior = await period_report(org_id, prior_start, first, zone) if compare else None
    connection = await growth.connected(org_id)
    current.update({"period": {"start": first, "end": stop, "timezone": zone, "timezone_fallback": fallback}, "comparison": prior["summary"] if prior else None, "attribution_days": (await growth.settings(org_id))["attribution_days"], "generated_at": growth.service().utcnow(), "last_sync_at": connection.get("last_sync_at"),
                    "definitions": ["Revenue: attributed net payments after available refunds; unpaid orders excluded. Currencies are separate.", "Attribution: one last eligible contact per order; this measures observed attribution, not uplift.", "Contact conversion: contacts sent in this period with an attributed purchase in their window; immature contacts are labelled pending.", "Customers: observed history only; first/repeat counts use authoritative Shopify customer purchase counts.", "Product values: recorded discounted line values, where available; recovered value is order value, not additional uplift.", "Campaign opt-outs: selected recipients opting out during this period; not proof the campaign caused the opt-out.", "Browser visits: consented pixel observations; redirects and preview requests are not confirmed shoppers.", "Costs: rate saved at send time; unknown historical rates are excluded. Provider invoice totals may differ."]})
    return current


async def process_weekly_reports():
    s = growth.service()
    for connection in await s.db.shopify_connections.find({"status": "connected"}).to_list(None):
        org_id = connection["org_id"]
        zone, _ = await shop_timezone(org_id)
        local = s.utcnow().astimezone(zone)
        monday = local.date()-timedelta(days=local.weekday())
        due = datetime.combine(monday, time(9), zone)
        if local < due:
            monday -= timedelta(days=7)
        key = identifier(org_id, monday)
        if await s.db.commerce_weekly_reports.find_one({"_id": key}):
            continue
        try:
            snapshot = await report(org_id, monday-timedelta(days=7), monday-timedelta(days=1))
            await s.db.commerce_weekly_reports.update_one({"_id": key}, {"$setOnInsert": {"org_id": org_id, "week_ending": (monday-timedelta(days=1)).isoformat(), "created_at": s.utcnow(), "snapshot": snapshot}}, upsert=True)
        except Exception as exc:
            s.logger.warning("Shopify weekly report deferred: {}", type(exc).__name__)
