"""
automation_service.py
─────────────────────
Handles all database operations for automation flows.

DOCUMENT LAYOUT
───────────────
Each org's flows are stored in a per-org collection:
    automation_flows_{org_id}

A saved document looks like this (fields added by the service):

{
    # ── Identity ──────────────────────────────────────────────────────────────
    "flow_id":        str,          # UUID, stable primary key
    "org_id":         str,
    "name":           str,
    "description":    str | None,
    "status":         "draft" | "published" | "archived",
    "trigger_type":   str | None,   # e.g. "whatsapp_message_received"
    "version":        int,

    # ── The full canvas snapshot (stored as-is from the frontend) ─────────────
    "flow_data": {
        "trigger_event":    "message_received",
        "trigger_from":     "webhook",
        "trigger_messages": "hi,hey,hello,menu",
        "welcome_message":  {...},   # WA interactive message
        "nodes":            {...},   # keyed by node id
        "ui_config":        {"nodes": {"node_id": {"x": ..., "y": ...}}},
        ...
    },

    # ── Webhook-runtime fields (only present when status == "active") ─────────
    # These are PROMOTED from flow_data so the webhook handler can find
    # them with a single top-level query:
    #   flow_collection.find_one({"trigger_from": "webhook",
    #                             "trigger_event": "message_received",
    #                             "status": "active"})
    "trigger_from":     "webhook",
    "trigger_event":    "message_received",
    "trigger_messages": "hi,hey,hello,menu",
    "welcome_message":  {...},
    "nodes":            {...},

    # ── Timestamps / audit ────────────────────────────────────────────────────
    "created_at":       datetime,
    "updated_at":       datetime,
    "published_at":     datetime | None,
    "created_by":       str | None,
    "updated_by":       str | None,

    # ── Metrics ───────────────────────────────────────────────────────────────
    "execution_count":  int,
    "last_executed_at": datetime | None,
    "trigger_count":    int,
    "last_run":         datetime | None,
}
"""

import uuid
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from database import get_mongo_db
from services.utils import serialize_mongo
from loguru import logger

db = get_mongo_db()


# ─── Internal helpers ─────────────────────────────────────────────────────────

def _col(org_id: str):
    """Return the per-org automation-flows collection."""
    return db[f"automation_flows_{org_id}"]


def _extract_wabot_runtime_fields(flow_data: dict) -> dict:
    """
    Pull the fields that the webhook handler reads at runtime out of flow_data
    and return them as a flat dict ready to be $set at the document root.

    The webhook handler (webhook_routes.py) does:
        flow_collection.find_one({
            "trigger_from":  "webhook",
            "trigger_event": "message_received",
            "status":        "active",
        })

    And execute_whatsapp_flow() then reads:
        flow.get("trigger_messages")
        flow.get("welcome_message")
        flow.get("nodes", {})
    """
    if not flow_data or not isinstance(flow_data, dict):
        return {}

    return {
        "trigger_from":     flow_data.get("trigger_from",     "webhook"),
        "trigger_event":    flow_data.get("trigger_event",    "message_received"),
        "trigger_messages": flow_data.get("trigger_messages", ""),
        "welcome_message":  flow_data.get("welcome_message"),
        "nodes":            flow_data.get("nodes", {}),
    }


# ─── CREATE ───────────────────────────────────────────────────────────────────

