""""
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

from database import get_mongo_db
from services.utils import serialize_mongo
from loguru import logger

db = get_mongo_db()

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

        doc = {
            **payload,                     # all flat WaBot fields from frontend
            "flow_id":    flow_id,
            "org_id":     org_id,
            "name":       payload.get("name", name),
            "flow_type":  "wabot",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
            "updated_by": created_by,
            "published_at": None if payload.get("status") != "active" else now,
            "trigger_count": 0,
            "last_run":   None,
        }

        col.insert_one(doc)
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

        flows = list(col.find(query).sort("created_at", -1))
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
        doc = col.find_one({"flow_id": flow_id, "org_id": org_id})
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
        if payload.get("status") == "active":
            set_fields["published_at"] = now

        result = col.find_one_and_update(
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
        result = col.delete_one({"flow_id": flow_id, "org_id": org_id})
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

    All three fields are already present in the flat document from the first
    save, so no promotion step is needed — just flip the status.
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        result = col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {
                "status":       "active",
                "published_at": now,
                "updated_at":   now,
                "updated_by":   updated_by,
            }},
            return_document=True,
        )

        if not result:
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

        result = col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {
                "status":     "draft",
                "updated_at": now,
                "updated_by": updated_by,
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