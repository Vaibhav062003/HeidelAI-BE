"""
  # ── Webhook-runtime fields (read by webhook_routes.py and execute_whatsapp_flow) ──
  # The webhook does:
  #   flow_collection.find_one({
  #       "trigger_from":  "webhook",
  #       "trigger_event": "message_received",
  #       "status":        "active",
  #   })
  # execute_whatsapp_flow then reads:
  #   flow["trigger_messages"], flow["welcome_message"], flow["nodes"]
  "trigger_from":     "webhook",
  "trigger_event":    "message_received",
  "trigger_messages": str,           # "hi,hey,hello,menu"
  "welcome_message":  dict,          # {type: "interactive", interactive: {...}}
  "nodes": {                         # keyed by node-id
    "node_id": {
      "type":        "button" | "list" | "cta_url",
      "body_text":   str,
      "header":      dict | None,
      "buttons":     list | None,    # for type "button"
      "button_text": str  | None,    # for type "list" / "cta_url"
      "sections":    list | None,    # for type "list"
      "url":         str  | None,    # for type "cta_url"
    }
"""

import uuid
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from database import get_async_mongo_db, get_async_mongo_client
from fastapi import HTTPException
from pydantic import TypeAdapter
from schemas.models import WhatsAppInteractiveMessage
from services.automation_state import WA_DRAFT_FIELDS, public_flow, version_query, draft_fields
from services.utils import serialize_mongo
from loguru import logger

db = get_async_mongo_db()
interactive_adapter = TypeAdapter(WhatsAppInteractiveMessage)

# ─── Internal helper ──────────────────────────────────────────────────────────

def _col(org_id: str):
    """Per-org WaBot flows collection — separate from automation_flows_{org_id}."""
    return db[f"wa_bot_flows_{org_id}"]


def _normalized_phone_ids(phone_ids: List[str]) -> List[str]:
    return list(dict.fromkeys(
        value for value in (str(phone_id or "").strip() for phone_id in phone_ids) if value
    ))


async def _validate_publish_phone_ids(org_id: str, phone_ids: List[str], session=None) -> List[str]:
    normalized = _normalized_phone_ids(phone_ids)
    if not normalized:
        connections = await db.whatsapp_connections.find(
            {"org_id": org_id, "is_active": {"$ne": False}},
            {"phone_id": 1, "slot": 1, "_id": 0},
            session=session,
        ).to_list(length=2)
        if len(connections) != 1 or not connections[0].get("phone_id"):
            raise ValueError("Select at least one active WhatsApp phone number")
        normalized = [str(connections[0]["phone_id"])]

    connections = await db.whatsapp_connections.find(
        {
            "org_id": org_id,
            "phone_id": {"$in": normalized},
            "is_active": {"$ne": False},
        },
        {"phone_id": 1, "_id": 0},
        session=session,
    ).to_list(length=len(normalized))
    found = {str(connection.get("phone_id") or "") for connection in connections}
    missing = [phone_id for phone_id in normalized if phone_id not in found]
    if missing:
        raise ValueError(f"Unknown or inactive WhatsApp phone IDs: {', '.join(missing)}")
    return normalized


async def _clear_flow_bindings(org_id: str, flow_id: str, session=None) -> None:
    await db.whatsapp_connections.update_many(
        {"org_id": org_id, "wa_bot_flow_id": flow_id},
        {"$unset": {"wa_bot_flow_id": ""}},
        session=session,
    )


async def _bind_flow_to_phone_ids(org_id: str, flow_id: str, phone_ids: List[str], session=None) -> None:
    previous = await db.whatsapp_connections.find(
        {"org_id": org_id, "phone_id": {"$in": phone_ids}, "wa_bot_flow_id": {"$ne": flow_id}},
        {"wa_bot_flow_id": 1, "_id": 0},
        session=session,
    ).to_list(length=len(phone_ids))
    replaced_flow_ids = {
        str(item.get("wa_bot_flow_id")) for item in previous if item.get("wa_bot_flow_id")
    }
    await _clear_flow_bindings(org_id, flow_id, session=session)
    await db.whatsapp_connections.update_many(
        {"org_id": org_id, "phone_id": {"$in": phone_ids}, "is_active": {"$ne": False}},
        {"$set": {"wa_bot_flow_id": flow_id}},
        session=session,
    )
    now = datetime.now(timezone.utc)
    for replaced_flow_id in replaced_flow_ids:
        remaining = await db.whatsapp_connections.count_documents({
            "org_id": org_id, "wa_bot_flow_id": replaced_flow_id, "is_active": {"$ne": False},
        }, session=session)
        if remaining == 0:
            await _col(org_id).update_one(
                {"flow_id": replaced_flow_id, "org_id": org_id},
                {"$set": {"status": "draft", "published_at": None, "updated_at": now}},
                session=session,
            )

