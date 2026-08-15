import re
import uuid
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import HTTPException
from loguru import logger
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from database import get_async_mongo_db
from services.followup_agent_service import analyze_customer_followup_reply, get_followup_message
from services.followup_policy import (
    CLAIM_TIMEOUT,
    FIRST_FOLLOWUP_DELAY,
    SECOND_FOLLOWUP_DELAY,
    can_auto_send,
    parse_customer_timeline,
)
from services.utils import serialize_mongo


db = get_async_mongo_db()
ACTIVE_STATUSES = ["pending", "processing", "sending", "failed", "delivery_unknown"]
CANCELLABLE_FUTURE_STATUSES = {"pending", "failed"}


async def ensure_followup_indexes() -> None:
    collection = db.scheduled_messages
    await collection.create_index([("status", 1), ("delivery_mode", 1), ("scheduled_time", 1)])
    await collection.create_index([("org_id", 1), ("created_at", -1)])
    await collection.create_index([("org_id", 1), ("conversation_id", 1), ("status", 1), ("sent_at", -1)])
    await collection.create_index([("status", 1), ("sequence_step", 1), ("sent_at", 1)])
    await collection.create_index("active_key", unique=True, sparse=True)
    await collection.create_index([("sequence_id", 1), ("sequence_step", 1)], unique=True, sparse=True)


async def _ai_is_active(org_id: str, conversation: dict) -> bool:
    organization = await db.organizations.find_one({"org_id": org_id}, {"is_ai_enabled": 1})
    return bool(organization and organization.get("is_ai_enabled") and conversation.get("is_ai_enabled", True))


