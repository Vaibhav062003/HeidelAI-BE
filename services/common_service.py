import uuid
import time
import re
import asyncio
from bson import ObjectId
from typing import Optional, List, Dict
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from pymongo import ReturnDocument
from datetime import datetime, timezone, timedelta
from core.services import services
from services.wa_service import send_whatsapp_message, upload_and_send_wa_media_message, send_whatsapp_template_message
from services.ig_service import send_ig_message, upload_and_send_ig_media_message
from services.utils import serialize_mongo, phone_search_parts
# from services.websocket_service import broadcast_on_stats_update
from services.websocket_service import broadcast_team_message
from services.work_team_service import (
    can_claim_conversation_ownership,
    conversation_access_condition,
    require_conversation_access,
    team_queue_condition,
)

from database import get_mongo_db, get_async_mongo_db

from loguru import logger
from dateutil import parser


db = get_mongo_db()


def validate_outbound_attachment(file) -> None:
    content_type = (file.content_type or "").lower()
    allowed = content_type.startswith(("image/", "video/", "audio/")) or content_type in {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
    }
    if not allowed:
        raise HTTPException(status_code=415, detail="Unsupported attachment type")
    max_size = 5 * 1024 * 1024 if content_type.startswith("image/") else 16 * 1024 * 1024
    if file.size is not None and file.size > max_size:
        raise HTTPException(status_code=413, detail="Attachment is too large")

FOCUS_GROUPS = {"needs_now", "high_intent", "waiting", "later"}
SEARCH_INDEXED_ORGS = set()
SEARCH_INDEX_LOCKS: Dict[str, asyncio.Lock] = {}

def build_focus_group_condition(
    focus_group: str,
    now: Optional[datetime] = None,
) -> Dict:
    if focus_group not in FOCUS_GROUPS:
        raise HTTPException(status_code=400, detail="Invalid focus group")

    now = now or datetime.now(timezone.utc)
    expiring_soon = now + timedelta(hours=2)
    active = {"status": {"$in": ["open", "follow-up"]}}
    needs_attention = {
        "$or": [
            {"unread_count": {"$gt": 0}},
            {
                "$and": [
                    {"status": "open"},
                    {
                        "reply_window_ends_at": {
                            "$gt": now,
                            "$lte": expiring_soon,
                        }
                    },
                ]
            },
        ]
    }

    conditions: List[Dict] = [
        {"type": {"$ne": "team"}},
        active,
        {"$nor": [needs_attention]},
        {"priority": {"$ne": "high"}},
    ]

    if focus_group == "needs_now":
        return {"$and": [{"type": {"$ne": "team"}}, active, needs_attention]}
    if focus_group == "high_intent":
        return {
            "$and": [
                {"type": {"$ne": "team"}},
                active,
                {"$nor": [needs_attention]},
                {"priority": "high"},
            ]
        }
    if focus_group == "waiting":
        return {"$and": [*conditions, {"status": "follow-up"}]}
    return {"$and": [*conditions, {"status": "open"}]}


async def fetch_focus_counts(org_id: str, user_id: str, role: str) -> Dict[str, int]:
    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{org_id}"]
    now = datetime.now(timezone.utc)
    access = await conversation_access_condition(org_id, user_id, role)
    facets = {
        group: [
            {"$match": {"$and": [build_focus_group_condition(group, now), access]} if access else build_focus_group_condition(group, now)},
            {"$count": "count"},
        ]
        for group in FOCUS_GROUPS
    }
    result = await collection.aggregate([{"$facet": facets}]).to_list(length=1)
    counts = {
        group: (
            result[0].get(group, [{}])[0].get("count", 0)
            if result and result[0].get(group)
            else 0
        )
        for group in FOCUS_GROUPS
    }
    counts["total_active"] = sum(counts.values())
    return counts


async def ensure_conversation_search_indexes(org_id: str) -> None:
    if org_id in SEARCH_INDEXED_ORGS:
        return
    lock = SEARCH_INDEX_LOCKS.setdefault(org_id, asyncio.Lock())
    async with lock:
        if org_id in SEARCH_INDEXED_ORGS:
            return
        async_db = get_async_mongo_db()
        conversations = async_db[f"conversations_{org_id}"]
        messages = async_db[f"messages_{org_id}"]
        await conversations.create_index("conversation_id")
        await conversations.create_index("customer_id")
        await conversations.create_index("phone_search_suffixes")
        await conversations.create_index("customer_name_prefixes")
        await conversations.create_index([("last_message_timestamp", -1)])
        await conversations.create_index([
            ("last_assignment_action", 1),
            ("last_transfer_to_agent_id", 1),
            ("assigned_agent_id", 1),
        ])
        await messages.create_index([("conversation_id", 1), ("timestamp", -1)])
        await messages.create_index(
            [("content", "text")],
            name="message_search_text",
            default_language="none",
        )
        SEARCH_INDEXED_ORGS.add(org_id)


