import uuid
from typing import Optional, Dict, List
from datetime import datetime, timezone
from pymongo import ReturnDocument

# Internal Modules
from database import get_mongo_db
from services.utils import serialize_mongo
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()


def _automation_flows_collection(org_id: str):
    return db[f"automation_flows_{org_id}"]


def _automation_triggers_collection():
    return db["automation_triggers"]


# =============================================================================
# CREATE
# =============================================================================

async def create_automation_flow(
    org_id: str,
    name: str,
    description: Optional[str] = None,
    flow_data: Optional[Dict] = None,
    created_by: Optional[str] = None
) -> Dict:
    try:
        collection = _automation_flows_collection(org_id)
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        flow_doc = {
            "flow_id": flow_id,
            "org_id": org_id,
            "name": name,
            "description": description,
            "status": "draft",
            "flow_data": flow_data or {"nodes": {}, "connections": [], "triggers": []},
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
            "updated_by": created_by,
            "published_at": None,
            "execution_count": 0,
            "last_executed_at": None,
        }

        collection.insert_one(flow_doc)
        logger.info(f"Created automation flow: {flow_id} for org: {org_id}")
        return serialize_mongo(flow_doc)

    except Exception as e:
        logger.error(f"Error creating automation flow: {str(e)}")
        raise


# =============================================================================
# LIST
# =============================================================================

async def list_automation_flows(org_id: str, status: Optional[str] = None) -> List[Dict]:
    try:
        collection = _automation_flows_collection(org_id)
        query = {"org_id": org_id}
        if status:
            query["status"] = status
        flows = list(collection.find(query).sort("created_at", -1))
        logger.info(f"Listed {len(flows)} automation flows for org: {org_id}")
        return [serialize_mongo(flow) for flow in flows]
    except Exception as e:
        logger.error(f"Error listing automation flows: {str(e)}")
        raise


# =============================================================================
# GET
# =============================================================================

async def get_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    try:
        collection = _automation_flows_collection(org_id)
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if flow:
            return serialize_mongo(flow)
        return None
    except Exception as e:
        logger.error(f"Error getting automation flow: {str(e)}")
        raise


# =============================================================================
# UPDATE
# =============================================================================

async def update_automation_flow(
    org_id: str,
    flow_id: str,
    update_data: Dict,
    updated_by: Optional[str] = None
) -> Optional[Dict]:
    try:
        collection = _automation_flows_collection(org_id)

        update_doc = {
            "updated_at": datetime.now(timezone.utc),
            "updated_by": updated_by,
        }

        if "name" in update_data:
            update_doc["name"] = update_data["name"]
        if "description" in update_data:
            update_doc["description"] = update_data["description"]
        if "flow_data" in update_data:
            update_doc["flow_data"] = update_data["flow_data"]
            collection.update_one(
                {"flow_id": flow_id, "org_id": org_id},
                {"$inc": {"version": 1}}
            )
        if "status" in update_data:
            update_doc["status"] = update_data["status"]

        flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER,
        )

        if flow:
            return serialize_mongo(flow)
        return None

    except Exception as e:
        logger.error(f"Error updating automation flow: {str(e)}")
        raise


# =============================================================================
# DELETE
# =============================================================================

async def delete_automation_flow(org_id: str, flow_id: str) -> bool:
    try:
        collection = _automation_flows_collection(org_id)
        result = collection.delete_one({"flow_id": flow_id, "org_id": org_id})
        if result.deleted_count > 0:
            logger.info(f"Deleted automation flow: {flow_id} for org: {org_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Error deleting automation flow: {str(e)}")
        raise


# =============================================================================
# PUBLISH
# =============================================================================