def _safe_generated_text(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text or len(text) > 600:
        raise ValueError("The generated follow-up was empty or too long.")
    unsafe = ("http://", "https://", "payment link", "bank account", "account number", "upi", "ignore previous", "system prompt")
    if any(marker in text.casefold() for marker in unsafe):
        raise ValueError("The generated follow-up did not pass the automatic safety check.")
    return text


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _sync_conversation_followup_status(org_id: str, conversation_id: str) -> str:
    active_query = {
        "org_id": org_id,
        "conversation_id": conversation_id,
        "$or": [
            {"active_key": {"$exists": True}},
            {
                "status": "sent",
                "sequence_step": 1,
                "second_claim_id": {"$exists": True},
                "future_cancelled_at": {"$exists": False},
            },
        ],
    }
    while True:
        active = bool(await db.scheduled_messages.find_one(active_query, {"_id": 1}))
        status = "follow-up" if active else "open"
        await db[f"conversations_{org_id}"].update_one(
            {"conversation_id": conversation_id, "status": {"$in": ["open", "follow-up"]}},
            {"$set": {"status": status}},
        )
        if active == bool(await db.scheduled_messages.find_one(active_query, {"_id": 1})):
            return status


async def _timeline_reference(org_id: str, conversation_id: str, expression: str) -> datetime | None:
    message = await db[f"messages_{org_id}"].find_one(
        {
            "conversation_id": conversation_id,
            "role": "customer",
            "content": {"$regex": re.escape(expression), "$options": "i"},
        },
        {"timestamp": 1},
        sort=[("timestamp", -1)],
    )
    return message.get("timestamp") if message else None


async def schedule_followup_message(
    org_id: str,
    conversation_id: str,
    source: str = "manual",
    quote_id: str | None = None,
    requested_by: str | None = None,
    requested_by_name: str | None = None,
    scheduled_for: datetime | None = None,
) -> dict:
    if not org_id:
        raise HTTPException(status_code=400, detail="Organization ID not found.")
    platform = conversation_id.split("_", 1)[0]
    if platform not in {"whatsapp", "instagram"}:
        raise HTTPException(status_code=422, detail="Follow-ups support WhatsApp and Instagram only.")

    now = datetime.now(timezone.utc)
    conversations = db[f"conversations_{org_id}"]
    scheduled = db.scheduled_messages
    conversation = await conversations.find_one({"conversation_id": conversation_id})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    active_key = f"{org_id}:{conversation_id}"
    existing = await scheduled.find_one({
        "org_id": org_id,
        "conversation_id": conversation_id,
        "$or": [
            {"active_key": active_key},
            {"active_key": {"$exists": False}, "status": {"$in": ACTIVE_STATUSES}},
        ],
    })
    if existing and source != "quotation":
        return {"message": "A follow-up is already active.", "followup": serialize_mongo(existing)}
    if existing and (existing.get("status") not in {"pending", "failed"} or existing.get("sequence_step", 1) != 1):
        return {"message": "A follow-up is already active.", "followup": serialize_mongo(existing)}
    if conversation.get("status") not in {"open", "follow-up"} and not existing:
        raise HTTPException(status_code=409, detail="Reopen this conversation before scheduling a follow-up.")

    ai_active = await _ai_is_active(org_id, conversation)
    if not ai_active and not scheduled_for:
        return {
            "message": "AI is off. Ask the sales agent when this follow-up should happen.",
            "requires_agent_time": True,
        }
    if scheduled_for:
        scheduled_for = _as_utc(scheduled_for)
        if scheduled_for <= now:
            raise HTTPException(status_code=422, detail="Choose a follow-up time in the future.")

    quotes = db[f"quotes_{org_id}"]
    if quote_id:
        quote = await quotes.find_one({
            "org_id": org_id,
            "conversation_id": conversation_id,
            "quote_id": quote_id,
        })
        if not quote:
            raise HTTPException(status_code=404, detail="Quote not found for this conversation.")
    else:
        quote = await quotes.find_one(
            {"org_id": org_id, "conversation_id": conversation_id},
            sort=[("created_at", -1)],
        )

    receiver_id = conversation.get("customer_id")
    sender_id = conversation.get("whatsapp_id" if platform == "whatsapp" else "instagram_id")
    if not receiver_id or not sender_id:
        raise HTTPException(status_code=422, detail="This conversation has no connected sender or customer.")

    followup_text = "Sales agent follow-up reminder"
    second_followup_text = None
    timeline_text = None
    if ai_active:
        quote_context = ""
        if quote and source == "quotation":
            quote_context = (
                f"Quotation {quote.get('quote_number', quote_id)} with a total of "
                f"INR {quote.get('public_grand_total', 0):,.0f} was just shared."
            )
        try:
            generated = await get_followup_message(org_id, conversation_id, quote_context)
            followup_text = _safe_generated_text(generated.followup)
            second_followup_text = _safe_generated_text(generated.second_followup)
            timeline_text = generated.timeline_expression
        except Exception as exc:
            logger.error(f"Follow-up generation failed for org={org_id}, conversation={conversation_id}: {exc}")
            raise HTTPException(status_code=502, detail="The follow-up draft could not be generated. Try again.") from exc

    timeline_reference = await _timeline_reference(org_id, conversation_id, timeline_text) if timeline_text else None
    parsed_timeline = parse_customer_timeline(timeline_text, timeline_reference or now) if timeline_text else None
    if parsed_timeline and parsed_timeline <= now + timedelta(minutes=5):
        parsed_timeline = None
    if timeline_text and not parsed_timeline and not scheduled_for:
        await conversations.update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "customer_buying_timeline": timeline_text,
                "customer_buying_timeline_updated_at": now,
            }},
        )
        return {
            "message": f'The customer said “{timeline_text}”. Ask the sales agent to choose the exact follow-up time.',
            "requires_agent_time": True,
            "timeline_text": timeline_text,
        }
    send_time = scheduled_for or parsed_timeline or (now + FIRST_FOLLOWUP_DELAY)
    delivery_mode = "auto" if ai_active and can_auto_send(
        platform,
        send_time,
        conversation.get("reply_window_ends_at"),
    ) else "agent_reminder"

    if parsed_timeline:
        await conversations.update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "customer_buying_timeline": timeline_text,
                "customer_buying_timeline_at": parsed_timeline,
                "customer_buying_timeline_updated_at": now,
            }},
        )

    document = {
        "org_id": org_id,
        "platform": platform,
        "conversation_id": conversation_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "followup_text": followup_text,
        "second_followup_text": second_followup_text,
        "scheduled_time": send_time,
        "delivery_mode": delivery_mode,
        "status": "pending",
        "active_key": active_key,
        "sequence_id": (existing.get("sequence_id") if existing else None) or str(uuid.uuid4()),
        "sequence_step": 1,
        "timeline_text": timeline_text if parsed_timeline else None,
        "timeline_source": "customer" if parsed_timeline else ("sales_agent" if scheduled_for else "default"),
        "source": source,
        "quote_id": quote_id or (quote.get("quote_id") if quote else None),
        "quote_number": quote.get("quote_number") if quote else None,
        "quote_value": quote.get("public_grand_total") if quote else None,
        "created_by": requested_by,
        "created_by_name": requested_by_name or "Team member",
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
    }
    try:
        if existing:
            document = await scheduled.find_one_and_update(
                {"_id": existing["_id"], "org_id": org_id, "status": {"$in": ["pending", "failed"]}},
                {"$set": document, "$unset": {"error": "", "processed_at": ""}},
                return_document=ReturnDocument.AFTER,
            )
            if not document:
                raise HTTPException(status_code=409, detail="The follow-up changed. Refresh and try again.")
        else:
            result = await scheduled.insert_one(document)
            document["_id"] = result.inserted_id
        await conversations.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"status": "follow-up"}},
        )
    except DuplicateKeyError:
        duplicate = await scheduled.find_one({"active_key": active_key})
        return {"message": "A follow-up is already active.", "followup": serialize_mongo(duplicate)}
    except PyMongoError as exc:
        logger.error(f"Could not save follow-up for org={org_id}, conversation={conversation_id}: {exc}")
        raise HTTPException(status_code=500, detail="The follow-up could not be saved. Try again.") from exc

    return {
        "message": "Follow-up scheduled." if delivery_mode == "auto" else "Sales-agent reminder scheduled.",
        "followup": serialize_mongo(document),
    }