def _search_snippet(value: object, query: str, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    match_at = text.casefold().find(query.casefold())
    start = max(0, match_at - limit // 3) if match_at >= 0 else 0
    end = min(len(text), start + limit)
    return f"{'…' if start else ''}{text[start:end]}{'…' if end < len(text) else ''}"


def _lookup_scope(value: object) -> object:
    if isinstance(value, list):
        return [_lookup_scope(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        (key if key.startswith("$") else f"conversation.{key}"): _lookup_scope(item)
        for key, item in value.items()
    }

def _escalated_condition() -> Dict:
    return {
        "$or": [
            {"type": "escalated"},
            {"priority": "urgent"},
            {"categories": {"$regex": "escalat", "$options": "i"}},
            {"labels": {"$regex": "escalat", "$options": "i"}},
        ]
    }


def _transferred_condition(current_user_id: Optional[str]) -> Dict:
    if not current_user_id:
        return {"_id": {"$exists": False}}
    return {"$and": [
        {"$or": [
            {"assigned_agent.id": current_user_id},
            {"assigned_agent_id": current_user_id},
        ]},
        {"last_assignment_action": "transferred"},
        {"last_transfer_to_agent_id": current_user_id},
        {"last_transfer_from_agent_id": {"$nin": [None, current_user_id, "system"]}},
    ]}


def _queue_filter_conditions(
    active_tab: str,
    current_user_id: Optional[str],
    attention: Optional[str],
    handling: Optional[str],
    status_filter: Optional[str],
    assigned_agent_id: Optional[str],
    team_id: Optional[str] = None,
) -> List[Dict]:
    conditions: List[Dict] = []
    if active_tab == "mine" and current_user_id:
        conditions.append({"$or": [
            {"assigned_agent.id": current_user_id},
            {"assigned_agent_id": current_user_id},
            {"active_work": {"$elemMatch": {
                "assigned_agent_id": current_user_id,
                "status": {"$in": ["pending", "in_progress", "needs_review"]},
            }}},
        ]})
    elif active_tab == "transferred":
        conditions.append(_transferred_condition(current_user_id))
    elif active_tab == "team" and team_id:
        conditions.append({"active_work": {"$elemMatch": {
                "team_id": team_id,
                "status": {"$in": ["pending", "in_progress", "needs_review"]},
            }}})
    elif active_tab == "unassigned":
        conditions.append({"$and": [
            {"$or": [
                {"assigned_agent": None},
                {"assigned_agent": {}},
                {"assigned_agent": {"$exists": False}},
            ]},
            {"$or": [
                {"assigned_agent_id": None},
                {"assigned_agent_id": {"$exists": False}},
            ]},
            {"$or": [
                {"owner_team_id": None},
                {"owner_team_id": {"$exists": False}},
            ]},
        ]})
    elif active_tab == "escalated":
        conditions.append(_escalated_condition())
    elif active_tab == "open":
        conditions.append({"status": {"$in": ["open", "follow-up"]}})
    elif active_tab == "closed":
        conditions.append({"status": "closed"})

    if attention == "needs_reply":
        conditions.append({"$and": [
            {"is_ai_enabled": {"$ne": True}},
            {"$or": [
                {"needs_reply": True},
                {"last_sender": "customer"},
                {"last_message_role": "customer"},
            ]},
        ]})
    elif attention == "unread":
        conditions.append({"unread_count": {"$gt": 0}})
    elif attention == "follow_up":
        conditions.append({"status": "follow-up"})

    if handling == "ai":
        conditions.append({"is_ai_enabled": True})
    elif handling == "human":
        conditions.append({"is_ai_enabled": {"$ne": True}})

    if status_filter in {"resolved", "closed"}:
        conditions.append({"status": "closed"})
    elif status_filter == "follow_up":
        conditions.append({"status": "follow-up"})
    elif status_filter == "escalated":
        conditions.append(_escalated_condition())
    elif status_filter == "open":
        conditions.append({"status": {"$in": ["open", None]}})

    if assigned_agent_id:
        conditions.append({"$or": [
            {"assigned_agent.id": assigned_agent_id},
            {"assigned_agent_id": assigned_agent_id},
        ]})
    return conditions


def _priority_condition(priority: Optional[str]) -> Optional[Dict]:
    values = [item.strip() for item in (priority or "").split(",") if item.strip()]
    if not values:
        return None
    if "medium" in values:
        return {"$or": [{"priority": {"$in": values}}, {"priority": None}]}
    return {"priority": {"$in": values}}


async def search_conversations(
    org_id: str,
    query: str,
    current_user_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 20,
    active_tab: str = "all",
    unread_only: bool = False,
    platform: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[str] = None,
    focus_group: Optional[str] = None,
    attention: Optional[str] = None,
    handling: Optional[str] = None,
    status_filter: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    current_user_role: str = "org:member",
    team_id: Optional[str] = None,
) -> Dict:
    clean_query = query.strip()
    if len(clean_query) < 2:
        return {"conversations": [], "has_more": False}

    await ensure_conversation_search_indexes(org_id)
    async_db = get_async_mongo_db()
    conversations = async_db[f"conversations_{org_id}"]
    messages = async_db[f"messages_{org_id}"]
    conditions: List[Dict] = [{"type": {"$ne": "team"}}]
    access = await conversation_access_condition(org_id, current_user_id or "", current_user_role)
    if access:
        conditions.append(access)
    if active_tab == "team" and team_id:
        await team_queue_condition(org_id, team_id, current_user_id or "", current_user_role)

    conditions.extend(_queue_filter_conditions(
        active_tab, current_user_id, attention, handling, status_filter, assigned_agent_id, team_id
    ))

    if focus_group:
        conditions.append(build_focus_group_condition(focus_group))
    if unread_only:
        conditions.append({"unread_count": {"$gt": 0}})
    if platform:
        conditions.append({"platform": {"$in": [item.strip() for item in platform.split(",") if item.strip()]}})
    priority_condition = _priority_condition(priority)
    if priority_condition:
        conditions.append(priority_condition)
    if labels:
        requested_labels = [item.strip() for item in labels.split(",") if item.strip()]
        if requested_labels:
            label_regexes = [re.compile(f"^{re.escape(item)}$", re.IGNORECASE) for item in requested_labels]
            matching_ids = await async_db[f"contacts_{org_id}"].distinct(
                "conversation_id", {"labels": {"$in": label_regexes}}
            )
            if not matching_ids:
                return {"conversations": [], "has_more": False}
            conditions.append({"conversation_id": {"$in": matching_ids}})

    projection = {
        "_id": 0,
        "whatsapp_id": 0,
        "instagram_id": 0,
        "raw_data": 0,
    }
    candidate_limit = skip + limit + 1
    digits, _ = phone_search_parts(clean_query)
    phone_query = len(digits) >= 6 and bool(re.fullmatch(r"[+\d\s().-]+", clean_query))
    results: Dict[str, tuple[Dict, Dict]] = {}

    def add_result(doc: Dict, match_type: str, snippet: str, message: Optional[Dict] = None) -> None:
        conversation_id = doc.get("conversation_id")
        if not conversation_id or conversation_id in results:
            return
        item = serialize_mongo(doc)
        item["id"] = conversation_id
        match = {
            "type": match_type,
            "snippet": snippet,
        }
        if message:
            match["message_id"] = message.get("id") or message.get("message_id")
            match["timestamp"] = serialize_mongo(message.get("timestamp"))
        item["search_match"] = match
        results[conversation_id] = (doc, item)

    if phone_query:
        phone_conditions: List[Dict] = [
            {"customer_id": digits},
            {"phone_digits": digits},
            {"conversation_id": f"whatsapp_{digits}"},
            {"conversation_id": f"dealer_{digits}"},
            {"phone_search_suffixes": digits},
            {"conversation_id": {"$regex": f"^(?:whatsapp|dealer)_\\d*{re.escape(digits)}$"}},
        ]
        direct_query = {"$and": [*conditions, {"$or": phone_conditions}]}
        direct_docs = await conversations.find(direct_query, projection).sort(
            "last_message_timestamp", -1
        ).limit(candidate_limit).to_list(length=candidate_limit)
        for doc in direct_docs:
            add_result(doc, "phone", str(doc.get("customer_id") or doc.get("conversation_id") or ""))
    else:
        name_terms = re.findall(r"[^\W_]+", clean_query.casefold())
        direct_docs = []
        if name_terms:
            direct_query = {"$and": [*conditions, {
                "customer_name_prefixes": {"$all": name_terms}
            }]}
            direct_docs = await conversations.find(direct_query, projection).sort(
                "last_message_timestamp", -1
            ).limit(candidate_limit).to_list(length=candidate_limit)
        for doc in direct_docs:
            name = str(doc.get("customer_name") or "")
            add_result(doc, "name", _search_snippet(name, clean_query))

        message_hits = await messages.aggregate([
            {"$match": {"$text": {"$search": clean_query}}},
            {"$sort": {"timestamp": -1, "id": -1}},
            {"$group": {
                "_id": "$conversation_id",
                "message": {"$first": "$$ROOT"},
                "match_timestamp": {"$first": "$timestamp"},
            }},
            {"$lookup": {
                "from": f"conversations_{org_id}",
                "localField": "_id",
                "foreignField": "conversation_id",
                "as": "conversation",
            }},
            {"$unwind": "$conversation"},
            {"$match": {"$and": [_lookup_scope(condition) for condition in conditions]}},
            {"$sort": {"conversation.last_message_timestamp": -1, "match_timestamp": -1, "_id": 1}},
            {"$limit": candidate_limit},
            {"$replaceRoot": {"newRoot": "$message"}},
            {"$project": {"_id": 0, "id": 1, "message_id": 1, "conversation_id": 1, "content": 1, "timestamp": 1}},
        ], allowDiskUse=True).to_list(length=candidate_limit)
        missing_ids = [
            message["conversation_id"]
            for message in message_hits
            if message.get("conversation_id") and message["conversation_id"] not in results
        ]
        if missing_ids:
            matching_docs = await conversations.find(
                {"$and": [*conditions, {"conversation_id": {"$in": missing_ids}}]},
                projection,
            ).to_list(length=len(missing_ids))
            docs_by_id = {doc.get("conversation_id"): doc for doc in matching_docs}
            for message in message_hits:
                conversation_id = message.get("conversation_id")
                doc = docs_by_id.get(conversation_id)
                if doc:
                    add_result(doc, "message", _search_snippet(message.get("content"), clean_query), message)

    def last_activity(pair: tuple[Dict, Dict]) -> float:
        value = pair[0].get("last_message_timestamp")
        if not isinstance(value, datetime):
            return 0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    ordered = sorted(
        results.values(),
        key=last_activity,
        reverse=True,
    )
    page_end = min(skip + limit, 500)
    truncated = len(ordered) > page_end and page_end >= 500
    return {
        "conversations": [item for _, item in ordered[skip:page_end]],
        "has_more": len(ordered) > page_end and not truncated,
        "truncated": truncated,
    }

async def fetch_conversations_new(
    org_id: str, 
    current_user_id: Optional[str] = None, 
    skip: int = 0, 
    limit: int = 50,
    search: Optional[str] = None,
    active_tab: str = "all",
    unread_only: bool = False,
    platform: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[str] = None,
    focus_group: Optional[str] = None,
    attention: Optional[str] = None,
    handling: Optional[str] = None,
    status_filter: Optional[str] = None,
    assigned_agent_id: Optional[str] = None,
    include_meta: bool = False,
    current_user_role: str = "org:member",
    team_id: Optional[str] = None,
) -> object:
    start_time = time.time()
    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{org_id}"]

    projection = {
        "whatsapp_id": 0,
        "instagram_id": 0,
        "next_action_at": 0,
        "reminder_stage": 0,
        "started_at": 0,
        "last_analyzed": 0,
        "_id": 0,
    }

    # Only customer conversations — team conversations are in their own
    # dedicated collection (team_conversations_{org_id}) and fetched
    # via GET /api/team/conversations
    base_conditions = [
        {"type": {"$ne": "team"}}
    ]
    access = await conversation_access_condition(org_id, current_user_id or "", current_user_role)
    if access:
        base_conditions.append(access)
    if active_tab == "team" and team_id:
        await team_queue_condition(org_id, team_id, current_user_id or "", current_user_role)
    base_conditions.extend(_queue_filter_conditions(
        active_tab,
        current_user_id,
        attention,
        handling,
        status_filter,
        assigned_agent_id,
        team_id,
    ))

    if focus_group:
        base_conditions.append(build_focus_group_condition(focus_group))

    # Labels filter
    if labels:
        labels_list = [l.strip() for l in labels.split(",") if l.strip()]
        if labels_list:
            contact_col = async_db[f"contacts_{org_id}"]
            
            # Optimization: Use regex for case-insensitive exact matching
            regex_labels = [re.compile(f"^{re.escape(l)}$", re.IGNORECASE) for l in labels_list]
            
            # Fetch unique conversation_ids from contacts who have any of these labels
            matching_ids = await contact_col.distinct("conversation_id", {"labels": {"$in": regex_labels}})
            
            if not matching_ids:
                return {"conversations": [], "has_more": False, "total": 0, "counts": {"all": 0, "mine": 0, "transferred": 0, "team": 0, "unassigned": 0, "escalated": 0}} if include_meta else []
            
            base_conditions.append({"conversation_id": {"$in": matching_ids}})

    # Unread filter
    if unread_only:
        base_conditions.append({"unread_count": {"$gt": 0}})

    # Platform Filter
    if platform:
        # Split "whatsapp,instagram" into ["whatsapp", "instagram"]
        platform_list = [p.strip() for p in platform.split(",")]
        base_conditions.append({"platform": {"$in": platform_list}})

    # Priority Filter
    priority_condition = _priority_condition(priority)
    if priority_condition:
        base_conditions.append(priority_condition)

    # Search filter
    if search:
        # $options: "i" makes the search case-insensitive
        safe_search = re.escape(search)
        search_regex = {"$regex": safe_search, "$options": "i"}
        base_conditions.append({
            "$or": [
                {"customer_name": search_regex},
                {"last_message": search_regex},
                {"conversation_id": search_regex}
            ]
        })

    # Final query
    query = {"$and": base_conditions} if base_conditions else {}

    cursor = (
        collection
        .find(query, projection)
        .sort("last_message_timestamp", -1)
        .skip(skip)   # <-- Added skip
        .limit(limit) # <-- Added limit
    )

    # Fetch all docs
    raw_docs = await cursor.to_list(length=limit)
    total = await collection.count_documents(query) if include_meta else None

    fetch_time = time.time()
    logger.info(f"[TIMING] Mongo fetch: {fetch_time - start_time:.4f}s")

    # Transform (keep it simple and fast)
    result = []
    for doc in raw_docs:
        conv_type = doc.get("type")
        platform = doc.get("platform")
        
        conv_obj = {
            "id": doc.get("conversation_id"),
            "platform": platform,
            "type": conv_type,
            "customer_id": doc.get("customer_username"),
            "phone_number": doc.get("customer_id") if platform == "whatsapp" else None,
            "customer_name": doc.get("customer_name") or "Unknown",
            "last_message": doc.get("last_message") or "",
            "last_message_is_private_note": doc.get("last_message_is_private_note", False),
            "last_message_role": doc.get("last_message_role") or doc.get("last_sender"),
            "needs_reply": doc.get("needs_reply"),
            "timestamp": (
                doc["last_message_timestamp"].isoformat()
                if isinstance(doc.get("last_message_timestamp"), datetime)
                else None
            ),
            "unread_count": doc.get("unread_count", 0),
            "message_counter": doc.get("message_counter", 0),
            "status": doc.get("status"),
            "priority": doc.get("priority"),
            "sentiment": doc.get("sentiment"),
            "assigned_agent": doc.get("assigned_agent", {}),
            "assigned_agent_id": doc.get("assigned_agent_id"),
            "owner_team_id": doc.get("owner_team_id"),
            "owner_team_name": doc.get("owner_team_name"),
            "active_work": doc.get("active_work", []),
            "is_ai_enabled": doc.get("is_ai_enabled"),
            "auto_ai_disabled": doc.get("auto_ai_disabled", False),
            "auto_ai_disabled_until": (
                doc["auto_ai_disabled_until"].isoformat()
                if isinstance(doc.get("auto_ai_disabled_until"), datetime)
                else doc.get("auto_ai_disabled_until")
            ),
            "reply_window_ends_at": doc.get("reply_window_ends_at").isoformat() if isinstance(doc.get("reply_window_ends_at"), datetime) else None,
            "assignment_history": doc.get("assignment_history", []),
            "last_assignment_action": doc.get("last_assignment_action"),
            "last_transfer_to_agent_id": doc.get("last_transfer_to_agent_id"),
            "last_transfer_from_agent_id": doc.get("last_transfer_from_agent_id"),
            "suggestions": doc.get("suggestions", []),
            "summary": doc.get("summary", []),
            "categories": doc.get("categories", []),
            "labels": doc.get("labels", []),
            "participants": doc.get("participants", []),
            "unread": doc.get("unread", {})
        }
            
        result.append(conv_obj)

    end_time = time.time()
    logger.info(f"[TIMING] Python transform: {end_time - fetch_time:.4f}s")
    logger.info(f"[TIMING] Total: {end_time - start_time:.4f}s")

    if not include_meta:
        return result

    count_scope_parts = [{"type": {"$ne": "team"}}]
    if access:
        count_scope_parts.append(access)
    count_scope = {"$and": count_scope_parts} if len(count_scope_parts) > 1 else count_scope_parts[0]
    team_conditions = _queue_filter_conditions("team", current_user_id, None, None, None, None, team_id)
    facets = {
        "all": [{"$match": count_scope}, {"$count": "value"}],
        "mine": [{"$match": {"$and": [count_scope, *_queue_filter_conditions("mine", current_user_id, None, None, None, None)]}}, {"$count": "value"}],
        "transferred": [{"$match": {"$and": [count_scope, _transferred_condition(current_user_id)]}}, {"$count": "value"}],
        "team": [{"$match": {"$and": [count_scope, *team_conditions]}} if team_conditions else {"$match": {"_id": None}}, {"$count": "value"}],
        "unassigned": [{"$match": {"$and": [count_scope, *_queue_filter_conditions("unassigned", current_user_id, None, None, None, None)]}}, {"$count": "value"}],
        "escalated": [{"$match": {"$and": [count_scope, _escalated_condition()]}}, {"$count": "value"}],
    }
    count_result = await collection.aggregate([{"$facet": facets}]).to_list(length=1)
    raw_counts = count_result[0] if count_result else {}
    counts = {
        key: (raw_counts.get(key) or [{"value": 0}])[0].get("value", 0)
        for key in facets
    }
    return {
        "conversations": result,
        "has_more": skip + len(result) < (total or 0),
        "total": total or 0,
        "counts": counts,
    }


async def fetch_conversation(org_id: str, conversation_id: str, user_id: str, role: str):
    """
    Fetch a specific conversation by its ID for a given organization.
    """
    try:
        async_db = get_async_mongo_db()

        # organization specific collection name for conversations
        org_conversations_collection_name = f"conversations_{org_id}"
        
        # Get the collection
        org_conversations_collection = async_db[org_conversations_collection_name]

        access = await conversation_access_condition(org_id, user_id, role)
        query = {"conversation_id": conversation_id}
        if access:
            query = {"$and": [query, access]}
        conversation = await org_conversations_collection.find_one(query, {"_id": 0})
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

        
        for field in conversation:
            if isinstance(conversation[field], datetime):
                conversation[field] = conversation[field].isoformat()
        
        return conversation
    
    except HTTPException:
        raise
    
    except Exception as e:
        logger.error(f"[fetch_conversation] {org_id=} {conversation_id=} error={e}")
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch conversation"
        )

async def fetch_messages(
    org_id: str,
    conversation_id: str,
    agent_id: str,
    agent_username: str,
    before: Optional[str] = None,      # ISO timestamp cursor
    limit: int = 30,
    role: str = "org:member",
):
    """
    Fetch messages and private notes for a specific conversation with cursor-based pagination.

    Arguments:
    - org_id: Organization ID
    - conversation_id: Conversation ID
    - agent_id: ID of the agent
    - agent_username: Username of the agent accessing this conversation
    - before: ISO timestamp — fetch messages strictly before this time (pagination cursor)
    - limit: Max number of messages to return (default 30)

    Returns:
    - A dictionary with:
        - 'messages': list of messages and notes (oldest→newest)
        - 'assignment': assignment details
        - 'has_more': whether older messages exist beyond this page
        - 'next_cursor': ISO timestamp to pass as `before` in the next request
    """
    try:
        await require_conversation_access(org_id, conversation_id, agent_id, role)
        org_messages_collection_name = f"messages_{org_id}"
        private_note_collection_name = f"private_notes_{org_id}"
        org_conversations_collection_name = f"conversations_{org_id}"

        convo_collection = db[org_conversations_collection_name]
        now = datetime.now(timezone.utc)
        assigned_convo = None
        if await can_claim_conversation_ownership(org_id, agent_id, role):
            assigned_convo = convo_collection.find_one_and_update(
                {
                    "conversation_id": conversation_id,
                    "$or": [
                        {"assigned_agent": {"$exists": False}},
                        {"assigned_agent": None}
                    ],
                    "status": "open"
                },
                {
                    "$set": {
                        "assigned_agent": {
                            "id": agent_id,
                            "username": agent_username,
                            "assigned_at": now
                        }
                    },
                    "$push": {
                        "assignment_history": {
                            "id": agent_id,
                            "username": agent_username,
                            "assigned_at": now
                        }
                    }
                },
                projection={"assigned_agent": 1, "assignment_history": 1, "type": 1, "participants": 1, "_id": 0},
                return_document=ReturnDocument.AFTER
            )

        if assigned_convo is None:
            assigned_convo = convo_collection.find_one(
                {"conversation_id": conversation_id},
                projection={"assigned_agent": 1, "assignment_history": 1, "type": 1, "participants": 1, "_id": 0}
            )

        # Parse compound cursor
        time_filter: dict = {"conversation_id": conversation_id}
        last_id = None

        if before:
            try:
                # Expect cursor format: "2024-01-01T12:00:00+00:00__msg_abc123"
                if "__" in before:
                    ts_part, last_id = before.split("__", 1)
                else:
                    ts_part = before  # backwards compat

                before_dt = parser.isoparse(ts_part) 
                if before_dt.tzinfo is None:
                    before_dt = before_dt.replace(tzinfo=timezone.utc)

                if last_id:
                    # Stable compound filter: same timestamp but id not yet seen, OR strictly older
                    time_filter["$or"] = [
                        {"timestamp": {"$lt": before_dt}},
                        {
                            "timestamp": before_dt,
                            "id": {"$lt": last_id}  # lexicographic tie-break on id
                        }
                    ]
                else:
                    time_filter["timestamp"] = {"$lt": before_dt}

            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid `before` cursor")

        fetch_limit = limit + 1

        messages, notes = [], []

        if org_messages_collection_name in db.list_collection_names():
            messages = list(
                db[org_messages_collection_name]
                .find(time_filter, {"_id": 0})
                .sort([("timestamp", -1), ("id", -1)])  # ← compound sort
                .limit(fetch_limit)
            )

        if private_note_collection_name in db.list_collection_names():
            notes = list(
                db[private_note_collection_name]
                .find(time_filter, {"_id": 0})
                .sort([("timestamp", -1), ("id", -1)])  # ← compound sort
                .limit(fetch_limit)
            )

        combined = messages + notes
        combined.sort(key=lambda x: (x.get("timestamp"), x.get("id")), reverse=True)

        has_more = len(combined) > limit
        if has_more:
            combined = combined[:limit]

        # Normalize timestamps
        for msg in combined:
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                msg["timestamp"] = ts.isoformat()

        combined.reverse()  # back to chronological for frontend

        # ← Compound cursor: oldest message's timestamp + id
        if has_more and combined:
            oldest = combined[0]
            next_cursor = f"{oldest.get('timestamp')}__{oldest.get('id', '')}"
        else:
            next_cursor = None

        return {
            "messages": combined,      
            "assignment": assigned_convo,
            "has_more": has_more,
            "next_cursor": next_cursor
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching messages for {conversation_id} of org {org_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch messages: {str(e)}")


async def acknowledge_conversation_opened(org_id: str, conversation_id: str, observed_counter: int) -> dict:
    result = await get_async_mongo_db()[f"conversations_{org_id}"].update_one(
        {
            "conversation_id": conversation_id,
            "$expr": {"$eq": [{"$ifNull": ["$message_counter", 0]}, observed_counter]},
        },
        {"$set": {"unread_count": 0}},
    )
    return {"status": "ok", "acknowledged": result.matched_count > 0}


async def search_conversation_messages(
    org_id: str,
    conversation_id: str,
    query: str,
    limit: int = 50,
) -> Dict:
    clean_query = query.strip()
    if len(clean_query) < 3:
        return {"matches": []}
    await ensure_conversation_search_indexes(org_id)
    collection = get_async_mongo_db()[f"messages_{org_id}"]
    projection = {"_id": 0, "id": 1, "message_id": 1, "content": 1, "timestamp": 1, "role": 1, "type": 1}
    rows = await collection.find(
        {"conversation_id": conversation_id, "$text": {"$search": clean_query}},
        projection,
    ).sort("timestamp", 1).limit(limit).to_list(length=limit)
    return {
        "matches": [
            {
                "message_id": str(row.get("id") or row.get("message_id") or ""),
                "timestamp": serialize_mongo(row.get("timestamp")),
                "snippet": _search_snippet(row.get("content"), clean_query),
                "sender": row.get("role"),
                "type": row.get("type"),
            }
            for row in rows
            if row.get("id") or row.get("message_id")
        ]
    }


async def fetch_message_context(
    org_id: str,
    conversation_id: str,
    message_id: str,
    radius: int = 15,
) -> Dict:
    collection = get_async_mongo_db()[f"messages_{org_id}"]
    projection = {
        "_id": 0,
        "id": 1,
        "message_id": 1,
        "conversation_id": 1,
        "content": 1,
        "timestamp": 1,
        "role": 1,
        "type": 1,
        "payload": 1,
        "context": 1,
        "status": 1,
        "sender_name": 1,
    }
    target = await collection.find_one(
        {
            "conversation_id": conversation_id,
            "$or": [{"id": message_id}, {"message_id": message_id}],
        },
        projection,
    )
    if not target:
        raise HTTPException(status_code=404, detail="Message not found in this conversation")
    timestamp = target.get("timestamp")
    if timestamp is None:
        return {"messages": [serialize_mongo(target)]}

    target_id = str(target.get("id") or target.get("message_id") or message_id)
    common = {"conversation_id": conversation_id}
    older_boundary = {"$or": [
        {"timestamp": {"$lt": timestamp}},
        {"timestamp": timestamp, "$or": [
            {"id": {"$lt": target_id}},
            {"id": {"$exists": False}, "message_id": {"$lt": target_id}},
        ]},
    ]}
    newer_boundary = {"$or": [
        {"timestamp": {"$gt": timestamp}},
        {"timestamp": timestamp, "$or": [
            {"id": {"$gt": target_id}},
            {"id": {"$exists": False}, "message_id": {"$gt": target_id}},
        ]},
    ]}
    older = await collection.find(
        {"$and": [common, older_boundary]}, projection
    ).sort([("timestamp", -1), ("id", -1), ("message_id", -1)]).limit(radius).to_list(length=radius)
    newer = await collection.find(
        {"$and": [common, newer_boundary]}, projection
    ).sort([("timestamp", 1), ("id", 1), ("message_id", 1)]).limit(radius).to_list(length=radius)
    messages = [*reversed(older), target, *newer]
    normalized = []
    for message in messages:
        item = serialize_mongo(message)
        item["id"] = str(item.get("id") or item.get("message_id") or "")
        normalized.append(item)
    return {"messages": normalized}

async def close_conversation(org_id: str, conversation_id: str, reason: str):
    """
    Mark a conversation as closed for a given organization.
    """
    try:
        conversation_collection = db[f"conversations_{org_id}"]

        now = datetime.now(timezone.utc)

        conversation =  conversation_collection.find_one({"conversation_id": conversation_id})

        if not conversation:
            return {"message": "Conversation not found. Nothing to close."}

        result = conversation_collection.find_one_and_update(
            {"conversation_id": conversation_id},
            {
                "$set": {
                    "status": "closed",
                    "closed_at": now,
                    "assigned_agent": None,
                    "closure_reason": reason
                }
            },
            return_document=ReturnDocument.AFTER
        )

        if "assignment_history" in conversation and conversation["assignment_history"]:
            last_index = len(conversation["assignment_history"]) - 1
            update_path = f"assignment_history.{last_index}.assigned_until"

            result = conversation_collection.find_one_and_update(
                {"conversation_id": conversation_id},
                {"$set": {update_path: now}},
                return_document=ReturnDocument.AFTER
            )

        convo = serialize_mongo(result)

        # await broadcast_on_stats_update(org_id)

        return {"conversation": convo}

    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

async def unarchive_data(org_id: str, platform: str, user_id: str):
    """
    Unarchives data from the archived collections back to the main collections. 
    For all the platforms.
    """

    try:
        query = {
            "org_id": org_id,
            "platform": platform,
            f"{platform}_id": user_id,
            "status": "active"
        }

        archived_meta_data = db["archived_tracker"].find_one(query)

        if not archived_meta_data:
            raise HTTPException(status_code=404, detail=f"No active archive found for {query}: {str(e)}")

        archive_id = archived_meta_data.get("archive_id")

        archive_messages = f"archived_messages_{org_id}_{archive_id}"
        archive_conversations = f"archived_conversations_{org_id}_{archive_id}"
        archive_private_notes = f"archived_private_notes_{org_id}_{archive_id}"

        main_messages = f"messages_{org_id}"
        main_conversations = f"conversations_{org_id}"
        main_private_notes = f"private_notes_{org_id}"

        move_conversations_unarchive_safe(archive_conversations, main_conversations)

        move_messages_unarchive_safe(archive_messages, main_messages)

        move_private_notes_unarchive_safe(archive_private_notes, main_private_notes)

        db["archived_tracker"].update_one(
            {"archive_id": archive_id},
            {"$set": {"status": "recovered", "restored_at": datetime.utcnow()}}
        )

        logger.info(f"Dropping archived collections: {archive_conversations}, {archive_messages}")
        db.drop_collection(archive_conversations)
        db.drop_collection(archive_messages)
        db.drop_collection(archive_private_notes)

        logger.success(f"Unarchiving completed for org_id={org_id}, platform={platform}")

        return {"message": "Unarchiving completed successfully."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
def move_messages_unarchive_safe(source_name, target_name):
    cursor = db[source_name].find({}, {"_id": 0})  
    buffer = []
    for doc in cursor:
        buffer.append(doc)
        if len(buffer) >= services.BATCH_SIZE:
            db[target_name].insert_many(buffer)
            buffer = []
    if buffer:
        db[target_name].insert_many(buffer)
    
    db[source_name].delete_many({})

def move_private_notes_unarchive_safe(source_name: str, target_name: str):
    """Moves private note docs from archive to main collection."""
    cursor = db[source_name].find({}, {"_id": 0})

    buffer = []
    for note in cursor:
        buffer.append(note)

        if len(buffer) >= services.BATCH_SIZE:
            if buffer:
                db[target_name].insert_many(buffer)
                buffer = []

    if buffer:
        db[target_name].insert_many(buffer)

    db[source_name].delete_many({})

def move_conversations_unarchive_safe(source_name: str, target_name: str):
    """
    Moves conversation docs from archive to main collection.
    If a doc with the same conversation_id already exists in main,
    skip inserting that doc (keep the main one).
    """
    cursor = db[source_name].find({}, {"_id": 0})

    buffer = []
    for convo in cursor:
        existing = db[target_name].find_one({"conversation_id": convo["conversation_id"]})
        if not existing:
            buffer.append(convo)

        if len(buffer) >= services.BATCH_SIZE:
            if buffer:
                db[target_name].insert_many(buffer)
                buffer = []

    if buffer:
        db[target_name].insert_many(buffer)

    db[source_name].delete_many({})

async def add_private_note(org_id: str, conversation_id: str, note: str, posted_by: str, role: str, platform: str, connection_id: str) -> bool:
    """
    Adds a private note for a specific conversation.
    Stores the note in the private_notes collection
    """

    try:
        private_notes_collection = db[f"private_notes_{org_id}"]

        note_doc = {
            "note_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "platform": platform,
            "connection_id": connection_id,
            "content": note,
            "timestamp": datetime.now(timezone.utc),
            "user_name": posted_by,
            "role": role
        }

        result = private_notes_collection.insert_one(note_doc)

        if not result.inserted_id:
            raise HTTPException(status_code=500, detail="Failed to add private note")
        
        return True
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    
async def delete_private_note(org_id: str, note_id: str) -> bool:
    """
    Removes the private_note field from a specific conversation document.
    """

    try:
        messages_collection = db[f"messages_{org_id}"]

        try:
            _id = ObjectId(note_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid note ID")

        note = messages_collection.find_one({
            "_id": _id, "message_type": "private_note"
        })

        if not note:
            raise HTTPException(status_code=404, detail="Private note not found")
        
        messages_collection.delete_one({"_id": _id})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting private note: {str(e)}")

async def send_message(org_id: str, platform: str, conversation_id: str, content: str, mode:str, sender_name: Optional[str] = "Agent", file: Optional[any] = None, temp_id: Optional[str] = None, context_type: Optional[str] = None, context: Optional[dict] = None, timestamp: Optional[str] = None, media_kind: Optional[str] = None, duration_ms: Optional[int] = None):
    """
    Sends a message to the specified conversation on the given platform.
    """
    res = {"status": "no_action"}

    conversation = None
    if platform in {"whatsapp", "instagram"}:
        conversation = db[f"conversations_{org_id}"].find_one({"conversation_id": conversation_id})
        stored_platform = (conversation or {}).get("platform") or conversation_id.split("_", 1)[0]
        if not conversation or stored_platform != platform:
            raise HTTPException(status_code=404, detail="Conversation not found for this organization and channel")

    if platform == "whatsapp":
        wa_id = conversation.get("whatsapp_id")
        recipient_id = conversation.get("customer_id")
        if not wa_id or not recipient_id:
            raise HTTPException(status_code=422, detail="This WhatsApp conversation has no connected sender or customer")
        connection = db.whatsapp_connections.find_one({
            "org_id": org_id,
            "wa_id": wa_id,
            "is_active": {"$ne": False},
        }, {"_id": 1})
        if not connection:
            raise HTTPException(status_code=409, detail="The WhatsApp number for this conversation is not connected")

        if file:
            res = await upload_and_send_wa_media_message(recipient_id, file, wa_id, org_id, content, temp_id, mode, context_type, context, sender_name, media_kind, duration_ms)

            if res.get("error"):
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to send media message: {res['error']}"
                )

        if content and not file:
            # Check if content is a JSON template
            try:
                import json
                content_data = json.loads(content)
                if isinstance(content_data, dict) and "name" in content_data and "language" in content_data:
                    res = await send_whatsapp_template_message(
                        recipient_id=recipient_id,
                        template=content_data,
                        wa_id=wa_id,
                        org_id=org_id,
                        temp_id=temp_id,
                        mode=mode,
                        context_type=context_type,
                        context=context,
                        sender_name=sender_name
                    )
                else:
                    raise ValueError("Not a template")
            except (json.JSONDecodeError, ValueError, TypeError):
                res = await send_whatsapp_message(
                    {
                        "conversation_id": conversation_id,
                        "content": content,
                        "sender_id": wa_id,
                    }, 
                    org_id=org_id, 
                    mode=mode, 
                    temp_id=temp_id, 
                    context_type=context_type, 
                    context=context, 
                    sender_name=sender_name
                )
        return res
    
    elif platform == "instagram":
        ig_id = conversation.get("instagram_id")
        if not ig_id:
            raise HTTPException(status_code=422, detail="This Instagram conversation has no connected sender")
        connection = db.instagram_connections.find_one({
            "org_id": org_id,
            "instagram_id": ig_id,
            "is_active": True,
        }, {"_id": 1})
        if not connection:
            raise HTTPException(status_code=409, detail="The Instagram account for this conversation is not connected")
        if not conversation or not conversation.get("customer_id"):
            raise HTTPException(status_code=404, detail="Instagram conversation not found")
        recipient = conversation["customer_id"]

        media_result = None
        if file:
            media_result = await upload_and_send_ig_media_message(
                recipient,
                file,
                ig_id,
                org_id,
                mode,
                temp_id=temp_id,
                conversation_id=conversation_id,
                sender_name=sender_name,
                context_type=context_type,
                context=context,
            )

            if not isinstance(media_result, dict) or not media_result.get("success") or not media_result.get("message_id"):
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to send media message: {media_result.get('error', 'Instagram did not confirm the message') if isinstance(media_result, dict) else 'Invalid response'}"
                )
        if content:
            text_result = await send_ig_message(
                recipient,
                content,
                ig_id,
                mode,
                org_id,
                temp_id=f"{temp_id}:caption" if file and temp_id else temp_id,
                conversation_id=conversation_id,
                sender_name=sender_name,
                context_type=context_type,
                context=context,
            )
            if not isinstance(text_result, dict) or not text_result.get("success") or not text_result.get("message_id"):
                if media_result:
                    return {
                        **media_result,
                        "partial_success": True,
                        "caption_error": text_result.get("error", "Instagram did not confirm the caption") if isinstance(text_result, dict) else "Invalid response",
                    }
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to send Instagram message: {text_result.get('error', 'Instagram did not confirm the message') if isinstance(text_result, dict) else 'Invalid response'}"
                )
            if media_result:
                return {**media_result, "caption": text_result}
            return text_result
        return media_result or res
    
    elif platform == "internal":
        participants = []
        if context and "participants" in context:
            participants = context["participants"]
            
        if not conversation_id or conversation_id == "new":
            if not participants or len(participants) < 2:
                raise HTTPException(status_code=400, detail="Participants required for internal chat")
            
            sorted_ids = sorted(participants)
            conversation_id = f"team_{sorted_ids[0]}_{sorted_ids[1]}"
        
        if timestamp:
            try:
                now = parser.parse(timestamp)
                if now.tzinfo is None:
                    now = now.replace(tzinfo=timezone.utc)
                else:
                    now = now.astimezone(timezone.utc)
            except Exception:
                now = datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)

        # Team conversations and messages live in their own dedicated collections
        convo_collection = db[f"team_conversations_{org_id}"]
        
        convo = convo_collection.find_one({"conversation_id": conversation_id})
        
        if not convo:
            if not participants:
                # Reconstruct participants correctly from team_userA_userB
                import re
                # Find all occurrences of 'user_' followed by alphanumeric characters
                participants = re.findall(r"user_[A-Za-z0-9]+", conversation_id)
                
                if not participants and len(conversation_id.split("_")) >= 3:
                    # Fallback for IDs that might not follow the 'user_' prefix pattern
                    participants = conversation_id.split("_")[1:]
            
            convo_doc = {
                "conversation_id": conversation_id,
                "type": "team",
                "participants": participants,
                "created_at": now,
                "updated_at": now,
                "last_message": content,
                "last_message_timestamp": now,
                "last_sender": context.get("sender_id") if context else None,
                "unread": {p: 0 for p in participants if p}
            }
            convo_collection.insert_one(convo_doc)
            convo = convo_doc
        
        # Ensure participants are populated for broadcasting
        if not participants:
            participants = convo.get("participants", [])
        
        # Self-healing: if participants list is broken (e.g. contains 'user' as a separate element)
        if participants and ("user" in participants):
            logger.info(f"Repairing broken participants list for {conversation_id}")
            import re
            reconstructed = re.findall(r"user_[A-Za-z0-9]+", conversation_id)
            
            if reconstructed:
                participants = reconstructed
                convo_collection.update_one(
                    {"conversation_id": conversation_id},
                    {
                        "$set": {
                            "participants": participants,
                            "unread": {p: 0 for p in participants if p}
                        }
                    }
                )
        
        messages_collection = db[f"team_messages_{org_id}"]
        sender_id = context.get("sender_id") if context else None
        sender_name = context.get("sender_name") if context else None
        
        msg_id = str(uuid.uuid4())
        msg_doc = {
            "id": msg_id,
            "message_id": msg_id,
            "temp_id": temp_id,
            "conversation_id": conversation_id,
            "platform": "internal",
            "type": "text",
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "role": "agent",
            "timestamp": now,
            "status": "sent"
        }
        messages_collection.insert_one(msg_doc)
        
        recipient_id = None
        if "participants" in convo:
            for p in convo["participants"]:
                if p != sender_id:
                    recipient_id = p
                    break
        
        update_query = {
            "$set": {
                "last_message": content,
                "last_message_timestamp": now,
                "last_sender": sender_id,
                "updated_at": now
            }
        }
        
        if recipient_id:
            update_query["$inc"] = {f"unread.{recipient_id}": 1}
            
        convo_collection.update_one(
            {"conversation_id": conversation_id},
            update_query
        )

        from services.utils import serialize_mongo
        broadcast_payload = serialize_mongo(msg_doc)
        await broadcast_team_message(org_id, conversation_id, participants, broadcast_payload)
        
        return {"status": "sent", "conversation_id": conversation_id, "message_id": msg_id}
    else:
        raise HTTPException(status_code=400, detail="Unsupported platform")