async def create_automation_flow(
    org_id: str,
    name: str,
    description: Optional[str] = None,
    flow_data: Optional[Any] = None,
    trigger_type: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict:
    """
    Insert a new draft flow document.
    Returns the serialised document (no _id).
    """
    try:
        col = _col(org_id)
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        doc = {
            "flow_id":        flow_id,
            "org_id":         org_id,
            "name":           name,
            "description":    description,
            "status":         "draft",
            "trigger_type":   trigger_type,
            "flow_data":      flow_data or {},
            "version":        1,
            "created_at":     now,
            "updated_at":     now,
            "published_at":   None,
            "created_by":     created_by,
            "updated_by":     created_by,
            "execution_count": 0,
            "last_executed_at": None,
            "trigger_count":  0,
            "last_run":       None,
        }

        col.insert_one(doc)
        logger.info(f"Created automation flow {flow_id} for org {org_id}")
        return serialize_mongo(doc)

    except Exception as e:
        logger.error(f"create_automation_flow error: {e}")
        raise


# ─── LIST ─────────────────────────────────────────────────────────────────────

async def list_automation_flows(
    org_id: str,
    status: Optional[str] = None,
) -> List[Dict]:
    """
    Return all flows for the org, newest first.
    DashboardView expects a plain list — the route returns it unwrapped.
    """
    try:
        col = _col(org_id)
        query: Dict[str, Any] = {"org_id": org_id}
        if status:
            query["status"] = status

        flows = list(col.find(query).sort("created_at", -1))
        logger.info(f"Listed {len(flows)} flows for org {org_id}")
        return [serialize_mongo(f) for f in flows]

    except Exception as e:
        logger.error(f"list_automation_flows error: {e}")
        raise


# ─── GET ONE ──────────────────────────────────────────────────────────────────

async def get_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Fetch a single flow by flow_id, scoped to org_id.

    WaBotEditor calls GET /api/automation_flows/{flow_id} when opening an
    existing flow.  It reads:
        const raw  = res.data?.flow || res.data
        const data = raw?.flow_data || raw

    So we return the document as-is (serialised).  The editor then
    reconstructs nodes from data.welcome_message, data.nodes, data.ui_config.
    """
    try:
        col = _col(org_id)
        doc = col.find_one({"flow_id": flow_id, "org_id": org_id})
        if not doc:
            return None
        return serialize_mongo(doc)

    except Exception as e:
        logger.error(f"get_automation_flow error: {e}")
        raise


# ─── UPDATE (PATCH) ───────────────────────────────────────────────────────────

async def update_automation_flow(
    org_id: str,
    flow_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    flow_data: Optional[Any] = None,
    status: Optional[str] = None,
    trigger_type: Optional[str] = None,
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Partial update.  Only the supplied fields are changed.

    If status is explicitly set to "archived" we keep it as-is but do NOT
    promote runtime fields (archiving ≠ publishing).
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        set_fields: Dict[str, Any] = {"updated_at": now, "updated_by": updated_by}

        if name is not None:
            set_fields["name"] = name
        if description is not None:
            set_fields["description"] = description
        if flow_data is not None:
            set_fields["flow_data"] = flow_data
        if status is not None:
            set_fields["status"] = status
        if trigger_type is not None:
            set_fields["trigger_type"] = trigger_type

        result = col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": set_fields},
            return_document=True,
        )

        if not result:
            return None

        logger.info(f"Updated flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"update_automation_flow error: {e}")
        raise


# ─── DELETE ───────────────────────────────────────────────────────────────────

async def delete_automation_flow(org_id: str, flow_id: str) -> bool:
    """
    Permanently delete a flow document.
    Returns True if something was actually deleted.
    """
    try:
        col = _col(org_id)
        result = col.delete_one({"flow_id": flow_id, "org_id": org_id})
        deleted = result.deleted_count > 0

        if deleted:
            logger.info(f"Deleted flow {flow_id} for org {org_id}")
        else:
            logger.warning(f"delete_automation_flow: flow {flow_id} not found for org {org_id}")

        return deleted

    except Exception as e:
        logger.error(f"delete_automation_flow error: {e}")
        raise


# ─── PUBLISH ──────────────────────────────────────────────────────────────────

async def publish_automation_flow(
    org_id: str,
    flow_id: str,
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Publish a flow.

    Two things happen:
    1.  status is set to "active"  ← what the webhook query looks for
    2.  The WaBot runtime fields are PROMOTED from flow_data to the document
        root so the webhook handler finds them with a simple flat query.

    The frontend always saves the WaBot schema inside flow_data, so we read
    from there:
        flow_data.trigger_from
        flow_data.trigger_event
        flow_data.trigger_messages
        flow_data.welcome_message
        flow_data.nodes
    """
    try:
        col = _col(org_id)

        # Fetch the current document so we can read flow_data
        current = col.find_one({"flow_id": flow_id, "org_id": org_id})
        if not current:
            return None

        now = datetime.now(timezone.utc)
        flow_data = current.get("flow_data") or {}

        # Promote runtime fields to doc root
        runtime_fields = _extract_wabot_runtime_fields(flow_data)

        set_fields: Dict[str, Any] = {
            "status":       "active",   # <-- webhook query looks for "active"
            "published_at": now,
            "updated_at":   now,
            "updated_by":   updated_by,
            **runtime_fields,
        }

        result = col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": set_fields},
            return_document=True,
        )

        if not result:
            return None

        logger.info(f"Published flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"publish_automation_flow error: {e}")
        raise


# ─── UNPUBLISH ────────────────────────────────────────────────────────────────

async def unpublish_automation_flow(
    org_id: str,
    flow_id: str,
    updated_by: Optional[str] = None,
) -> Optional[Dict]:
    """
    Revert a published flow back to draft.

    We also REMOVE the top-level runtime fields so the webhook handler no
    longer picks up this flow (those fields are still safely in flow_data
    for when the user re-publishes).
    """
    try:
        col = _col(org_id)
        now = datetime.now(timezone.utc)

        result = col.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {
                "$set": {
                    "status":     "draft",
                    "updated_at": now,
                    "updated_by": updated_by,
                },
                # Remove the promoted runtime fields
                "$unset": {
                    "trigger_from":     "",
                    "trigger_event":    "",
                    "trigger_messages": "",
                    "welcome_message":  "",
                    "nodes":            "",
                },
            },
            return_document=True,
        )

        if not result:
            return None

        logger.info(f"Unpublished flow {flow_id} for org {org_id}")
        return serialize_mongo(result)

    except Exception as e:
        logger.error(f"unpublish_automation_flow error: {e}")
        raise


