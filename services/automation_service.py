import uuid
from typing import Optional, Dict, List
from datetime import datetime, timezone
from pydantic import TypeAdapter

# Internal Modules
from database import get_mongo_db
from schemas.models import WhatsAppInteractiveMessage
from services.utils import get_first_name, inject_variables, serialize_mongo
from services.wa_service import send_interactive_whatsapp_message, send_whatsapp_message
from loguru import logger

db = get_mongo_db()

InteractiveMessageAdapter = TypeAdapter(WhatsAppInteractiveMessage)

# Helper Functions for Collection Access
def _automation_flows_collection(org_id: str):
    """Get the automation flows collection for an organization"""
    return db[f"automation_flows_{org_id}"]


# CREATE AUTOMATION FLOW
async def create_automation_flow(
    org_id: str,
    name: str,
    description: Optional[str] = None,
    flow_data: Optional[Dict] = None,
    created_by: Optional[str] = None
) -> Dict:
    """
    Create a new automation flow
    
    Args:
        org_id: Organization ID
        name: Flow name
        description: Flow description (optional)
        flow_data: Flow structure with nodes, connections, triggers (optional)
        created_by: User ID who created the flow
    
    Returns:
        Created flow document (without _id field)
    """
    try:
        collection = _automation_flows_collection(org_id)
        
        # Generate unique flow ID
        flow_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        
        # Create flow document
        flow_doc = {
            "flow_id": flow_id,
            "org_id": org_id,
            "name": name,
            "description": description,
            "status": "draft",  # Always start as draft
            "flow_data": flow_data or {
                "nodes": {},
                "connections": [],
                "triggers": []
            },
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
            "updated_by": created_by,
            "published_at": None,
            "execution_count": 0,
            "last_executed_at": None
        }
        
        # Insert into database
        collection.insert_one(flow_doc)
        
        logger.info(f"Created automation flow: {flow_id} for org: {org_id}")
        
        # Return serialized document (removes _id field)
        return serialize_mongo(flow_doc)
        
    except Exception as e:
        logger.error(f"Error creating automation flow: {str(e)}")
        raise


# List all flows 
async def list_automation_flows(
    org_id: str,
    status: Optional[str] = None
) -> List[Dict]:
    """
    List all automation flows for an organization
    
    Args:
        org_id: Organization ID
        status: Optional status filter ("draft" or "published")
    
    Returns:
        List of flow documents (sorted by created_at, newest first)
    """
    try:
        collection = _automation_flows_collection(org_id)
        
        # Build query
        query = {"org_id": org_id}
        if status:
            query["status"] = status
        
        # Get flows, sorted by creation date (newest first)
        flows = list(collection.find(query).sort("created_at", -1))
        
        logger.info(f"Listed {len(flows)} automation flows for org: {org_id}, status: {status}")
        
        # Serialize each flow (remove _id)
        return [serialize_mongo(flow) for flow in flows]
        
    except Exception as e:
        logger.error(f"Error listing automation flows: {str(e)}")
        raise

#  FLOW VALIDATION
def validate_flow_structure(flow_data: Dict) -> tuple:
    """
    Validate the structure of a flow
    
    Args:
        flow_data: Flow data containing nodes, connections, triggers
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        # Check required fields exist
        if not isinstance(flow_data, dict):
            return False, "Flow data must be a dictionary"
        
        if "nodes" not in flow_data:
            return False, "Flow data must contain 'nodes'"
        
        if "connections" not in flow_data:
            return False, "Flow data must contain 'connections'"
        
        if "triggers" not in flow_data:
            return False, "Flow data must contain 'triggers'"
        
        # Validate nodes
        nodes = flow_data.get("nodes", {})
        if not isinstance(nodes, dict):
            return False, "'nodes' must be a dictionary"
        
        # Validate connections
        connections = flow_data.get("connections", [])
        if not isinstance(connections, list):
            return False, "'connections' must be a list"
        
        # Validate each connection references existing nodes
        for conn in connections:
            if "source" not in conn or "target" not in conn:
                return False, "Each connection must have 'source' and 'target'"
            
            if conn["source"] not in nodes:
                return False, f"Connection source '{conn['source']}' not found in nodes"
            
            if conn["target"] not in nodes:
                return False, f"Connection target '{conn['target']}' not found in nodes"
        
        # Validate triggers
        triggers = flow_data.get("triggers", [])
        if not isinstance(triggers, list):
            return False, "'triggers' must be a list"
        
        # Validate each trigger references existing node
        for trigger in triggers:
            if "start_node_id" not in trigger:
                return False, "Each trigger must have 'start_node_id'"
            
            if trigger["start_node_id"] not in nodes:
                return False, f"Trigger start_node_id '{trigger['start_node_id']}' not found in nodes"
        
        return True, None
        
    except Exception as e:
        return False, f"Validation error: {str(e)}"


# Helpers functions
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

def _complete_onboarding(org_id: str, conversation_id: str):
    db[f"conversations_{org_id}"].update_one(
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
):
    logger.info(f"[{org_id}] Executing WhatsApp flow for conversation: {conversation_id} | Stage: {stage} | Type: {message_type}")

    welcome_message = flow.get("welcome_message")
    introduction_message = flow.get("introduction_message")
    flow_nodes = flow.get("nodes", {})

    trigger_keywords = [msg.strip().lower() for msg in trigger_messages.split(",")] if trigger_messages else []
    template_variables = {"customer_name": get_first_name(customer_name)}

    ctx = dict(org_id=org_id, whatsapp_business_id=whatsapp_business_id, conversation_id=conversation_id)

    # Interactive (button / list click)
    if message_type == "interactive":
        selection_id = message_content
        logger.info(f"[{org_id}] Processing interactive selection ID: '{selection_id}'")

        if selection_id not in flow_nodes:
            logger.warning(f"[{org_id}] Node '{selection_id}' not found in flow, routing to AI...")
            return False

        node_data = flow_nodes[selection_id]
        node_type = node_data.get("type")

        _complete_onboarding(org_id, conversation_id)

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
    if welcome_message:
        is_new_session = stage == "welcome"
        is_keyword_match = (
            message_type == "text"
            and trigger_event == "message_received"
            and message_content.strip().lower() in trigger_keywords
        )

        if not (is_new_session or is_keyword_match):
            logger.info(f"[{org_id}] Message did not match welcome flow triggers, routing to AI...")
            return False

        welcome_type = welcome_message.get("type")
        logger.info(f"[{org_id}] Trigger conditions met. Sending welcome message (Type: {welcome_type})")

        # Send optional introduction first
        if introduction_message:
            intro_type = introduction_message.get("type")
            if intro_type == "text":
                await _send_text_message(**ctx, raw_content=introduction_message.get("content", ""), template_variables=template_variables)
                logger.info(f"[{org_id}] Successfully sent introduction text message.")
            elif intro_type == "interactive":
                await _send_interactive_message(**ctx, raw_data=introduction_message.get("interactive", {}), template_variables=template_variables, label="introduction")
                logger.info(f"[{org_id}] Successfully sent introduction interactive message.")

        # Send welcome message
        if welcome_type == "text":
            await _send_text_message(**ctx, raw_content=welcome_message.get("content", ""), template_variables=template_variables)
            _complete_onboarding(org_id, conversation_id)  # plain-text welcome → done immediately
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