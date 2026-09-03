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

from database import get_async_mongo_db
from services.utils import serialize_mongo
from loguru import logger

db = get_async_mongo_db()

# ─── Internal helper ──────────────────────────────────────────────────────────

def _col(org_id: str):
    """Per-org WaBot flows collection — separate from automation_flows_{org_id}."""
    return db[f"wa_bot_flows_{org_id}"]


def _normalized_phone_ids(phone_ids: List[str]) -> List[str]:
    return list(dict.fromkeys(
        value for value in (str(phone_id or "").strip() for phone_id in phone_ids) if value
    ))


async def _validate_publish_phone_ids(org_id: str, phone_ids: List[str]) -> List[str]:
    normalized = _normalized_phone_ids(phone_ids)
    if not normalized:
        connections = await db.whatsapp_connections.find(
            {"org_id": org_id, "is_active": {"$ne": False}},
            {"phone_id": 1, "slot": 1, "_id": 0},
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
    ).to_list(length=len(normalized))
    found = {str(connection.get("phone_id") or "") for connection in connections}
    missing = [phone_id for phone_id in normalized if phone_id not in found]
    if missing:
        raise ValueError(f"Unknown or inactive WhatsApp phone IDs: {', '.join(missing)}")
    return normalized


async def _clear_flow_bindings(org_id: str, flow_id: str) -> None:
    await db.whatsapp_connections.update_many(
        {"org_id": org_id, "wa_bot_flow_id": flow_id},
        {"$unset": {"wa_bot_flow_id": ""}},
    )


async def _bind_flow_to_phone_ids(org_id: str, flow_id: str, phone_ids: List[str]) -> None:
    previous = await db.whatsapp_connections.find(
        {"org_id": org_id, "phone_id": {"$in": phone_ids}, "wa_bot_flow_id": {"$ne": flow_id}},
        {"wa_bot_flow_id": 1, "_id": 0},
    ).to_list(length=len(phone_ids))
    replaced_flow_ids = {
        str(item.get("wa_bot_flow_id")) for item in previous if item.get("wa_bot_flow_id")
    }
    await _clear_flow_bindings(org_id, flow_id)
    await db.whatsapp_connections.update_many(
        {"org_id": org_id, "phone_id": {"$in": phone_ids}, "is_active": {"$ne": False}},
        {"$set": {"wa_bot_flow_id": flow_id}},
    )
    now = datetime.now(timezone.utc)
    for replaced_flow_id in replaced_flow_ids:
        remaining = await db.whatsapp_connections.count_documents({
            "org_id": org_id, "wa_bot_flow_id": replaced_flow_id, "is_active": {"$ne": False},
        })
        if remaining == 0:
            await _col(org_id).update_one(
                {"flow_id": replaced_flow_id, "org_id": org_id},
                {"$set": {"status": "draft", "published_at": None, "updated_at": now}},
            )

# ─── CREATE ───────────────────────────────────────────────────────────────────

async def create_wa_bot_flow(
    org_id: str,
    name: str,
    payload: Dict[str, Any],
    created_by: Optional[str] = None,
) -> Dict:
    try:
        col = _col(org_id)
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Strip any _id that might have leaked in
        payload.pop("_id", None)
        payload.pop("flow_id", None)

        raw_status = payload.get("status")
        status = raw_status if raw_status in { "draft", "active" } else "draft"

        doc = {
            **payload,                     # all flat WaBot fields from frontend
            "flow_id":    flow_id,
            "org_id":     org_id,
            "name":       payload.get("name", name),
            "status":     status,
            "flow_type":  "wabot",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
            "updated_by": created_by,
            "published_at": None if payload.get("status") != "active" else now,
            "trigger_count": 0,
            "last_run":   None,
        }

        await col.insert_one(doc)
        
        # Ensure only 1 active flow per organization if creating as active
        if status == "active":
            await col.update_many(
                {"org_id": org_id, "flow_id": {"$ne": flow_id}},
                {"$set": {
                    "status": "draft",
                    "published_at": None,
                    "updated_at": now,
                }}
            )
            
        logger.info(f"Created wa_bot_flow {flow_id} for org {org_id}")
        return serialize_mongo(doc)

    except Exception as e:
        logger.error(f"create_wa_bot_flow error: {e}")
        raise


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
        return [serialize_mongo(f) for f in flows]

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
        return serialize_mongo(doc)

    except Exception as e:
        logger.error(f"get_wa_bot_flow error: {e}")
        raise