# ─── Flow validation (kept for backward compat with old route import) ─────────

def validate_flow_structure(flow_data: dict) -> tuple:
    """Basic structural validation — returns (is_valid, error_message)."""
    if not isinstance(flow_data, dict):
        return False, "flow_data must be a dict"
    return True, None


# ─── Execute WhatsApp flow (unchanged, kept here for import compatibility) ────

async def _send_text_message(org_id, whatsapp_business_id, conversation_id, raw_content, template_variables):
    from services.utils import inject_variables
    from services.wa_service import send_whatsapp_message
    personalized = inject_variables(raw_content, template_variables)
    await send_whatsapp_message(
        org_id=org_id,
        mode="reply",
        message={
            "sender_id": whatsapp_business_id,
            "conversation_id": conversation_id,
            "content": personalized,
        },
    )


async def _send_interactive_message(org_id, whatsapp_business_id, conversation_id, raw_data, template_variables, label):
    from pydantic import TypeAdapter
    from schemas.models import WhatsAppInteractiveMessage
    from services.utils import inject_variables
    from services.wa_service import send_interactive_whatsapp_message
    InteractiveMessageAdapter = TypeAdapter(WhatsAppInteractiveMessage)
    try:
        personalized = inject_variables(raw_data, template_variables)
        validated = InteractiveMessageAdapter.validate_python(personalized)
        await send_interactive_whatsapp_message(
            org_id=org_id,
            conversation_id=conversation_id,
            sender_id=whatsapp_business_id,
            interactive_message=validated,
        )
        return True
    except Exception as e:
        logger.error(f"[{org_id}] Failed to validate interactive data for '{label}': {e}")
        return False


