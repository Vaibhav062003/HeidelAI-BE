"""Customer enquiries, evidence, and a small durable, explicit restock outbox.

Reuse purchase-intent extraction and the WhatsApp template sender. A provider
timeout is deliberately UNKNOWN, never automatically retried (duplicate risk).
"""
import asyncio
import re
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.product_request_policy import (
    RESTOCK_TEMPLATE, RESTOCK_BODY, RESTOCK_FOOTER, request_key, grounded_request,
    eligibility_reason, approved_restock_template,
)
from services.work_team_service import require_conversation_access


def now():
    return datetime.now(timezone.utc)


async def ensure_product_request_indexes():
    db = get_async_mongo_db()
    await db.product_requests.create_index([("org_id", 1), ("status", 1), ("last_asked_at", -1)])
    await db.product_requests.create_index([("org_id", 1), ("product_id", 1), ("business_phone_id", 1)])
    await db.product_requests.create_index("product_key", unique=True, sparse=True)
    await db.restock_outbox.create_index([("status", 1), ("created_at", 1)])


async def capture_product_requests(job, raw_requests, messages):
    if not raw_requests:
        return
    db = get_async_mongo_db()
    org = job["org_id"]
    conversation = await db[f"conversations_{org}"].find_one({"conversation_id": job["conversation_id"]})
    if (not conversation or conversation.get("platform") != "whatsapp"
            or conversation.get("is_dealer") or conversation.get("persona") in {"dealer", "dealers"}):
        return
    # Capture must not race the independent restock worker's index setup.
    await db.product_requests.create_index("product_key", unique=True, sparse=True)
    for raw in raw_requests[:20]:
        item = grounded_request(raw, messages)
        if not item:
            continue
        key = request_key(org, job["conversation_id"], item["product_name"])
        stamp = item["source_at"]
        if not isinstance(stamp, datetime):
            continue
        # Insert first, then compare source times: out-of-order workers cannot revive stale interest.
        # Adopt an existing exact-name row without changing its public ID or reviewed state.
        try:
            await db.product_requests.update_one(
                {"_id": key, "org_id": org, "product_key": {"$exists": False}, "archived_at": {"$exists": False}},
                {"$set": {"product_key": key}})
        except DuplicateKeyError:
            pass  # A canonical row already exists, including during concurrent capture.
        base = {"_id": str(uuid4()), "product_key": key, "org_id": org, "conversation_id": job["conversation_id"],
                "customer_name": conversation.get("customer_name") or job.get("customer_name"),
                "customer_phone": re.sub(r"\D", "", str(conversation.get("customer_id") or job.get("customer_phone") or "")),
                "business_phone_id": str(conversation.get("business_phone_id") or ""),
                "business_phone": conversation.get("business_phone"),
                "status": "requested", "created_at": now(), "product_name": item["product_name"]}
        try:
            await db.product_requests.update_one({"product_key": key}, {"$setOnInsert": base}, upsert=True)
        except DuplicateKeyError:
            pass
        update = {"evidence": item["evidence"], "source_message_id": item["source_message_id"],
                  "product_reference_message_id": item["product_reference_message_id"],
                  "product_mention": item["product_mention"], "specificity": item["specificity"],
                  "confidence": item["confidence"], "last_asked_at": stamp,
                  "notification_requested": item["notification_requested"]}
        if item["withdrawn"]:
            update["status"] = "dismissed"
        await db.product_requests.update_one(
            {"product_key": key, "archived_at": {"$exists": False}, "$or": [{"last_asked_at": {"$lt": stamp}}, {"last_asked_at": {"$exists": False}}]},
            {"$set": update})


async def catalogue_org(org):
    record = await get_async_mongo_db().organizations.find_one({"org_id": org}, {"sales_catalogue_org_id": 1}) or {}
    selected = str(record.get("sales_catalogue_org_id") or "")
    return selected if re.fullmatch(r"[A-Za-z0-9_-]+", selected) else org


