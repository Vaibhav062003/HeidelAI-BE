"""Audit formal quotes and prices after the provider accepts the reply."""

from datetime import datetime, timezone
from typing import Optional

from pymongo.errors import DuplicateKeyError

from database import get_async_mongo_db
from services.sales_offer_policy import (
    DELIVERY_RANK,
    explicit_total,
    extract_quoted_prices,
    is_outbound_offer,
    next_delivery_status,
    offer_prices,
    quote_revision_snapshot,
    structured_offer_snapshot,
    tax_basis,
)


def _collection(org_id: str):
    return get_async_mongo_db()[f"sales_offers_{org_id}"]


def _delivery_collection(org_id: str):
    return get_async_mongo_db()[f"sales_offer_delivery_{org_id}"]


def _outbox_collection(org_id: str):
    return get_async_mongo_db()[f"sales_offer_outbox_{org_id}"]


_VERIFIED_OUTCOME_ORDER = {"superseded": -1, "open": 0, "accepted": 1, "billed": 2, "paid": 3}


async def ensure_sales_offer_indexes() -> None:
    db = get_async_mongo_db()
    organizations = await db.organizations.find({}, {"org_id": 1}).to_list(length=None)
    for organization in organizations:
        org_id = organization.get("org_id")
        if not org_id:
            continue
        offers = db[f"sales_offers_{org_id}"]
        await offers.create_index([("agent_id", 1), ("status", 1), ("source_type", 1), ("created_at", -1)])
        await offers.create_index([("conversation_id", 1), ("created_at", -1)])
        await db[f"sales_offer_delivery_{org_id}"].create_index("updated_at", expireAfterSeconds=86_400)


def _message_id(result: Optional[dict]) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    nested = result.get("response")
    value = result.get("message_id") or (nested.get("message_id") if isinstance(nested, dict) else None)
    return str(value) if value else None


async def _broadcast_offer(org_id: str, offer: dict, action: str = "updated") -> None:
    from services.websocket_service import broadcast_org_ws

    await broadcast_org_ws(org_id, "sales_offer_updated", {
        "org_id": org_id,
        "conversation_id": offer.get("conversation_id"),
        "offer_id": str(offer.get("_id") or ""),
        "action": action,
    })


async def record_verified_offer_outcome(
    org_id: str, conversation_id: str, quote_id: str, quote_revision: int,
    status: str, event_key: str,
) -> dict | None:
    """Advance the exact formal quote from a verified Tally event."""
    if status not in {"billed", "paid"} or not quote_id or not event_key:
        return None
    collection = _collection(org_id)
    query = {
        "org_id": org_id,
        "conversation_id": conversation_id,
        "quote_id": quote_id,
        "quote_revision": int(quote_revision),
        "source_type": "formal_quote",
        "delivery_status": {"$ne": "failed"},
    }
    offer = await collection.find_one(query, sort=[("created_at", -1)])
    if not offer or event_key in (offer.get("outcome_event_keys") or []):
        return offer
    current = str(offer.get("status") or "open")
    if current not in _VERIFIED_OUTCOME_ORDER or _VERIFIED_OUTCOME_ORDER[current] >= _VERIFIED_OUTCOME_ORDER[status]:
        return offer

    now = datetime.now(timezone.utc)
    result = await collection.update_one(
        {"_id": offer["_id"], "status": current, "outcome_event_keys": {"$ne": event_key}},
        {
            "$set": {
                "status": status,
                "outcome_source": "tally_verified",
                f"{status}_at": now,
                "updated_at": now,
            },
            "$addToSet": {"outcome_event_keys": event_key},
            "$push": {"outcome_history": {
                "from": current,
                "to": status,
                "changed_at": now,
                "changed_by": "tally",
                "changed_by_name": "Tally verification",
                "event_key": event_key,
            }},
        },
    )
    if not result.modified_count:
        return await collection.find_one({"_id": offer["_id"]})
    updated = await collection.find_one({"_id": offer["_id"]})
    if updated:
        await _broadcast_offer(org_id, updated, "outcome")
    return updated


async def _supersede_older_formal_quotes(org_id: str, current: dict) -> None:
    await _collection(org_id).update_many(
        {
            "quote_id": current.get("quote_id"),
            "status": "open",
            "created_at": {"$lte": current["created_at"]},
            "_id": {"$ne": current["_id"]},
        },
        {"$set": {
            "status": "superseded",
            "superseded_by": current["_id"],
            "updated_at": datetime.now(timezone.utc),
        }},
    )