async def publish_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Publish (activate) an automation flow.

    whatsapp_followup triggers  → schedule a timed job via automation_scheduler
    All other trigger types     → register in automation_triggers collection (Instagram etc.)
    """
    try:
        from services.automation_scheduler import schedule_followup, cancel_flow_followups

        collection = _automation_flows_collection(org_id)
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return None

        flow_data = flow.get("flow_data", {})
        triggers = flow_data.get("triggers", [])
        if not triggers:
            raise ValueError("Flow must have at least one trigger")

        nodes = flow_data.get("nodes", {})
        connections = flow_data.get("connections", [])

        # Cancel any old pending follow-up jobs before creating new ones (re-publish safety)
        cancel_flow_followups(org_id, flow_id, reason="republished")

        # ------------------------------------------------------------------
        # Process each trigger
        # ------------------------------------------------------------------
        for idx, trigger_config in enumerate(triggers):
            trigger_type = trigger_config.get("type")
            config = trigger_config.get("config", {})
            start_node_id = trigger_config.get("start_node_id")

            if not start_node_id:
                logger.warning(f"Trigger {idx} missing start_node_id — skipping.")
                continue

            # ---- WhatsApp Follow-Up: schedule a timed job -----------------
            if trigger_type == "whatsapp_followup":
                conversation_id = config.get("conversation_id")
                if not conversation_id:
                    logger.warning(f"whatsapp_followup trigger missing conversation_id — skipping.")
                    continue

                delay_seconds, message_text = _extract_delay_and_message(
                    start_node_id, nodes, connections
                )

                if delay_seconds is None or message_text is None:
                    logger.warning(
                        f"Could not extract delay/message from flow {flow_id}. "
                        f"Ensure a smart_delay node and a message node are connected."
                    )
                    continue

                schedule_followup(
                    org_id=org_id,
                    flow_id=flow_id,
                    conversation_id=conversation_id,
                    delay_seconds=delay_seconds,
                    message_config={"text": message_text},
                )
                logger.success(
                    f"Scheduled WhatsApp follow-up — flow={flow_id}, "
                    f"conversation={conversation_id}, delay={delay_seconds}s"
                )

            # ---- All other triggers: register in automation_triggers ------
            else:
                await _register_single_trigger(
                    org_id, flow_id, idx, trigger_type, config, start_node_id
                )

        # Update flow status
        now = datetime.now(timezone.utc)
        updated_flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {"status": "published", "published_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )

        if updated_flow:
            return serialize_mongo(updated_flow)
        return None

    except Exception as e:
        logger.error(f"Error publishing automation flow: {str(e)}")
        raise


# =============================================================================
# UNPUBLISH
# =============================================================================

async def unpublish_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Unpublish (deactivate) an automation flow.
    Cancels pending WhatsApp follow-up jobs and deactivates trigger registrations.
    """
    try:
        from services.automation_scheduler import cancel_flow_followups

        collection = _automation_flows_collection(org_id)
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return None

        # Cancel pending follow-up jobs
        cancel_flow_followups(org_id, flow_id, reason="flow_unpublished")

        # Deactivate registered triggers (instagram / tag-based triggers)
        await _deactivate_flow_triggers(org_id, flow_id)

        now = datetime.now(timezone.utc)
        updated_flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {"status": "draft", "unpublished_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )

        if updated_flow:
            logger.info(f"Unpublished automation flow: {flow_id}")
            return serialize_mongo(updated_flow)
        return None

    except Exception as e:
        logger.error(f"Error unpublishing automation flow: {str(e)}")
        raise


async def _deactivate_flow_triggers(org_id: str, flow_id: str):
    """Deactivate trigger registrations for a flow."""
    triggers_collection = _automation_triggers_collection()
    result = triggers_collection.update_many(
        {"flow_id": flow_id, "org_id": org_id},
        {"$set": {"status": "inactive", "deactivated_at": datetime.now(timezone.utc)}},
    )
    logger.info(f"Deactivated {result.modified_count} triggers for flow {flow_id}")


# =============================================================================
# EXECUTION COUNT
# =============================================================================

async def increment_execution_count(org_id: str, flow_id: str):
    try:
        collection = _automation_flows_collection(org_id)
        collection.update_one(
            {"flow_id": flow_id, "org_id": org_id},
            {
                "$inc": {"execution_count": 1},
                "$set": {"last_executed_at": datetime.now(timezone.utc)},
            },
        )
    except Exception as e:
        logger.error(f"Error incrementing execution count: {str(e)}")


# =============================================================================
# INTERNAL HELPERS
# =============================================================================

def _children(node_id: str, connections: list) -> list:
    return [c["target"] for c in connections if c.get("source") == node_id]


def _extract_delay_and_message(
    start_node_id: str,
    nodes: dict,
    connections: list,
) -> tuple:
    """
    Walk graph: start_node (smart_delay) → message_node
    Returns (delay_seconds, message_text) or (None, None) on failure.

    """
    UNIT_TO_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}

    delay_seconds = None
    message_text = None

    # FIX BUG #1: start_node_id IS the smart_delay — read it directly,
    # don't look for a smart_delay among its children.
    start_node = nodes.get(start_node_id, {})

    if start_node.get("type") == "smart_delay":
        config = start_node.get("config", {})
        amount = config.get("amount", 0)
        unit = config.get("unit", "seconds")
        delay_seconds = int(amount) * UNIT_TO_SECONDS.get(unit, 1)

        # Now walk one level down to find the message node
        for msg_id in _children(start_node_id, connections):
            msg_node = nodes.get(msg_id, {})
            if msg_node.get("type") == "message":
                # FIX BUG #2: text lives at node["content"]["text"], not node["config"]["content"]["text"]
                message_text = msg_node.get("content", {}).get("text", "")
                break

    return delay_seconds, message_text


