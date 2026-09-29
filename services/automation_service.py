import uuid
import asyncio
from typing import Optional, Dict, List
from datetime import datetime, timezone
from pymongo import ReturnDocument
from pydantic import TypeAdapter

# Internal Modules
from database import get_mongo_db, get_async_mongo_db
from schemas.models import WhatsAppInteractiveMessage
from services.automation_keywords import message_contains_keyword
from services.utils import get_first_name, inject_variables, serialize_mongo
from services.wa_service import send_interactive_whatsapp_message, send_whatsapp_message
from core.services import services
from loguru import logger

db = get_mongo_db()
async_db = get_async_mongo_db()

InteractiveMessageAdapter = TypeAdapter(WhatsAppInteractiveMessage)

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
    """
    Delete an automation flow AND all related data:
      - All automation_triggers for this flow (prevents scheduler from firing)
      - All google_sheet_sent_numbers dedup records for this flow
      - Any scheduled follow-up jobs
      - The flow document itself
    """
    try:
        collection = _automation_flows_collection(org_id)
        
        # Check if flow exists before attempting cleanup
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return False

        # 1. Cancel any scheduled follow-up jobs
        try:
            from services.automation_scheduler import cancel_flow_followups
            cancel_flow_followups(org_id, flow_id, reason="flow_deleted")
        except Exception as e:
            logger.error(f"Failed to cancel follow-ups for flow {flow_id}: {e}")

        # 2. Delete all automation triggers for this flow
        try:
            triggers_result = _automation_triggers_collection().delete_many(
                {"flow_id": flow_id, "org_id": org_id}
            )
            if triggers_result.deleted_count > 0:
                logger.info(f"Cleaned up {triggers_result.deleted_count} trigger(s) for deleted flow {flow_id}")
        except Exception as e:
            logger.error(f"Failed to clean up triggers for flow {flow_id}: {e}")

        # 3. Delete all dedup (sent numbers) records for this flow
        try:
            dedup_result = db.google_sheet_sent_numbers.delete_many(
                {"flow_id": flow_id, "org_id": org_id}
            )
            if dedup_result.deleted_count > 0:
                logger.info(f"Cleaned up {dedup_result.deleted_count} dedup record(s) for deleted flow {flow_id}")
        except Exception as e:
            logger.error(f"Failed to clean up dedup records for flow {flow_id}: {e}")

        # 4. Finally delete the flow document
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

    google_sheet triggers      → register trigger, then immediately process all sheet rows
    whatsapp_followup triggers → schedule a timed job via automation_scheduler
    All other trigger types    → register in automation_triggers collection (Instagram etc.)
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

        # Deactivate registered triggers (instagram / tag-based / google_sheet triggers)
        await _deactivate_flow_triggers(org_id, flow_id)

        # ------------------------------------------------------------------
        # Process each trigger
        # ------------------------------------------------------------------
        deferred_tasks = []

        for idx, trigger_config in enumerate(triggers):
            trigger_type = trigger_config.get("type")
            config = trigger_config.get("config", {})
            start_node_id = trigger_config.get("start_node_id")

            if not start_node_id:
                logger.warning(f"Trigger {idx} missing start_node_id — skipping.")
                continue

            # ---- Google Sheet trigger: register --------------------------
            if trigger_type == "google_sheet":
                # Returns a dict of params for process_google_sheet_trigger if valid
                task_params = await _handle_google_sheet_trigger(
                    org_id=org_id,
                    flow_id=flow_id,
                    idx=idx,
                    config=config,
                    start_node_id=start_node_id,
                    nodes=nodes,
                    connections=connections,
                )
                if task_params:
                    deferred_tasks.append(task_params)

            # ---- WhatsApp Follow-Up: schedule a timed job -----------------
            elif trigger_type == "whatsapp_followup":
                conversation_id = config.get("conversation_id")
                if not conversation_id:
                    logger.warning(f"whatsapp_followup trigger missing conversation_id — skipping.")
                    continue

                delay_seconds, message_config = _extract_delay_and_message(
                    start_node_id, nodes, connections
                )

                if delay_seconds is None or message_config is None:
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
                    message_config=message_config,
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
            # Process existing sheet rows IMMEDIATELY after publish.
            # Strategy: sync sheet → process via the MongoDB path (same as scheduler).
            # This avoids a redundant direct Google API fetch and ensures consistency.
            # NOTE: Task references are stored in a global set to prevent garbage collection
            # from silently cancelling them (Python asyncio best practice).
            for params in deferred_tasks:
                sheet_id = params.get("trigger_config", {}).get("sheet_id")
                if sheet_id:
                    task = asyncio.create_task(
                        _safe_immediate_sheet_processing(org_id, flow_id, sheet_id)
                    )
                    services.background_tasks.add(task)
                    task.add_done_callback(services.background_tasks.discard)
                    logger.success(f"Started immediate sheet processing for google_sheet flow {flow_id}, sheet {sheet_id}")


            return serialize_mongo(updated_flow)
        return None

    except Exception as e:
        logger.error(f"Error publishing automation flow: {str(e)}")
        raise