def _complete_onboarding(org_id, conversation_id):
    db[f"conversations_{org_id}"].update_one(
        {"conversation_id": conversation_id},
        {"$set": {"onboarding_stage": "completed"}},
    )
    logger.info(f"[{org_id}] Onboarding completed for {conversation_id}")


async def execute_whatsapp_flow(
    flow: dict,
    org_id: str,
    conversation_id: str,
    stage: str,
    customer_name: str,
    trigger_event: str,
    trigger_messages: str,
    message_content: str,
    whatsapp_business_id: str,
    message_type: str,
):
    """
    Execute the next step in a WhatsApp automation flow.

    The flow document now has all relevant fields at the top level after
    publish_automation_flow() promotes them, so direct access works:
        flow["welcome_message"]
        flow["nodes"]
        flow["trigger_messages"]
    """
    from services.utils import get_first_name

    logger.info(
        f"[{org_id}] Executing WA flow for conversation {conversation_id} "
        f"| stage={stage} | type={message_type}"
    )

    welcome_message = flow.get("welcome_message")
    welcome_message_ids = (
        [btn["reply"]["id"]
         for btn in welcome_message.get("interactive", {}).get("buttons", [])]
        if welcome_message and welcome_message.get("type") == "interactive"
        else []
    )
    flow_nodes = flow.get("nodes", {})
    trigger_keywords = (
        [m.strip().lower() for m in trigger_messages.split(",")]
        if trigger_messages else []
    )
    template_variables = {"customer_name": get_first_name(customer_name)}
    ctx = dict(
        org_id=org_id,
        whatsapp_business_id=whatsapp_business_id,
        conversation_id=conversation_id,
    )

    # ── Interactive (button / list click) ────────────────────────────────────
    if message_type == "interactive":
        selection_id = message_content
        logger.info(f"[{org_id}] Interactive selection ID: '{selection_id}'")

        if selection_id not in flow_nodes:
            logger.warning(f"[{org_id}] Node '{selection_id}' not found → routing to AI")
            return False

        if selection_id in welcome_message_ids:
            logger.info(f"[{org_id}] Selection '{selection_id}' is a welcome-message button")

        node_data = flow_nodes[selection_id]
        node_type = node_data.get("type")
        _complete_onboarding(org_id, conversation_id)

        if node_type == "text":
            await _send_text_message(
                **ctx,
                raw_content=node_data.get("content", ""),
                template_variables=template_variables,
            )
            return True

        elif node_type in {"button", "list", "cta_url"}:
            ok = await _send_interactive_message(
                **ctx,
                raw_data=node_data,
                template_variables=template_variables,
                label=selection_id,
            )
            return ok

        else:
            logger.warning(f"[{org_id}] Unknown node_type '{node_type}' for '{selection_id}'")
            return False

    # ── Welcome / keyword trigger ────────────────────────────────────────────
    if welcome_message:
        is_new_session = stage == "welcome"
        is_keyword_match = (
            message_type == "text"
            and trigger_event == "message_received"
            and message_content.strip().lower() in trigger_keywords
        )

        if not (is_new_session or is_keyword_match):
            logger.info(f"[{org_id}] No trigger match → routing to AI")
            return False

        welcome_type = welcome_message.get("type")
        logger.info(f"[{org_id}] Trigger matched. Sending welcome (type={welcome_type})")

        if welcome_type == "text":
            await _send_text_message(
                **ctx,
                raw_content=welcome_message.get("content", ""),
                template_variables=template_variables,
            )
            _complete_onboarding(org_id, conversation_id)
            return True

        elif welcome_type == "interactive":
            ok = await _send_interactive_message(
                **ctx,
                raw_data=welcome_message.get("interactive", {}),
                template_variables=template_variables,
                label="welcome",
            )
            return ok

    logger.warning(f"[{org_id}] Flow fell through all scenarios → routing to AI")
    return False