async def list_scheduled_followups(org_id: str, limit: int = 200) -> list[dict]:
    followups = await db.scheduled_messages.find({"org_id": org_id}).sort("created_at", -1).limit(limit).to_list(length=limit)
    conversation_ids = list({item["conversation_id"] for item in followups if item.get("conversation_id")})
    rows = await db[f"conversations_{org_id}"].find(
        {"conversation_id": {"$in": conversation_ids}},
        {"_id": 0, "conversation_id": 1, "customer_name": 1, "customer_id": 1, "platform": 1},
    ).to_list(length=len(conversation_ids) or 1)
    conversations = {row["conversation_id"]: row for row in rows}
    children_by_sequence = {
        item.get("sequence_id"): item
        for item in followups
        if item.get("sequence_id") and item.get("sequence_step") == 2
    }

    for followup in followups:
        conversation = conversations.get(followup.get("conversation_id"), {})
        followup["customer_name"] = conversation.get("customer_name") or "Customer"
        followup["customer_phone"] = conversation.get("customer_id") or followup.get("receiver_id") or ""
        followup["taken_at"] = (
            followup.get("fulfilled_at") or followup.get("sent_at")
            or followup.get("cancelled_at") or followup.get("aborted_at")
        )
        child = children_by_sequence.get(followup.get("sequence_id"))
        followup["can_cancel_future"] = bool(
            followup.get("status") == "sent"
            and followup.get("sequence_step") == 1
            and followup.get("second_followup_text")
            and not followup.get("response_handled_at")
            and not followup.get("second_skipped_at")
            and (not child or child.get("status") in CANCELLABLE_FUTURE_STATUSES)
        )
    return serialize_mongo(followups)