async def _restore_superseded_formal_quotes(org_id: str, failed: dict) -> None:
    await _collection(org_id).update_many(
        {"status": "superseded", "superseded_by": failed["_id"]},
        {
            "$set": {"status": "open", "updated_at": datetime.now(timezone.utc)},
            "$unset": {"superseded_by": ""},
        },
    )


async def record_outbound_offer(
    *, org_id: str, agent_id: str, agent_name: str, conversation_id: str,
    platform: str, content: str, result: Optional[dict], context: Optional[dict] = None,
) -> Optional[dict]:
    source_message_id = _message_id(result)
    if not source_message_id:
        return None
    event_id = f"{platform}:{source_message_id}"
    existing = await _collection(org_id).find_one({"_id": event_id})
    if existing:
        if platform == "whatsapp":
            await update_offer_delivery(org_id, source_message_id, "sent")
            return await _collection(org_id).find_one({"_id": event_id})
        return existing

    db = get_async_mongo_db()
    now = datetime.now(timezone.utc)
    context = context or {}
    quote_context = context.get("quote") if isinstance(context.get("quote"), dict) else None
    structured_offer = structured_offer_snapshot(context)
    conversation = await db[f"conversations_{org_id}"].find_one(
        {"conversation_id": conversation_id},
        {"customer_name": 1, "customer_phone_no": 1, "phone": 1, "username": 1},
    ) or {}
    document = {
        "_id": event_id,
        "org_id": org_id,
        "conversation_id": conversation_id,
        "platform": platform,
        "source_message_id": source_message_id,
        "agent_id": agent_id,
        "agent_name": agent_name or "Team member",
        "customer_name": conversation.get("customer_name") or conversation.get("username") or "Customer",
        "customer_phone": conversation.get("customer_phone_no") or conversation.get("phone") or "",
        "delivery_status": "sent",
        "status": "open",
        "created_at": now,
        "updated_at": now,
    }

    if quote_context:
        quote_id = str(quote_context.get("id") or "")
        quote = await db[f"quotes_{org_id}"].find_one({
            "org_id": org_id,
            "quote_id": quote_id,
            "conversation_id": conversation_id,
        })
        if not quote:
            return None
        revision = quote_context.get("revision") or quote.get("revision_number") or 1
        snapshot = quote_revision_snapshot(quote, revision)
        if not snapshot:
            return None
        amount = float(snapshot.get("public_grand_total") or 0)
        document.update({
            "source_type": "formal_quote",
            "quote_id": quote_id,
            "quote_revision": int(revision),
            "quote_number": snapshot.get("quote_number"),
            "amounts": [amount],
            "total_amount": amount,
            "tax_basis": "documented",
            "message_preview": (content or "Please find your quotation attached.")[:240],
        })
    elif structured_offer:
        document.update({
            "source_type": "chat_price",
            "amounts": structured_offer["amounts"],
            "total_amount": structured_offer["total_amount"],
            "tax_basis": structured_offer["tax_basis"],
            "line_items": structured_offer["items"],
            "message_preview": (content or "")[:240],
        })
    else:
        prices = offer_prices(content or "", extract_quoted_prices(content or ""))
        if not is_outbound_offer(content, prices):
            return None
        document.update({
            "source_type": "chat_price",
            "amounts": [float(value) for value in prices],
            "total_amount": explicit_total(content, prices),
            "tax_basis": tax_basis(content),
            "message_preview": (content or "")[:240],
        })

    receipt = await _delivery_collection(org_id).find_one({"_id": source_message_id})
    if receipt:
        document["delivery_status"] = next_delivery_status("sent", str(receipt.get("status") or "sent"))
    try:
        await _collection(org_id).insert_one(document)
    except DuplicateKeyError:
        if platform == "whatsapp":
            await update_offer_delivery(org_id, source_message_id, "sent")
        return await _collection(org_id).find_one({"_id": event_id})
    await _broadcast_offer(org_id, document, "created")
    if receipt:
        await _delivery_collection(org_id).delete_one({"_id": source_message_id})
    if platform == "whatsapp":
        await update_offer_delivery(org_id, source_message_id, "sent")
        return await _collection(org_id).find_one({"_id": event_id})
    if document["source_type"] == "formal_quote":
        await _supersede_older_formal_quotes(org_id, document)
    return document