async def product_for(row):
    if not row.get("product_id") or row.get("catalogue_org_id") != await catalogue_org(row["org_id"]):
        return None
    value = row["product_id"]
    ids = [value, ObjectId(value)] if ObjectId.is_valid(value) else [value]
    db = get_async_mongo_db()
    product = await db[f"products_{row['catalogue_org_id']}"].find_one({"_id": {"$in": ids}})
    if not product:
        return None
    sources = product.get("source_sheet_ids") or []
    sources = [sources] if isinstance(sources, str) else list(sources)
    if product.get("source_sheet_id"):
        sources.append(product["source_sheet_id"])
    if sources and not await db.product_sheet_connections.find_one({
        "org_id": row["catalogue_org_id"], "sheet_id": {"$in": list(set(sources))},
        "status": {"$in": ["active", "syncing"]}, "disconnect_requested_at": {"$exists": False},
    }, {"_id": 1}):
        return None
    from services.stock_service import effective_stock_product
    return await effective_stock_product(row["catalogue_org_id"], product, database=db)


async def connection_for(org, phone):
    connection = await get_async_mongo_db().whatsapp_connections.find_one({
        "org_id": org, "phone_id": phone, "is_active": {"$ne": False},
    })
    if not connection or not connection.get("access_token"):
        raise HTTPException(409, "The original WhatsApp line is disconnected")
    return connection


async def accessible_request(user, request_id):
    row = await get_async_mongo_db().product_requests.find_one({"org_id": user.org_id, "_id": request_id, "archived_at": {"$exists": False}})
    if not row:
        raise HTTPException(404, "Product request not found")
    conversation = await require_conversation_access(user.org_id, row["conversation_id"], user.user_id, user.role)
    customer = re.sub(r"\D", "", str(conversation.get("customer_id") or ""))
    if (conversation.get("platform") != "whatsapp" or not customer
            or customer != str(row.get("customer_phone") or "")
            or not row.get("business_phone_id")
            or str(conversation.get("business_phone_id") or "") != str(row["business_phone_id"])):
        raise HTTPException(409, "The conversation customer or WhatsApp line changed; review its product request before outreach")
    return row


async def review_request(user, request_id, payload):
    row = await accessible_request(user, request_id)
    cancelling = payload.status in {"dismissed", "fulfilled"} or payload.consent_note is not None and not payload.consent_note.strip()
    if row.get("notification_status") in {"queued", "checking", "sending"} and not cancelling:
        raise HTTPException(409, "Dismiss the request or clear notification permission to cancel pending outreach first")
    if cancelling and payload.product_id and payload.product_id != row.get("product_id"):
        raise HTTPException(409, "Cancel outreach before changing the matched product")
    update = {"status": payload.status, "updated_at": now()}
    unset = {}
    if payload.product_id:
        selected = {**row, "product_id": payload.product_id, "catalogue_org_id": await catalogue_org(user.org_id)}
        product = await product_for(selected)
        if not product:
            raise HTTPException(404, "Product is not in this organization's selected catalogue")
        update.update(product_id=payload.product_id, catalogue_org_id=selected["catalogue_org_id"],
                      catalogue_name=product.get("product_name") or product.get("name") or payload.product_id,
                      reviewed_by=user.user_id, reviewed_at=now())
    elif "product_id" in getattr(payload, "model_fields_set", set()):
        unset = {key: "" for key in ("product_id", "catalogue_org_id", "catalogue_name", "reviewed_by", "reviewed_at")}
    # Permission is a human-reviewed evidence note, never inferred from a generic enquiry.
    if payload.consent_note is not None:
        note = payload.consent_note.strip()
        if note and len(note) < 10:
            raise HTTPException(422, "Record where and when this customer requested a WhatsApp restock notification")
        update.update(consent_note=note, consent_by=user.user_id if note else None, consent_at=now())
    db = get_async_mongo_db()
    if cancelling:
        # Cancel before editing the row: dispatch atomically crosses checking -> sending.
        # Once sending won that race we cannot recall Meta, but still record withdrawal.
        await db.restock_outbox.update_many(
            {"org_id": user.org_id, "request_id": row["_id"], "status": {"$in": ["preparing", "queued", "checking"]}},
            {"$set": {"status": "cancelled", "error": "Request dismissed or notification permission withdrawn", "completed_at": now()}})
        await db.product_requests.update_one(
            {"_id": row["_id"], "org_id": user.org_id, "notification_status": {"$in": ["queued", "checking"]}},
            {"$set": {"notification_status": "cancelled"}})
    match = {"_id": row["_id"], "org_id": user.org_id}
    if not cancelling:
        match["notification_status"] = {"$nin": ["queued", "checking", "sending"]}
    result = await db.product_requests.update_one(match, {"$set": update, **({"$unset": unset} if unset else {})})
    if not result.matched_count:
        raise HTTPException(409, "Outreach was queued during your edit; refresh the request")
    sending = await db.restock_outbox.find_one({"org_id": user.org_id, "request_id": row["_id"], "status": "sending"}) if cancelling else None
    return {"ok": True, "outreach_already_sending": bool(sending)}