async def cancel_future_followup(
    org_id: str,
    followup_id: str,
    user_id: str,
    user_name: str,
) -> dict:
    try:
        object_id = ObjectId(followup_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Follow-up not found.")

    collection = db.scheduled_messages
    current = await collection.find_one({"_id": object_id, "org_id": org_id})
    if not current:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    if current.get("future_cancelled_at"):
        await _sync_conversation_followup_status(org_id, current.get("conversation_id"))
        return {"message": "The next follow-up was already cancelled."}
    if current.get("status") != "sent" or current.get("sequence_step") != 1 or not current.get("second_followup_text"):
        raise HTTPException(status_code=409, detail="This follow-up has no future message to cancel.")

    now = datetime.now(timezone.utc)
    sequence_id = current.get("sequence_id")
    if not sequence_id:
        raise HTTPException(status_code=409, detail="This older follow-up cannot be changed safely.")
    child = await collection.find_one({"org_id": org_id, "sequence_id": sequence_id, "sequence_step": 2})
    if child and child.get("status") == "delivery_unknown":
        raise HTTPException(
            status_code=409,
            detail="Delivery is uncertain. Check the chat before changing this follow-up.",
        )
    if child and child.get("status") in {"processing", "sending", "sent"}:
        raise HTTPException(status_code=409, detail="The next follow-up is already being sent or was sent.")

    resolution = {
        "status": "cancelled",
        "cancelled_at": now,
        "updated_at": now,
        "resolved_by": user_id,
        "resolved_by_name": user_name or "Team member",
    }
    if child and child.get("status") == "cancelled":
        pass
    elif child:
        cancelled = await collection.update_one(
            {
                "_id": child["_id"],
                "org_id": org_id,
                "status": {"$in": list(CANCELLABLE_FUTURE_STATUSES)},
            },
            {"$set": resolution, "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""}},
        )
        if not cancelled.modified_count:
            raise HTTPException(status_code=409, detail="The next follow-up is already being sent or was sent.")
    else:
        cancelled_child = {
            **{key: current.get(key) for key in (
                "org_id", "platform", "conversation_id", "sender_id", "receiver_id",
                "sequence_id", "quote_id", "quote_number", "quote_value",
            )},
            "followup_text": current["second_followup_text"],
            "scheduled_time": _as_utc(current.get("sent_at") or now) + SECOND_FOLLOWUP_DELAY,
            "delivery_mode": current.get("delivery_mode", "auto"),
            "sequence_step": 2,
            "parent_followup_id": str(current["_id"]),
            "parent_sent_at": current.get("sent_at"),
            "source": "auto_retry",
            "created_by": "ai-followup",
            "created_by_name": "AI follow-up",
            "created_at": now,
            **resolution,
        }
        try:
            await collection.insert_one(cancelled_child)
        except DuplicateKeyError:
            child = await collection.find_one({"org_id": org_id, "sequence_id": sequence_id, "sequence_step": 2})
            if child and child.get("status") == "delivery_unknown":
                raise HTTPException(
                    status_code=409,
                    detail="Delivery is uncertain. Check the chat before changing this follow-up.",
                )
            if not child or child.get("status") not in CANCELLABLE_FUTURE_STATUSES:
                raise HTTPException(status_code=409, detail="The next follow-up is already being sent or was sent.")
            cancelled = await collection.update_one(
                {"_id": child["_id"], "org_id": org_id, "status": child["status"]},
                {"$set": resolution, "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""}},
            )
            if not cancelled.modified_count:
                raise HTTPException(status_code=409, detail="The next follow-up is already being sent or was sent.")

    updated = await collection.update_one(
        {
            "_id": object_id,
            "org_id": org_id,
            "status": "sent",
            "sequence_step": 1,
            "future_cancelled_at": {"$exists": False},
        },
        {
            "$set": {
                "second_skipped_at": now,
                "second_skip_reason": "Cancelled by a team member.",
                "future_cancelled_at": now,
                "future_cancelled_by": user_id,
                "future_cancelled_by_name": user_name or "Team member",
                "updated_at": now,
            },
            "$unset": {"second_claim_id": "", "second_claimed_at": "", "active_key": ""},
        },
    )
    if not updated.modified_count and not (await collection.find_one(
        {"_id": object_id, "org_id": org_id}, {"future_cancelled_at": 1}
    ) or {}).get("future_cancelled_at"):
        raise HTTPException(status_code=409, detail="The follow-up changed. Refresh and try again.")
    await _sync_conversation_followup_status(org_id, current.get("conversation_id"))
    return {"message": "The next follow-up was cancelled."}


async def transition_scheduled_followup(
    org_id: str,
    followup_id: str,
    status: str,
    user_id: str,
    user_name: str,
) -> dict:
    try:
        object_id = ObjectId(followup_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Follow-up not found.")

    collection = db.scheduled_messages
    current = await collection.find_one({"_id": object_id, "org_id": org_id})
    if not current:
        raise HTTPException(status_code=404, detail="Follow-up not found.")
    allowed = {
        "cancelled": {"pending", "failed"},
        "fulfilled": {"pending", "sent", "failed", "delivery_unknown"},
    }
    if current.get("status") == status:
        return serialize_mongo(current)
    if current.get("status") not in allowed[status]:
        raise HTTPException(status_code=409, detail="This follow-up is being sent or is already closed.")

    now = datetime.now(timezone.utc)
    updated = await collection.find_one_and_update(
        {"_id": object_id, "org_id": org_id, "status": current["status"]},
        {
            "$set": {
                "status": status,
                f"{status}_at": now,
                "updated_at": now,
                "resolved_by": user_id,
                "resolved_by_name": user_name or "Team member",
            },
            "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="The follow-up changed. Refresh and try again.")
    await _sync_conversation_followup_status(org_id, current.get("conversation_id"))
    return serialize_mongo(updated)


async def abort_scheduled_message(org_id: str, conversation_id: str) -> dict:
    now = datetime.now(timezone.utc)
    result = await db.scheduled_messages.update_many(
        {
            "conversation_id": conversation_id,
            "org_id": org_id,
            "status": {"$in": ["pending", "processing"]},
        },
        {
            "$set": {"status": "aborted", "aborted_at": now, "updated_at": now},
            "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""},
        },
    )
    return {"message": f"Aborted {result.modified_count} scheduled follow-up(s)."}


async def _create_escalation(org_id: str, conversation_id: str, content: str, followup: dict, reason: str) -> None:
    from services.team_service import create_ai_escalation

    conversations = db[f"conversations_{org_id}"]
    conversation = await conversations.find_one({"conversation_id": conversation_id}) or {}
    now = datetime.now(timezone.utc)
    await conversations.update_one(
        {"conversation_id": conversation_id},
        {"$set": {"is_ai_enabled": False, "followup_escalated_at": now}},
    )
    await create_ai_escalation(org_id, {
        "user_name": conversation.get("customer_name") or "Customer",
        "customer_number": conversation.get("customer_id") or conversation_id,
        "customer_text": content,
        "inferred_intent": "Follow-up reply needs a sales agent",
        "metadata": {
            "reason": reason,
            "conversation_id": conversation_id,
            "followup_id": str(followup["_id"]),
            "action": "Choose the next follow-up time",
        },
    })


async def handle_followup_customer_reply(org_id: str, conversation_id: str, content: str) -> bool:
    """Return True when this reply was intercepted and normal AI must not answer it."""
    claim_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    collection = db.scheduled_messages
    followup = await collection.find_one_and_update(
        {
            "org_id": org_id,
            "conversation_id": conversation_id,
            "status": "sent",
            "delivery_mode": "auto",
            "sent_at": {"$lte": now},
            "response_handled_at": {"$exists": False},
            "$or": [
                {"response_claim_id": {"$exists": False}},
                {"response_claimed_at": {"$lt": now - CLAIM_TIMEOUT}},
            ],
        },
        {"$set": {"response_claim_id": claim_id, "response_claimed_at": now}},
        sort=[("sent_at", -1)],
        return_document=ReturnDocument.AFTER,
    )
    if not followup:
        return False

    await abort_scheduled_message(org_id, conversation_id)
    conversation = await db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id}) or {}
    ai_active = await _ai_is_active(org_id, conversation)
    timeline_text = None
    timeline_at = None
    reason = "AI is off. A sales agent must choose the next follow-up time."
    if ai_active:
        try:
            analysis = await analyze_customer_followup_reply(content)
            timeline_text = analysis.timeline_expression if analysis.outcome == "timeline" else None
            timeline_at = parse_customer_timeline(timeline_text, now)
            reason = analysis.reason or "The reply needs a sales agent."
        except Exception as exc:
            logger.error(f"Follow-up reply analysis failed for org={org_id}, conversation={conversation_id}: {exc}")
            reason = "The follow-up reply could not be classified automatically."

    if timeline_text:
        await db[f"conversations_{org_id}"].update_one(
            {"conversation_id": conversation_id},
            {"$set": {
                "customer_buying_timeline": timeline_text,
                "customer_buying_timeline_at": timeline_at,
                "customer_buying_timeline_updated_at": now,
            }},
        )

    # A second follow-up may have become due while reply analysis was running.
    await abort_scheduled_message(org_id, conversation_id)

    if timeline_text and timeline_at:
        await collection.update_one(
            {"_id": followup["_id"], "response_claim_id": claim_id},
            {
                "$set": {"response_handled_at": now, "response_outcome": "timeline", "response_timeline": timeline_text},
                "$unset": {"response_claim_id": "", "response_claimed_at": "", "active_key": ""},
            },
        )
        try:
            await schedule_followup_message(
                org_id,
                conversation_id,
                requested_by="ai-followup",
                requested_by_name="AI follow-up",
                scheduled_for=timeline_at,
            )
        except Exception as exc:
            logger.error(f"Timeline follow-up scheduling failed for org={org_id}, conversation={conversation_id}: {exc}")
            reason = "The timeline was saved, but the next follow-up needs a sales agent."
            await _create_escalation(org_id, conversation_id, content, followup, reason)
            await collection.update_one(
                {"_id": followup["_id"]},
                {"$set": {"response_outcome": "escalated", "response_reason": reason}},
            )
        return True

    await _create_escalation(org_id, conversation_id, content, followup, reason)
    await collection.update_one(
        {"_id": followup["_id"], "response_claim_id": claim_id},
        {
            "$set": {"response_handled_at": now, "response_outcome": "escalated", "response_reason": reason},
            "$unset": {"response_claim_id": "", "response_claimed_at": "", "active_key": ""},
        },
    )
    return True


async def _create_due_second_followups(now: datetime) -> None:
    collection = db.scheduled_messages
    stale = now - CLAIM_TIMEOUT
    candidates = await collection.find({
        "status": "sent",
        "sequence_step": 1,
        "second_followup_text": {"$type": "string", "$ne": ""},
        "second_scheduled_at": {"$exists": False},
        "second_skipped_at": {"$exists": False},
        "future_cancelled_at": {"$exists": False},
        "response_handled_at": {"$exists": False},
        "sent_at": {"$lte": now - SECOND_FOLLOWUP_DELAY},
        "$or": [
            {"second_claim_id": {"$exists": False}},
            {"second_claimed_at": {"$lt": stale}},
        ],
    }).sort("sent_at", 1).limit(100).to_list(length=100)
    for candidate in candidates:
        claim_id = str(uuid.uuid4())
        claimed = await collection.find_one_and_update(
            {
                "_id": candidate["_id"],
                "second_scheduled_at": {"$exists": False},
                "second_skipped_at": {"$exists": False},
                "future_cancelled_at": {"$exists": False},
                "response_handled_at": {"$exists": False},
                "response_claim_id": {"$exists": False},
                "$or": [
                    {"second_claim_id": {"$exists": False}},
                    {"second_claimed_at": {"$lt": stale}},
                ],
            },
            {
                "$set": {"second_claim_id": claim_id, "second_claimed_at": now},
                "$unset": {"active_key": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            continue
        conversation = await db[f"conversations_{claimed['org_id']}"].find_one({"conversation_id": claimed["conversation_id"]}) or {}
        ai_active = await _ai_is_active(claimed["org_id"], conversation)
        send_time = now
        delivery_mode = "auto" if ai_active and can_auto_send(
            claimed["platform"], send_time, conversation.get("reply_window_ends_at")
        ) else "agent_reminder"
        second = {
            **{key: claimed.get(key) for key in (
                "org_id", "platform", "conversation_id", "sender_id", "receiver_id",
                "sequence_id", "quote_id", "quote_number", "quote_value",
            )},
            "followup_text": claimed["second_followup_text"],
            "scheduled_time": send_time,
            "delivery_mode": delivery_mode,
            "status": "pending",
            "active_key": f"{claimed['org_id']}:{claimed['conversation_id']}",
            "sequence_step": 2,
            "parent_followup_id": str(claimed["_id"]),
            "parent_sent_at": claimed.get("sent_at"),
            "source": "auto_retry",
            "created_by": "ai-followup",
            "created_by_name": "AI follow-up",
            "created_at": now,
            "updated_at": now,
        }
        try:
            await collection.insert_one(second)
        except DuplicateKeyError:
            duplicate = await collection.find_one({
                "org_id": claimed["org_id"],
                "sequence_id": claimed["sequence_id"],
                "sequence_step": 2,
            })
            if duplicate and duplicate.get("status") in {"cancelled", "aborted", "fulfilled"}:
                await collection.update_one(
                    {"_id": claimed["_id"], "second_claim_id": claim_id},
                    {
                        "$set": {"second_skipped_at": now, "second_skip_reason": "The next follow-up was closed."},
                        "$unset": {"second_claim_id": "", "second_claimed_at": "", "active_key": ""},
                    },
                )
                await _sync_conversation_followup_status(claimed["org_id"], claimed["conversation_id"])
                continue
            if not duplicate:
                await collection.update_one(
                    {"_id": claimed["_id"], "second_claim_id": claim_id},
                    {
                        "$set": {"second_skipped_at": now, "second_skip_reason": "Another follow-up is already active."},
                        "$unset": {"second_claim_id": "", "second_claimed_at": ""},
                    },
                )
                continue
        except PyMongoError as exc:
            logger.error(f"Could not create second follow-up for {claimed['conversation_id']}: {exc}")
            continue
        scheduled_result = await collection.update_one(
            {"_id": claimed["_id"], "second_claim_id": claim_id},
            {
                "$set": {"second_scheduled_at": now},
                "$unset": {"second_claim_id": "", "second_claimed_at": ""},
            },
        )
        if not scheduled_result.modified_count:
            continue
        await _sync_conversation_followup_status(claimed["org_id"], claimed["conversation_id"])


async def process_scheduled_messages(messaging_services: dict) -> None:
    now = datetime.now(timezone.utc)
    stale = now - CLAIM_TIMEOUT
    collection = db.scheduled_messages
    await collection.update_many(
        {"status": "processing", "claimed_at": {"$lt": stale}},
        {"$set": {"status": "pending", "updated_at": now}, "$unset": {"claim_id": "", "claimed_at": ""}},
    )
    await collection.update_many(
        {"status": "sending", "claimed_at": {"$lt": stale}},
        {"$set": {
            "status": "delivery_unknown",
            "error": "Delivery could not be confirmed after a restart. Check the chat before retrying.",
            "updated_at": now,
        }, "$unset": {"claim_id": "", "claimed_at": ""}},
    )
    await _create_due_second_followups(now)

    due = await collection.find({
        "status": "pending",
        "delivery_mode": "auto",
        "scheduled_time": {"$lte": now},
    }).sort("scheduled_time", 1).limit(100).to_list(length=100)
    for item in due:
        claim_time = datetime.now(timezone.utc)
        claim_id = str(uuid.uuid4())
        claimed = await collection.find_one_and_update(
            {"_id": item["_id"], "status": "pending", "delivery_mode": "auto"},
            {"$set": {"status": "processing", "claim_id": claim_id, "claimed_at": claim_time, "updated_at": claim_time}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            continue
        conversation = await db[f"conversations_{claimed['org_id']}"].find_one({"conversation_id": claimed["conversation_id"]}) or {}
        if (
            claimed.get("sequence_step") == 2
            and conversation.get("last_sender") == "customer"
            and conversation.get("last_message_timestamp")
            and claimed.get("parent_sent_at")
            and _as_utc(conversation["last_message_timestamp"]) > _as_utc(claimed["parent_sent_at"])
        ):
            await collection.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {
                    "$set": {"status": "aborted", "aborted_at": claim_time, "updated_at": claim_time},
                    "$unset": {"active_key": "", "claim_id": "", "claimed_at": ""},
                },
            )
            continue
        if not await _ai_is_active(claimed["org_id"], conversation) or not can_auto_send(
            claimed["platform"], claim_time, conversation.get("reply_window_ends_at")
        ):
            await collection.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id, "status": "processing"},
                {
                    "$set": {"status": "pending", "delivery_mode": "agent_reminder", "updated_at": claim_time},
                    "$unset": {"claim_id": "", "claimed_at": ""},
                },
            )
            continue

        service = messaging_services.get(claimed["platform"])
        if not service:
            await collection.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id},
                {
                    "$set": {"status": "failed", "error": "Unsupported platform", "updated_at": claim_time},
                    "$unset": {"claim_id": "", "claimed_at": ""},
                },
            )
            continue
        started = await collection.update_one(
            {"_id": claimed["_id"], "claim_id": claim_id, "status": "processing"},
            {"$set": {"status": "sending", "send_started_at": claim_time, "updated_at": claim_time}},
        )
        if not started.modified_count:
            continue
        try:
            response = await service.send_message(
                sender_id=claimed["sender_id"],
                recipient_id=claimed["receiver_id"],
                message=claimed["followup_text"],
                org_id=claimed["org_id"],
                conversation_id=claimed["conversation_id"],
                is_auto_followup=True,
                temp_id=f"followup:{claimed['_id']}",
                sender_name="AI follow-up",
            )
            update = {
                "$set": {"status": "sent", "sent_at": datetime.now(timezone.utc), "processed_at": datetime.now(timezone.utc)},
                "$unset": {"claim_id": "", "claimed_at": "", "error": ""},
            }
            if claimed.get("sequence_step") != 1 or not claimed.get("second_followup_text"):
                update["$unset"]["active_key"] = ""
            await collection.update_one({"_id": claimed["_id"], "claim_id": claim_id, "status": "sending"}, update)
            logger.info(f"Sent follow-up to {claimed['conversation_id']}: {response}")
        except Exception as exc:
            await collection.update_one(
                {"_id": claimed["_id"], "claim_id": claim_id, "status": "sending"},
                {
                    "$set": {"status": "failed", "error": str(exc)[:500], "processed_at": datetime.now(timezone.utc)},
                    "$unset": {"claim_id": "", "claimed_at": ""},
                },
            )
            logger.error(f"Follow-up send failed for {claimed['conversation_id']}: {exc}")
