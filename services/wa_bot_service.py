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
                # Ensure only 1 active flow per organization
                await col.update_many(
                    {"org_id": org_id, "flow_id": {"$ne": flow_id}},
                    {"$set": {
                        "status": "draft",
                        "published_at": None,
                        "updated_at": now,
                    }}
                )
            else:
                set_fields["published_at"] = None
                
        result = await col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": set_fields},
            return_document=True,
        )

        if not result:
            return None

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
            logger.info(f"Deleted wa_bot_flow {flow_id} for org {org_id}")
        return deleted

    except Exception as e:
        logger.error(f"delete_wa_bot_flow error: {e}")
        raise


# ─── PUBLISH ──────────────────────────────────────────────────────────────────

async def publish_wa_bot_flow(
    org_id: str,
    flow_id: str,
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

        # Ensure only 1 active flow per organization by setting all others to draft
        await col.update_many(
            {"org_id": org_id, "flow_id": {"$ne": flow_id}},
            {"$set": {
                "status": "draft",
                "published_at": None,
                "updated_at": now,
            }}
        )

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
            return None

        logger.info(f"Published wa_bot_flow {flow_id} for org {org_id} and unpublished all others.")
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
            "last_run",
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