# ─── CREATE ───────────────────────────────────────────────────────────────────

async def create_wa_bot_flow(org_id: str, name: str, payload: Dict[str, Any], created_by: Optional[str] = None) -> Dict:
    now = datetime.now(timezone.utc)
    doc = {key: value for key, value in payload.items() if key in WA_DRAFT_FIELDS}
    doc.update(flow_id=str(uuid.uuid4()), org_id=org_id, name=payload.get("name", name),
               status="draft", flow_type="wabot", trigger_from="webhook", trigger_event="message_received",
               version=1, created_at=now, updated_at=now, created_by=created_by, updated_by=created_by,
               published_at=None, trigger_count=0, last_run=None)
    await _col(org_id).insert_one(doc)
    return serialize_mongo(public_flow(doc))


# ─── LIST ─────────────────────────────────────────────────────────────────────

async def list_wa_bot_flows(
    org_id: str,
    status: Optional[str] = None,
) -> List[Dict]:
    """
    Return all WaBot flows for the org, newest first.
    """
    try:
        col = _col(org_id)
        query: Dict[str, Any] = {"org_id": org_id}
        if status:
            query["status"] = status

        flows = await col.find(query).sort("created_at", -1).to_list(length=None)
        logger.info(f"Listed {len(flows)} wa_bot_flows for org {org_id}")
        return [serialize_mongo(public_flow(f)) for f in flows]

    except Exception as e:
        logger.error(f"list_wa_bot_flows error: {e}")
        raise


# ─── GET ONE ──────────────────────────────────────────────────────────────────