async def check_candidate(user, request_id):
    from services.wa_service import get_broadcast_suppressed_phone_keys
    row = await accessible_request(user, request_id)
    product = await product_for(row)
    reason = eligibility_reason(row, product)
    if not reason:
        suppressed = await asyncio.to_thread(get_broadcast_suppressed_phone_keys, user.org_id, [row["customer_phone"]])
        if suppressed:
            reason = "Customer opted out of WhatsApp broadcasts"
    return row, product, reason


async def restock_preview(user, ids):
    result = []
    seen = set()
    for request_id in dict.fromkeys(ids):
        row, product, reason = await check_candidate(user, request_id)
        key = (row.get("business_phone_id"), row.get("customer_phone"), row.get("product_id"))
        if key in seen:
            reason = "Duplicate recipient for this product and WhatsApp line"
        seen.add(key)
        result.append({"id": request_id, "customer_name": row.get("customer_name"),
                       "customer_phone": row.get("customer_phone"), "business_phone": row.get("business_phone"),
                       "product": (product or {}).get("product_name") or row.get("catalogue_name"),
                       "eligible": not reason, "reason": reason})
    return result


async def prepare_template(user, phone):
    from services.wa_service import create_templates, get_templates
    connection = await connection_for(user.org_id, phone)
    templates = await get_templates(user.org_id, connection["wa_id"], connection["access_token"], name=RESTOCK_TEMPLATE)
    existing = next((t for t in templates["templates"] if t.get("name") == RESTOCK_TEMPLATE), None)
    if existing:
        return {"status": existing.get("status"), "approved": approved_restock_template([existing])}
    result = await create_templates(user.org_id, connection["wa_id"], connection["access_token"],
        RESTOCK_TEMPLATE, "en_US", "MARKETING", [
            {"type": "BODY", "text": RESTOCK_BODY, "example": {"body_text": [["Samsung Galaxy S24 256GB"]]}},
            {"type": "FOOTER", "text": RESTOCK_FOOTER}])
    return {"status": "PENDING", "approved": False, "result": result}


async def queue_restock(user, ids):
    from services.entitlement_service import require_feature
    from services.wa_service import get_templates
    await asyncio.to_thread(require_feature, user.org_id, "BROADCASTING")
    preview = await restock_preview(user, ids)
    if any(not item["eligible"] for item in preview):
        raise HTTPException(409, "Audience changed or includes ineligible customers; refresh the preview")
    db = get_async_mongo_db()
    rows = [await accessible_request(user, item["id"]) for item in preview]
    if len({(r.get("product_id"), r.get("business_phone_id")) for r in rows}) != 1:
        raise HTTPException(422, "Choose one exact catalogue product and one WhatsApp line per broadcast")
    connection = await connection_for(user.org_id, rows[0]["business_phone_id"])
    templates = await get_templates(user.org_id, connection["wa_id"], connection["access_token"], name=RESTOCK_TEMPLATE)
    if not approved_restock_template(templates["templates"]):
        raise HTTPException(409, "Create the restock template and wait for Meta approval on this WhatsApp account")
    queued = 0
    for row in rows:
        # Stable ID guards double-clicks/retries. Each customer/product/line gets one notification.
        key = request_key(user.org_id, row["customer_phone"] + ":" + row["business_phone_id"], row["product_id"])
        try:
            await db.restock_outbox.insert_one({"_id": key, "org_id": user.org_id, "request_id": row["_id"],
                "snapshot": outreach_snapshot(row), "actor_id": user.user_id,
                "status": "preparing", "send_attempted": False, "created_at": now(), "started_at": now()})
        except DuplicateKeyError:
            continue
        queued += int(await publish_restock_job(db, row, key))
    return {"queued": queued, "already_queued": len(rows) - queued}