async def _safe_immediate_sheet_processing(org_id: str, flow_id: str, sheet_id: str):
    """
    Immediately sync and process sheet rows on publish with a 3-phase speed optimization:
      1. PRE-SYNC: Instant send to already cached rows in MongoDB for the target flow.
      2. SYNC: Fast bulk-reconciliation with Google Sheets.
      3. POST-SYNC: Full check for all active flows to catch new data.
    """
    try:
        from services.google_sheet_automation import process_new_sheet_rows
        from api.google_sheets_routes import _sync_sheet

        # Phase 1: PRE-SYNC (Instant gratification)
        # Process whatever is already in MongoDB for THIS flow immediately.
        logger.info(f"[PublishSync] Phase 1/3: PRE-SYNC processing for flow {flow_id}...")
        await process_new_sheet_rows(org_id, sheet_id, target_flow_id=flow_id)

        # Phase 2: SYNC (Optimized reconciliation)
        # Ensure MongoDB has the latest data from Google Sheets.
        logger.info(f"[PublishSync] Phase 2/3: Syncing sheet {sheet_id}...")
        try:
            sync_result = await _sync_sheet(org_id, sheet_id)
            logger.info(
                f"[PublishSync] Synced sheet {sheet_id}: "
                f"+{sync_result.get('rows_added', 0)} ~{sync_result.get('rows_updated', 0)}"
            )
        except Exception as e:
            logger.warning(f"[PublishSync] Sheet sync failed, continuing with existing cache: {e}")

        # Phase 3: POST-SYNC (Completeness)
        # Process all rows again for all triggers (idempotency ensured by dedup collection).
        logger.info(f"[PublishSync] Phase 3/3: POST-SYNC full check for sheet {sheet_id}...")
        await process_new_sheet_rows(org_id, sheet_id)
        
        logger.success(f"[PublishSync] Completed optimized immediate processing for flow {flow_id}")

    except Exception as e:
        logger.error(f"[PublishSync] FAILED immediate processing for flow {flow_id}: {e}")


# =============================================================================
# UNPUBLISH
# =============================================================================

async def unpublish_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Unpublish (deactivate) an automation flow.
    Cancels pending WhatsApp follow-up jobs and deactivates trigger registrations.
    NOTE: For google_sheet triggers, sent records are NOT cleared so re-publish
    won't re-message the same numbers.
    """
    try:
        from services.automation_scheduler import cancel_flow_followups

        collection = _automation_flows_collection(org_id)
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return None

        # Cancel pending follow-up jobs
        cancel_flow_followups(org_id, flow_id, reason="flow_unpublished")

        # Deactivate registered triggers (instagram / tag-based / google_sheet triggers)
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
    Returns (delay_seconds, message_config: dict) or (None, None) on failure.
    """
    UNIT_TO_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600, "days": 86400}

    delay_seconds = None
    message_config = None

    start_node = nodes.get(start_node_id, {})

    if start_node.get("type") == "smart_delay":
        config = start_node.get("config", {})
        amount = config.get("amount", 0)
        unit = config.get("unit", "seconds")
        delay_seconds = int(amount) * UNIT_TO_SECONDS.get(unit, 1)

        for msg_id in _children(start_node_id, connections):
            msg_node = nodes.get(msg_id, {})
            if msg_node.get("type") == "message":
                # Extract full content config (handles both text and templates)
                content = msg_node.get("config", {}).get("content", {})
                if not content:
                    content = msg_node.get("content", {})
                
                if content:
                    message_config = content
                    break

    return delay_seconds, message_config