async def enqueue_outbound_offer(**payload) -> Optional[dict]:
    if payload.get("platform") not in {"whatsapp", "instagram"}:
        return None
    source_message_id = _message_id(payload.get("result"))
    if not source_message_id:
        return None
    context = payload.get("context")
    quote_context = context.get("quote") if isinstance(context, dict) else None
    structured_offer = context.get("offer") if isinstance(context, dict) else None
    if not isinstance(quote_context, dict) and not isinstance(structured_offer, dict):
        content = str(payload.get("content") or "")
        prices = offer_prices(content, extract_quoted_prices(content))
        if not is_outbound_offer(content, prices):
            return None
    org_id = str(payload["org_id"])
    event_id = f"{payload.get('platform')}:{source_message_id}"
    retry_payload = {key: value for key, value in payload.items() if key not in {"result", "context"}}
    retry_payload["result"] = {"message_id": source_message_id}
    retry_payload["context"] = (
        {"quote": quote_context} if isinstance(quote_context, dict)
        else {"offer": structured_offer} if isinstance(structured_offer, dict)
        else None
    )
    await _outbox_collection(org_id).update_one(
        {"_id": event_id},
        {"$setOnInsert": {
            "payload": retry_payload,
            "created_at": datetime.now(timezone.utc),
            "attempts": 0,
        }},
        upsert=True,
    )
    try:
        recorded = await record_outbound_offer(**retry_payload)
    except Exception as exc:
        await _outbox_collection(org_id).update_one(
            {"_id": event_id},
            {"$inc": {"attempts": 1}, "$set": {
                "last_error": str(exc)[:300],
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        raise
    await _outbox_collection(org_id).delete_one({"_id": event_id})
    return recorded


async def process_pending_sales_offer_audits() -> None:
    db = get_async_mongo_db()
    organizations = await db.organizations.find({}, {"org_id": 1}).to_list(length=None)
    for organization in organizations:
        org_id = organization.get("org_id")
        if not org_id:
            continue
        pending = await _outbox_collection(org_id).find().sort("created_at", 1).limit(100).to_list(length=100)
        for item in pending:
            try:
                await record_outbound_offer(**item["payload"])
                await _outbox_collection(org_id).delete_one({"_id": item["_id"]})
            except Exception as exc:
                await _outbox_collection(org_id).update_one(
                    {"_id": item["_id"]},
                    {"$inc": {"attempts": 1}, "$set": {
                        "last_error": str(exc)[:300],
                        "updated_at": datetime.now(timezone.utc),
                    }},
                )


async def update_offer_delivery(org_id: str, message_id: str, status: str) -> None:
    if status not in DELIVERY_RANK:
        return
    now = datetime.now(timezone.utc)
    incoming_rank = DELIVERY_RANK[status]
    await _delivery_collection(org_id).update_one(
        {"_id": message_id},
        [{"$set": {
            "status": {"$cond": [
                {"$gte": [incoming_rank, {"$ifNull": ["$rank", 0]}]},
                status,
                {"$ifNull": ["$status", status]},
            ]},
            "rank": {"$max": [{"$ifNull": ["$rank", 0]}, incoming_rank]},
            "updated_at": now,
        }}],
        upsert=True,
    )
    receipt = await _delivery_collection(org_id).find_one({"_id": message_id}) or {}
    offer = await _collection(org_id).find_one({"_id": f"whatsapp:{message_id}"})
    if not offer:
        return
    delivery_status = next_delivery_status(offer.get("delivery_status"), str(receipt.get("status") or status))
    await _collection(org_id).update_one(
        {"_id": offer["_id"]},
        {"$set": {"delivery_status": delivery_status, "updated_at": now}},
    )
    await _delivery_collection(org_id).delete_one({"_id": message_id})
    if offer.get("source_type") == "formal_quote":
        if delivery_status in {"delivered", "read"}:
            await _supersede_older_formal_quotes(org_id, offer)
        elif delivery_status == "failed":
            await _restore_superseded_formal_quotes(org_id, offer)
    if delivery_status != offer.get("delivery_status"):
        await _broadcast_offer(org_id, {**offer, "delivery_status": delivery_status}, "delivery")