async def get_wa_bot_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Fetch a single WaBot flow by flow_id, scoped to org_id.

    WaBotEditor's load useEffect reads:
        const raw  = res.data?.flow || res.data
        const data = raw?.flow_data || raw   ← falls back to raw (our flat doc)

    Then it reads:
        data.trigger_messages
        data.welcome_message
        data.nodes
        data.ui_config.nodes   ← canvas positions
    """
    try:
        col = _col(org_id)
        doc = await col.find_one({"flow_id": flow_id, "org_id": org_id})
        if not doc:
            return None
        return serialize_mongo(public_flow(doc))

    except Exception as e:
        logger.error(f"get_wa_bot_flow error: {e}")
        raise


# ─── UPDATE (PATCH) ───────────────────────────────────────────────────────────

async def update_wa_bot_flow(org_id: str, flow_id: str, payload: Dict[str, Any], updated_by: Optional[str] = None) -> Optional[Dict]:
    col = _col(org_id)
    flow = await col.find_one({"flow_id": flow_id, "org_id": org_id})
    if not flow:
        return None
    query = version_query(flow, payload.get("expected_draft_version"))
    fields = draft_fields(flow, payload, WA_DRAFT_FIELDS)
    fields.update(updated_at=datetime.now(timezone.utc), updated_by=updated_by,
                  trigger_from="webhook", trigger_event="message_received")
    result = await col.find_one_and_update(query, {"$set": fields}, return_document=True)
    if not result:
        raise HTTPException(409, "This workflow changed. Reload before saving.")
    return serialize_mongo(public_flow(result))


# ─── DELETE ───────────────────────────────────────────────────────────────────

async def delete_wa_bot_flow(org_id: str, flow_id: str) -> bool:
    try:
        col = _col(org_id)
        result = await col.delete_one({"flow_id": flow_id, "org_id": org_id})
        deleted = result.deleted_count > 0
        if deleted:
            await _clear_flow_bindings(org_id, flow_id)
            logger.info(f"Deleted wa_bot_flow {flow_id} for org {org_id}")
        return deleted

    except Exception as e:
        logger.error(f"delete_wa_bot_flow error: {e}")
        raise


# ─── PUBLISH ──────────────────────────────────────────────────────────────────

def _validate_wa_bot_draft(flow):
    nodes = flow.get("nodes")
    if not str(flow.get("trigger_messages") or "").strip() or not isinstance(nodes, dict):
        raise ValueError("Complete the WhatsApp trigger and message steps before publishing.")
    start = flow.get("start_node_id")
    if start and start not in nodes:
        raise ValueError("The WhatsApp start step is missing.")
    if not start and not flow.get("welcome_message"):
        raise ValueError("Connect the WhatsApp trigger to a message.")
    messages = list(nodes.values())
    for key in ("welcome_message", "introduction_message"):
        message = flow.get(key)
        if message:
            messages.append(message.get("interactive", {}) if message.get("type") == "interactive" else message)
    for message in messages:
        if message.get("type") == "text":
            if not str(message.get("content") or "").strip():
                raise ValueError("Text steps require a message.")
            continue
        interactive_adapter.validate_python(message)
        if not str(message.get("body_text") or "").strip():
            raise ValueError("Interactive steps require a message.")
        routes = [button["reply"]["id"] for button in message.get("buttons", [])]
        routes += [row["id"] for section in message.get("sections", []) for row in section.get("rows", [])]
        if message.get("type") in {"button", "list"} and not routes:
            raise ValueError("Interactive steps require buttons or list rows.")
        if any(route not in nodes for route in routes):
            raise ValueError("Connect every WhatsApp button and list row to a step.")


async def publish_wa_bot_flow(org_id: str, flow_id: str, phone_ids: List[str], updated_by: Optional[str] = None, expected_draft_version: Optional[int] = None) -> Optional[Dict]:
    async def publish(session):
        flow = await _col(org_id).find_one({"flow_id": flow_id, "org_id": org_id}, session=session)
        if not flow:
            return None
        query = version_query(flow, expected_draft_version)
        _validate_wa_bot_draft(flow)
        normalized = await _validate_publish_phone_ids(org_id, phone_ids, session=session)
        # Snapshot the current draft, rather than a previous published snapshot.
        snapshot = {key: flow[key] for key in WA_DRAFT_FIELDS if key in flow}
        now = datetime.now(timezone.utc)
        result = await _col(org_id).find_one_and_update(query, {"$set": {
            "status": "active", "trigger_from": "webhook", "trigger_event": "message_received",
            "published_at": now, "updated_at": now, "updated_by": updated_by,
            "published_snapshot": snapshot, "published_version": flow.get("version", 1),
        }}, return_document=True, session=session)
        if not result:
            raise HTTPException(409, "This workflow changed. Reload before publishing.")
        await _bind_flow_to_phone_ids(org_id, flow_id, normalized, session=session)
        return result
    async with await get_async_mongo_client().start_session() as session:
        result = await session.with_transaction(publish)
    return serialize_mongo(public_flow(result)) if result else None


# ─── UNPUBLISH ────────────────────────────────────────────────────────────────

async def unpublish_wa_bot_flow(org_id: str, flow_id: str, updated_by: Optional[str] = None) -> Optional[Dict]:
    async def unpublish(session):
        result = await _col(org_id).find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {"status": "draft", "published_at": None, "updated_at": datetime.now(timezone.utc), "updated_by": updated_by}},
            return_document=True, session=session,
        )
        if result:
            await _clear_flow_bindings(org_id, flow_id, session=session)
        return result
    async with await get_async_mongo_client().start_session() as session:
        result = await session.with_transaction(unpublish)
    return serialize_mongo(public_flow(result)) if result else None


# ─── DUPLICATE ────────────────────────────────────────────────────────────────

async def duplicate_wa_bot_flow(
    org_id: str,
    flow_id: str,
    created_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Create a deep copy of an existing WaBot flow with a fresh identity.

    Copies all content fields (trigger_messages, welcome_message, nodes,
    introduction_message, start_node_id, ui_config, etc.) and ensures
    the webhook-critical fields (trigger_from, trigger_event) are present.
    The copy is always created in 'draft' status.
    """
    try:
        col = _col(org_id)
        source = await col.find_one({"flow_id": flow_id, "org_id": org_id})
        if not source:
            return None

        new_flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Strip identity and metadata fields — keep all content fields
        skip_keys = {
            "_id", "flow_id", "org_id", "created_at", "updated_at",
            "created_by", "updated_by", "published_at", "trigger_count",
            "last_run", "phone_ids", "wa_bot_flow_id", "version", "draft_version", "published_snapshot", "published_version",
        }
        content = {k: v for k, v in source.items() if k not in skip_keys}

        doc = {
            **content,
            "flow_id":       new_flow_id,
            "org_id":        org_id,
            "name":          f"{source.get('name', 'Untitled')} (Copy)",
            "status":        "draft",
            "flow_type":     "wabot",
            "trigger_from":  source.get("trigger_from", "webhook"),
            "trigger_event": source.get("trigger_event", "message_received"),
            "created_at":    now,
            "updated_at":    now,
            "created_by":    created_by,
            "updated_by":    created_by,
            "published_at":  None,
            "trigger_count": 0,
            "last_run":      None,
            "version":       1,
        }

        await col.insert_one(doc)
        logger.info(f"Duplicated wa_bot_flow {flow_id} → {new_flow_id} for org {org_id}")
        return serialize_mongo(public_flow(doc))

    except Exception as e:
        logger.error(f"duplicate_wa_bot_flow error: {e}")
        raise