def outreach_snapshot(row):
    return {field: row.get(field) for field in (
        "product_id", "catalogue_org_id", "consent_note", "consent_by", "consent_at",
        "customer_phone", "business_phone_id", "conversation_id",
    )}


async def publish_restock_job(db, row, key):
    # The dispatcher cannot consume 'preparing'. Publish only after the guarded
    # request update, so a fast worker can never be overwritten back to queued.
    claimed = await db.product_requests.update_one(
        {"_id": row["_id"], "org_id": row["org_id"], "status": "requested",
         **outreach_snapshot(row), "notification_status": row.get("notification_status")},
        {"$set": {"notification_status": "queued", "notification_id": key}})
    if not claimed.matched_count:
        await db.restock_outbox.update_one({"_id": key, "status": "preparing"},
            {"$set": {"status": "blocked", "error": "Request changed before queueing", "completed_at": now()}})
        return False
    published = await db.restock_outbox.update_one({"_id": key, "status": "preparing"},
        {"$set": {"status": "queued", "created_at": now()}})
    return bool(published.matched_count)


async def retry_restock(user, request_id):
    """Explicit admin retry of a proven pre-send block/cancellation only."""
    from services.entitlement_service import require_feature
    from services.wa_service import get_templates, get_broadcast_suppressed_phone_keys
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(403, "Only an administrator can retry a broadcast")
    await asyncio.to_thread(require_feature, user.org_id, "BROADCASTING")
    row = await accessible_request(user, request_id)
    reason = eligibility_reason({**row, "notification_status": None}, await product_for(row))
    if reason:
        raise HTTPException(409, reason)
    if await asyncio.to_thread(get_broadcast_suppressed_phone_keys, user.org_id, [row["customer_phone"]]):
        raise HTTPException(409, "Customer opted out")
    connection = await connection_for(user.org_id, row["business_phone_id"])
    templates = await get_templates(user.org_id, connection["wa_id"], connection["access_token"], name=RESTOCK_TEMPLATE)
    if not approved_restock_template(templates["templates"]):
        raise HTTPException(409, "Restock template is not approved")
    db = get_async_mongo_db()
    job = await db.restock_outbox.find_one_and_update(
        {"org_id": user.org_id, "request_id": request_id,
         "status": {"$in": ["blocked", "cancelled"]}, "send_attempted": False,
         "quota_release_pending": {"$ne": True}},
        {"$set": {"status": "preparing", "started_at": now(), "snapshot": outreach_snapshot(row), "actor_id": user.user_id},
         "$unset": {"completed_at": "", "error": ""}}, return_document=ReturnDocument.AFTER)
    if not job:
        raise HTTPException(409, "Only a notification proven not to have been sent can be retried")
    expected_key = request_key(user.org_id, row["customer_phone"] + ":" + row["business_phone_id"], row["product_id"])
    if job["_id"] != expected_key:
        # A reviewed product correction must use its own recipient/product key;
        # otherwise a second enquiry could bypass per-product deduplication.
        await db.restock_outbox.update_one({"_id": job["_id"], "status": "preparing"},
            {"$set": {"status": "cancelled", "error": "Product match changed; superseded by reviewed retry", "completed_at": now()}})
        try:
            await db.restock_outbox.insert_one({"_id": expected_key, "org_id": user.org_id, "request_id": request_id,
                "snapshot": outreach_snapshot(row), "actor_id": user.user_id, "status": "preparing",
                "send_attempted": False, "created_at": now(), "started_at": now()})
        except DuplicateKeyError:
            raise HTTPException(409, "A notification already exists for this customer, product and WhatsApp line")
        job["_id"] = expected_key
    if not await publish_restock_job(db, row, job["_id"]):
        raise HTTPException(409, "Request changed during retry; refresh the request")
    return {"queued": 1, "already_queued": 0}