# ─── UPDATE (PATCH) ───────────────────────────────────────────────────────────

async def update_wa_bot_flow(
    org_id: str,
    flow_id: str,
    payload: Dict[str, Any],
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Replace the mutable fields of an existing WaBot flow.

    `payload` is the raw body from WaBotEditor — contains all flat WaBot
    fields that may have changed. We merge them over the existing document.
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        # Never overwrite identity / immutable fields via PATCH
        for key in ("_id", "flow_id", "org_id", "created_at", "created_by"):
            payload.pop(key, None)

        set_fields = {
            **payload,
            "updated_at": now,
            "updated_by": updated_by,
            "flow_type":  "wabot",   # always keep constant
        }

        # If publishing, stamp published_at
        if "status" in payload:
            if payload["status"] == "active":
                set_fields["published_at"] = now
            else:
                set_fields["published_at"] = None
                
        result = await col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": set_fields},
            return_document=True,
        )

        if not result:
            return None

        # Ensure only 1 active flow per organization if activating
        if "status" in payload and payload["status"] == "active":
            await col.update_many(
                {"org_id": org_id, "flow_id": {"$ne": flow_id}},
                {"$set": {
                    "status": "draft",
                    "published_at": None,
                    "updated_at": now,
                }}
            )

        logger.info(f"Updated wa_bot_flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"update_wa_bot_flow error: {e}")
        raise


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

async def publish_wa_bot_flow(
    org_id: str,
    flow_id: str,
    phone_ids: List[str],
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Set status → "active" so the webhook picks it up.

    The webhook query is:
        db[f"wa_bot_flows_{org_id}"].find_one({
            "trigger_from":  "webhook",
            "trigger_event": "message_received",
            "status":        "active",
        })

    We defensively set trigger_from and trigger_event here so the webhook
    query always matches, even if those fields were somehow lost during
    an update or manual DB edit.
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        existing = await col.find_one({"flow_id": flow_id, "org_id": org_id})
        if not existing:
            return None
        normalized_phone_ids = await _validate_publish_phone_ids(org_id, phone_ids)
        await _bind_flow_to_phone_ids(org_id, flow_id, normalized_phone_ids)

        result = await col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {
                "status":        "active",
                "trigger_from":  "webhook",
                "trigger_event": "message_received",
                "published_at":  now,
                "updated_at":    now,
                "updated_by":    updated_by,
            }},
            return_document=True,
        )

        if not result:
            await _clear_flow_bindings(org_id, flow_id)
            return None

        logger.info(f"Published wa_bot_flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"publish_wa_bot_flow error: {e}")
        raise


# ─── UNPUBLISH ────────────────────────────────────────────────────────────────

async def unpublish_wa_bot_flow(
    org_id: str,
    flow_id: str,
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Revert to draft so the webhook no longer picks up this flow.
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        result = await col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {
                "status":       "draft",
                "published_at": None,
                "updated_at":   now,
                "updated_by":   updated_by,
            }},
            return_document=True,
        )

        if not result:
            return None

        await _clear_flow_bindings(org_id, flow_id)
        logger.info(f"Unpublished wa_bot_flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"unpublish_wa_bot_flow error: {e}")
        raise


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
            "last_run", "phone_ids", "wa_bot_flow_id",
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
        }

        await col.insert_one(doc)
        logger.info(f"Duplicated wa_bot_flow {flow_id} → {new_flow_id} for org {org_id}")
        return serialize_mongo(doc)

    except Exception as e:
        logger.error(f"duplicate_wa_bot_flow error: {e}")
        raise