async def _register_single_trigger(
    org_id: str,
    flow_id: str,
    idx: int,
    trigger_type: str,
    config: dict,
    start_node_id: str,
):
    """Register a single non-followup trigger in automation_triggers."""
    triggers_collection = _automation_triggers_collection()

    platform = "instagram" if "instagram" in trigger_type else "whatsapp"

    keyword_value = config.get("keyword", "")
    if isinstance(keyword_value, list):
        keyword_value = "|".join(keyword_value)

    filters = {
        "keyword": keyword_value,
        "post_id": config.get("post_id"),
        "story_id": config.get("story_id"),
        "tag": config.get("tag"),
    }

    trigger_doc = {
        "trigger_id": f"trigger_{flow_id}_{idx}",
        "flow_id": flow_id,
        "org_id": org_id,
        "platform": platform,
        "trigger_type": trigger_type,
        "filters": filters,
        "start_node_id": start_node_id,
        "status": "active",
        "registered_at": datetime.now(timezone.utc),
        "last_triggered_at": None,
    }

    triggers_collection.update_one(
        {"trigger_id": trigger_doc["trigger_id"]},
        {"$set": trigger_doc},
        upsert=True,
    )
    logger.info(f"Registered trigger: {trigger_type} for flow {flow_id}")


async def _register_flow_triggers(org_id: str, flow_id: str, flow_data: Dict):
    """
    Register all non-followup triggers. Kept for backward compatibility.
    publish_automation_flow() calls _register_single_trigger directly now.
    """
    for idx, trigger_config in enumerate(flow_data.get("triggers", [])):
        trigger_type = trigger_config.get("type")
        if trigger_type == "whatsapp_followup":
            continue
        config = trigger_config.get("config", {})
        start_node_id = trigger_config.get("start_node_id")
        if not start_node_id:
            continue
        await _register_single_trigger(org_id, flow_id, idx, trigger_type, config, start_node_id)


# =============================================================================
# FLOW VALIDATION
# =============================================================================

def validate_flow_structure(flow_data: Dict) -> tuple:
    try:
        if not isinstance(flow_data, dict):
            return False, "Flow data must be a dictionary"
        if "nodes" not in flow_data:
            return False, "Flow data must contain 'nodes'"
        if "connections" not in flow_data:
            return False, "Flow data must contain 'connections'"
        if "triggers" not in flow_data:
            return False, "Flow data must contain 'triggers'"

        nodes = flow_data.get("nodes", {})
        if not isinstance(nodes, dict):
            return False, "'nodes' must be a dictionary"

        connections = flow_data.get("connections", [])
        if not isinstance(connections, list):
            return False, "'connections' must be a list"

        for conn in connections:
            if "source" not in conn or "target" not in conn:
                return False, "Each connection must have 'source' and 'target'"
            if conn["source"] not in nodes:
                return False, f"Connection source '{conn['source']}' not found in nodes"
            if conn["target"] not in nodes:
                return False, f"Connection target '{conn['target']}' not found in nodes"

        triggers = flow_data.get("triggers", [])
        if not isinstance(triggers, list):
            return False, "'triggers' must be a list"

        for trigger in triggers:
            if "start_node_id" not in trigger:
                return False, "Each trigger must have 'start_node_id'"
            if trigger["start_node_id"] not in nodes:
                return False, f"Trigger start_node_id '{trigger['start_node_id']}' not found in nodes"

        return True, None

    except Exception as e:
        return False, f"Validation error: {str(e)}"