async def current_outreach_actor(job):
    from types import SimpleNamespace
    from api.transfer_routes import get_org_memberships, membership_public_data, _clerk_cache
    # Sending must recheck revocation/role changes, not reuse the five-minute UI cache.
    _clerk_cache.pop(job["org_id"], None)
    memberships = await get_org_memberships(job["org_id"])
    for membership in memberships:
        public = membership_public_data(membership)
        if public.get("user_id") == job["actor_id"] and not public.get("banned"):
            role = membership.get("role")
            if role in {"admin", "org:admin"}:
                return SimpleNamespace(org_id=job["org_id"], user_id=job["actor_id"], role=role)
    raise HTTPException(403, "The authorizing administrator is no longer an active administrator")


async def process_restock_outbox():
    """One recipient per tick; reuse the existing 15-second intent-worker loop."""
    from services.entitlement_service import consume_quota_async, release_quota_async, require_feature
    from services.wa_service import get_templates, send_whatsapp_template_message
    db = get_async_mongo_db()
    # Interrupted sends may have reached Meta. Never retry them blindly.
    stale = await db.restock_outbox.find({"status": {"$in": ["preparing", "checking", "sending"]}, "started_at": {"$lt": now() - timedelta(minutes=5)}}).limit(100).to_list(100)
    for old in stale:
        recovered_status = "unknown" if old["status"] == "sending" else "blocked"
        recovered = await db.restock_outbox.update_one({"_id": old["_id"], "status": old["status"], "started_at": old["started_at"]},
            {"$set": {"status": recovered_status, "quota_release_pending": recovered_status == "blocked",
                      "error": "Dispatch was interrupted; review notification status", "completed_at": now()}})
        if recovered.matched_count:
            await db.product_requests.update_one({"_id": old["request_id"], "notification_id": old["_id"], "notification_status": {"$in": ["queued", "checking", "sending"]}},
                {"$set": {"notification_status": recovered_status}})
            if old["status"] == "preparing":
                await db.product_requests.update_one(
                    {"_id": old["request_id"], "org_id": old["org_id"], **old["snapshot"], "notification_status": None},
                    {"$set": {"notification_status": "blocked", "notification_id": old["_id"], "notification_error": "Queue preparation was interrupted; safe to retry"}})
    releases = await db.restock_outbox.find({"quota_release_pending": True, "send_attempted": False,
        "status": {"$in": ["blocked", "cancelled"]}}).limit(20).to_list(20)
    for release in releases:
        try:
            await release_quota_async(release["org_id"], "monthly_recipients", release.get("quota_event_key") or "restock:" + release["_id"])
            await db.restock_outbox.update_one({"_id": release["_id"], "started_at": release.get("started_at"),
                "quota_event_key": release.get("quota_event_key"), "send_attempted": False,
                "status": {"$in": ["blocked", "cancelled"]}}, {"$unset": {"quota_release_pending": ""}})
        except Exception:
            pass  # Durable marker remains for the next tick; no message is resent.
    job = await db.restock_outbox.find_one({"status": "queued"}, sort=[("created_at", 1)])
    if not job:
        return
    # Cross-instance rate guard. Initial upsert collision means another worker won.
    try:
        lock = await db.restock_dispatch.find_one_and_update(
            {"_id": job["org_id"], "$or": [{"next_at": {"$lte": now()}}, {"next_at": {"$exists": False}}]},
            {"$set": {"next_at": now() + timedelta(seconds=15)}}, upsert=True, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return
    if not lock:
        return
    started_at = now()
    # Refunds are scoped to one dispatch attempt: a delayed old worker must
    # never remove the quota charged to an explicit later retry.
    quota_event_key = "restock:" + job["_id"] + ":" + started_at.isoformat()
    job = await db.restock_outbox.find_one_and_update({"_id": job["_id"], "status": "queued"},
        {"$set": {"status": "checking", "started_at": started_at,
                  "quota_event_key": quota_event_key, "quota_release_pending": True}}, return_document=ReturnDocument.AFTER)
    if not job:
        return
    sent_attempted = False
    status, error, message_id = "blocked", "", None
    try:
        user = await current_outreach_actor(job)
        row = await accessible_request(user, job["request_id"])
        # Ignore our own queued flag, but revalidate all other mutable conditions before sending.
        check = {**row, "notification_status": None}
        reason = eligibility_reason(check, await product_for(row))
        if any(row.get(k) != v for k, v in job["snapshot"].items()):
            reason = "Catalogue match or permission changed after preview"
        from services.wa_service import get_broadcast_suppressed_phone_keys
        if await asyncio.to_thread(get_broadcast_suppressed_phone_keys, user.org_id, [row["customer_phone"]]):
            reason = "Customer opted out"
        if reason:
            raise HTTPException(409, reason)
        connection = await connection_for(user.org_id, row["business_phone_id"])
        templates = await get_templates(user.org_id, connection["wa_id"], connection["access_token"], name=RESTOCK_TEMPLATE)
        if not approved_restock_template(templates["templates"]):
            raise HTTPException(409, "Restock template is not approved")
        await asyncio.to_thread(require_feature, user.org_id, "BROADCASTING")
        await consume_quota_async(user.org_id, "BROADCASTING", "monthly_recipients", 1, quota_event_key)
        # Recheck after external template/quota calls. Any cancellation wins the
        # following atomic boundary unless sending has already begun.
        row = await accessible_request(user, job["request_id"])
        reason = eligibility_reason({**row, "notification_status": None}, await product_for(row))
        if reason or any(row.get(k) != v for k, v in job["snapshot"].items()):
            raise HTTPException(409, reason or "Request changed during dispatch")
        if await asyncio.to_thread(get_broadcast_suppressed_phone_keys, user.org_id, [row["customer_phone"]]):
            raise HTTPException(409, "Customer opted out")
        claimed = await db.restock_outbox.update_one(
            {"_id": job["_id"], "status": "checking", "started_at": job["started_at"]},
            {"$set": {"status": "sending", "send_attempted": True}, "$unset": {"quota_release_pending": ""}})
        if not claimed.matched_count:
            return
        sent_attempted = True
        await db.product_requests.update_one({"_id": row["_id"], "notification_id": job["_id"]}, {"$set": {"notification_status": "sending"}})
        result = await send_whatsapp_template_message(row["customer_phone"], {
            "name": RESTOCK_TEMPLATE, "language": {"code": "en_US"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": row["catalogue_name"]}]}],
        }, connection["wa_id"], user.org_id, None, "reply", None, None,
            sender_name="Restock notification", conversation_id=row["conversation_id"])
        message_id = result.get("message_id")
        status = "accepted" if message_id else "unknown"
    except Exception as exc:
        status = "unknown" if sent_attempted else "blocked"
        error = str(getattr(exc, "detail", "Restock check failed; review before retrying"))[:300]
    finally:
        if not sent_attempted:
            try:
                # A failed quota call may still have reserved quota. Release
                # this attempt only; retry remains fenced until cleanup settles.
                await release_quota_async(job["org_id"], "monthly_recipients", quota_event_key)
                await db.restock_outbox.update_one({"_id": job["_id"], "started_at": job["started_at"],
                    "quota_event_key": quota_event_key, "send_attempted": False}, {"$unset": {"quota_release_pending": ""}})
            except Exception:
                pass  # Atomic checking claim already persisted the cleanup marker.
    completed = await db.restock_outbox.update_one(
        {"_id": job["_id"], "status": "sending" if sent_attempted else "checking", "started_at": job["started_at"]},
        {"$set": {"status": status, "error": error, "message_id": message_id, "completed_at": now()}})
    if completed.matched_count:
        await db.product_requests.update_one({"_id": job["request_id"], "notification_id": job["_id"]}, {"$set": {"notification_status": status, "notification_error": error, "notification_message_id": message_id}})