def _extract_message_from_start(
    start_node_id: str,
    nodes: dict,
    connections: list,
) -> Optional[dict]:
    """
    Walk from start_node_id to find the first message node.
    Used by _handle_google_sheet_trigger.

    Handles both node structures the frontend may produce:
      - node.config.content.text  (primary — used by execution engine)
      - node.content.text         (fallback / alternate serialisation)
    """
    visited: set = set()
    queue: list = [start_node_id]

    while queue:
        node_id = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)

        node = nodes.get(node_id, {})
        if node.get("type") == "message":
            # Return the full content object (which contains text, template_name, etc.)
            content = node.get("config", {}).get("content", {})
            if not content:
                content = node.get("content", {})
            if content:
                return content

        next_ids = _children(node_id, connections)
        queue.extend(next_ids)

    return None


async def _handle_google_sheet_trigger(
    org_id: str,
    flow_id: str,
    idx: int,
    config: dict,
    start_node_id: str,
    nodes: dict,
    connections: list,
) -> Optional[dict]:
    """
    Handle a google_sheet trigger on publish:
    1. Register in automation_triggers for future scheduler runs
    2. Extract the message text from the connected node
    3. Return params for immediate processing (to be run after publish success)
    """
    sheet_id = config.get("sheet_id")
    column_name = config.get("column_name")

    if not sheet_id or not column_name:
        logger.warning(
            f"google_sheet trigger for flow {flow_id} missing sheet_id or column_name — skipping."
        )
        return None

    # Get message config from the flow's message node
    message_config = _extract_message_from_start(start_node_id, nodes, connections)
    if not message_config:
        logger.debug(
             "Message extraction debug for flow {}: start_node_id={}, node_count={}, connection_count={}",
             flow_id,
             start_node_id,
             len(nodes),
             len(connections),
         )
        return None

    # Register the trigger (with message_text stored for scheduler use)
    triggers_collection = _automation_triggers_collection()
    trigger_doc = {
        "trigger_id": f"trigger_{flow_id}_{idx}",
        "flow_id": flow_id,
        "org_id": org_id,
        "platform": "google_sheet",
        "trigger_type": "google_sheet",
        "filters": {
            "sheet_id": config.get("sheet_id"),
            "sheet_name": config.get("sheet_name"),
            "column_name": config.get("column_name"),
            "row_start": config.get("row_start"),
            "row_end": config.get("row_end"),
        },
        "message_config": message_config,
        "message_text": message_config.get("text", ""),  # Fallback for older triggers
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
    logger.info(f"Registered google_sheet trigger for flow {flow_id}, sheet {sheet_id}")

    # Return params for immediate processing (deferred until finish successfully)
    return {
        "org_id": org_id,
        "flow_id": flow_id,
        "trigger_config": trigger_doc["filters"],
        "message_config": message_config,
    }


async def _register_single_trigger(
    org_id: str,
    flow_id: str,
    idx: int,
    trigger_type: str,
    config: dict,
    start_node_id: str,
):
    """Register a single non-followup, non-google_sheet trigger in automation_triggers."""
    triggers_collection = _automation_triggers_collection()

    # Determine platform from trigger type
    if any(ig_key in trigger_type for ig_key in ["instagram", "ig_comment", "ig_dm", "ig_story"]):
        platform = "instagram"
    elif any(wa_key in trigger_type for wa_key in ["whatsapp", "wa_"]):
        platform = "whatsapp"
    else:
        # Fallback based on trigger type context or existing defaults
        platform = "whatsapp"

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
    """
    for idx, trigger_config in enumerate(flow_data.get("triggers", [])):
        trigger_type = trigger_config.get("type")
        if trigger_type in ("whatsapp_followup", "google_sheet"):
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

            # Extra validation for google_sheet triggers
            if trigger.get("type") == "google_sheet":
                config = trigger.get("config", {})
                if not config.get("sheet_id"):
                    return False, "Google Sheet trigger requires 'sheet_id'"
                if not config.get("column_name"):
                    return False, "Google Sheet trigger requires 'column_name' (phone number column)"

                # Validate row range if provided
                row_start = config.get("row_start")
                row_end = config.get("row_end")
                if row_start is not None and row_start < 1:
                    return False, "row_start must be >= 1"
                if row_end is not None and row_start is not None and row_end < row_start:
                    return False, "row_end must be >= row_start"

            # Strict check for WhatsApp message received trigger (New System)
            if trigger.get("type") == "whatsapp_message_received":
                config = trigger.get("config", {})
                keyword = config.get("keyword")
                # Reject None, empty string, empty list
                if not keyword or (isinstance(keyword, (list, str)) and not any(keyword)):
                    return False, "WhatsApp message trigger requires at least one non-empty keyword"

        # Strict check for WhatsApp message received trigger (Legacy System)
        if flow_data.get("trigger_from") == "webhook" and flow_data.get("trigger_event") == "message_received":
            trigger_msgs = flow_data.get("trigger_messages")
            if not trigger_msgs or (isinstance(trigger_msgs, (list, str)) and not any(trigger_msgs)):
                return False, "WhatsApp flow requires trigger keywords for message_received event"

        return True, None

    except Exception as e:
        return False, f"Validation error: {str(e)}"


# Helper functions
async def _send_text_message(org_id: str, whatsapp_business_id: str, conversation_id: str, raw_content: str, template_variables: dict):
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

async def _send_interactive_message(org_id: str, whatsapp_business_id: str, conversation_id: str, raw_data: dict, template_variables: dict, label: str) -> bool:
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

async def _complete_onboarding(org_id: str, conversation_id: str):
    await async_db[f"conversations_{org_id}"].update_one(
        {"conversation_id": conversation_id},
        {"$set": {"onboarding_stage": "completed"}},
    )
    logger.info(f"[{org_id}] Onboarding stage marked as completed for {conversation_id}")

# TODO: flow doc must have an index on status, trigger_event, trigger_from
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
    source_message_id: str | None = None,
):
    logger.info(f"[{org_id}] Executing WhatsApp flow for conversation: {conversation_id} | Stage: {stage} | Type: {message_type}")

    welcome_message = flow.get("welcome_message")
    welcome_message_ids = [btn["reply"]["id"] for btn in welcome_message.get("interactive", {}).get("buttons", [])] if welcome_message and welcome_message.get("type") == "interactive" else []
    introduction_message = flow.get("introduction_message")
    start_node_id = flow.get("start_node_id")
    flow_nodes = flow.get("nodes", {})

    # Parse trigger keywords — supports both single words and full sentences/phrases.
    # Keywords are comma-separated, but we also keep the FULL string as a keyword
    # so that phrases containing commas (e.g. "Hello, I am interested in X") still match.
    trigger_keywords = []
    if trigger_messages:
        full_phrase = trigger_messages.strip().lower()
        if full_phrase:
            trigger_keywords.append(full_phrase)  # always add the full string as-is
        # Also split by comma for individual keywords ("hi,hello,menu" → ["hi", "hello", "menu"])
        for part in trigger_messages.split(","):
            cleaned = part.strip().lower()
            if cleaned and cleaned != full_phrase:  # avoid duplicate of full string
                trigger_keywords.append(cleaned)
    logger.info(f"[{org_id}] Trigger keywords: {trigger_keywords}")
    template_variables = {"customer_name": get_first_name(customer_name)}

    ctx = dict(org_id=org_id, whatsapp_business_id=whatsapp_business_id, conversation_id=conversation_id)

    async def consume_run() -> bool:
        from fastapi import HTTPException
        from services.entitlement_service import consume_quota_async

        key = source_message_id or f"{conversation_id}:{message_type}:{message_content}"
        try:
            await consume_quota_async(org_id, "AUTOMATIONS", "monthly_runs", 1, f"wabot:{flow.get('flow_id')}:{key}")
            return True
        except HTTPException as exc:
            logger.warning(f"[{org_id}] WhatsApp automation blocked: {exc.detail}")
            return False

    # Interactive (button / list click)
    if message_type == "interactive":
        selection_id = message_content
        logger.info(f"[{org_id}] Processing interactive selection ID: '{selection_id}'")

        if selection_id not in flow_nodes:
            logger.warning(f"[{org_id}] Node '{selection_id}' not found in flow, routing to AI...")
            return False

        if not await consume_run():
            return False

        if selection_id in welcome_message_ids:
            logger.info(f"[{org_id}] Selection ID '{selection_id}' is a welcome message button. Updating conversations collection.")

        node_data = flow_nodes[selection_id]
        node_type = node_data.get("type")

        await _complete_onboarding(org_id, conversation_id)

        if node_type == "text":
            logger.info(f"[{org_id}] Sending text node response for '{selection_id}'")
            await _send_text_message(**ctx, raw_content=node_data.get("content", ""), template_variables=template_variables)
            logger.success(f"[{org_id}] Successfully sent text node response for '{selection_id}'")
            return True

        # Note: Currently only button, list, and cta_url are supported
        # TODO: Add support for more interactive types
        elif node_type in {"button", "list", "cta_url"}:
            logger.info(f"[{org_id}] Sending interactive node response for '{selection_id}' (Type: {node_type})")
            ok = await _send_interactive_message(**ctx, raw_data=node_data, template_variables=template_variables, label=selection_id)
            if ok:
                logger.success(f"[{org_id}] Successfully sent interactive node response for '{selection_id}'")
            return ok

        else:
            logger.warning(f"[{org_id}] Unknown node_type '{node_type}' for selection '{selection_id}'")
            return False

    # Welcome / keyword trigger
    if welcome_message or start_node_id:
        msg_lower = message_content.strip().lower() if message_content else ""

        # Match complete keywords or phrases, not fragments inside unrelated words.
        is_keyword_match = (
            message_type == "text"
            and trigger_event == "message_received"
            and msg_lower
            and any(message_contains_keyword(msg_lower, kw) for kw in trigger_keywords)
        )

        if is_keyword_match:
            matched = [kw for kw in trigger_keywords if message_contains_keyword(msg_lower, kw)]
            logger.info(f"[{org_id}] Keyword match found: message='{msg_lower}' matched={matched}")

        if not is_keyword_match:
            logger.info(f"[{org_id}] Message '{msg_lower}' did not match flow trigger keywords {trigger_keywords}, routing to AI...")
            return False

        if not await consume_run():
            return False

        # Send optional introduction first
        if introduction_message:
            intro_type = introduction_message.get("type")
            if intro_type == "text":
                await _send_text_message(**ctx, raw_content=introduction_message.get("content", ""), template_variables=template_variables)
                logger.info(f"[{org_id}] Successfully sent introduction text message.")
            elif intro_type == "interactive":
                await _send_interactive_message(**ctx, raw_data=introduction_message.get("interactive", {}), template_variables=template_variables, label="introduction")
                logger.info(f"[{org_id}] Successfully sent introduction interactive message.")

        if start_node_id and start_node_id in flow_nodes:
            logger.info(f"[{org_id}] Trigger conditions met. Starting flow at node '{start_node_id}'")
            node_data = flow_nodes[start_node_id]
            node_type = node_data.get("type")

            await _complete_onboarding(org_id, conversation_id)

            if node_type == "text":
                await _send_text_message(**ctx, raw_content=node_data.get("content", ""), template_variables=template_variables)
                logger.success(f"[{org_id}] Successfully sent text start node and completed onboarding.")
                return True
            elif node_type in {"button", "list", "cta_url"}:
                ok = await _send_interactive_message(**ctx, raw_data=node_data, template_variables=template_variables, label=start_node_id)
                if ok:
                    logger.success(f"[{org_id}] Successfully sent interactive start node. Awaiting user selection.")
                return ok
            else:
                logger.warning(f"[{org_id}] Unknown start node_type '{node_type}'")
                return False

        elif welcome_message:
            # Legacy fallback
            welcome_type = welcome_message.get("type")
            logger.info(f"[{org_id}] Trigger conditions met. Sending legacy welcome message (Type: {welcome_type})")

            # Send welcome message
            if welcome_type == "text":
                await _send_text_message(**ctx, raw_content=welcome_message.get("content", ""), template_variables=template_variables)
                await _complete_onboarding(org_id, conversation_id)  # plain-text welcome → done immediately
                logger.success(f"[{org_id}] Successfully sent text welcome message and completed onboarding.")
                return True

            elif welcome_type == "interactive":
                ok = await _send_interactive_message(**ctx, raw_data=welcome_message.get("interactive", {}), template_variables=template_variables, label="welcome")
                if ok:
                    # Keep stage as "welcome" until they actually click a button
                    logger.success(f"[{org_id}] Successfully sent interactive welcome message. Awaiting user selection.")
                return ok

    logger.warning(f"[{org_id}] Flow execution fell through all scenarios. Routing to AI...")
    return False
