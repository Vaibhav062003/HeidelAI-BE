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


# TODO: flow doc must have an index on status, trigger_event, trigger_from
async def execute_whatsapp_flow(
        flow: dict,
        org_id: str,
        conversation_id: str,
        stage: str,
        customer_name: str,
        trigger_event: str,
        trigger_messages: str,
        message_content: str,  # For interactive message_type, this is the selection_id
        whatsapp_business_id: str,
        message_type: str,
    ):
    logger.info(f"[{org_id}] Executing WhatsApp flow for conversation: {conversation_id} | Stage: {stage} | Type: {message_type}")
    
    welcome_message = flow.get("welcome_message")
    flow_nodes = flow.get("nodes", {})
    
    # Safely parse trigger keywords
    trigger_keywords = [msg.strip().lower() for msg in trigger_messages.split(",")] if trigger_messages else []

    first_name = get_first_name(customer_name)
    template_variables = {
        "customer_name": first_name,
    }

    # User clicked a button or list item
    if message_type == "interactive":
        selection_id = message_content  # The ID of the button/list item clicked
        logger.info(f"[{org_id}] Processing interactive selection ID: '{selection_id}'")
        
        if selection_id in flow_nodes:
            node_data = flow_nodes[selection_id]
            node_type = node_data.get("type")

            # The user successfully clicked a button, so we graduate them from the onboarding stage
            db[f"conversations_{org_id}"].update_one(
                {"conversation_id": conversation_id},
                {"$set": {"onboarding_stage": "completed"}},
            )
            logger.info(f"[{org_id}] Onboarding stage marked as completed for {conversation_id}")

            if node_type == "text":
                logger.info(f"[{org_id}] Sending text node response for '{selection_id}'")
                raw_content = node_data.get("content", "")
                personalized_content = inject_variables(raw_content, template_variables)

                await send_whatsapp_message(
                    org_id=org_id,
                    mode="reply",
                    message={
                        "sender_id": whatsapp_business_id,
                        "conversation_id": conversation_id,
                        "content": personalized_content
                    }
                )

                logger.success(f"[{org_id}] Successfully sent text node response for '{selection_id}'")
                return True
            
            # Note: Currently we only support button, list, and cta_url 
            # TODO: Add support for more interactive types
            elif node_type in ["button", "list", "cta_url"]:
                try:
                    logger.info(f"[{org_id}] Sending interactive node response for '{selection_id}' (Type: {node_type})")
                    # Inject variables into the raw dictionary
                    personalized_dict = inject_variables(node_data, template_variables)
                    
                    # Validate the personalized dictionary with Pydantic
                    validated_interactive_msg = InteractiveMessageAdapter.validate_python(personalized_dict)
                    
                    await send_interactive_whatsapp_message(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        sender_id=whatsapp_business_id,
                        interactive_message=validated_interactive_msg
                    )

                    logger.success(f"[{org_id}] Successfully sent interactive node response for '{selection_id}'")
                    return True
                except Exception as e:
                    logger.error(f"[{org_id}] Failed to validate interactive node data for '{selection_id}': {e}")
                    return False
            else:
                logger.warning(f"[{org_id}] Unknown node_type '{node_type}' for selection '{selection_id}'")

        else:
            # The button ID isn't in the flow JSON. 
            logger.warning(f"[{org_id}] Node '{selection_id}' not found in flow, routing to AI...")
            # await send_to_pma_chatbot(...)
            return False

    # Triggering the Welcome Message
    elif welcome_message:
        is_new_session = (stage == "welcome")
        is_keyword_match = (
            message_type == "text" 
            and trigger_event == "message_received" 
            and message_content.strip().lower() in trigger_keywords
        )

        if is_new_session or is_keyword_match:
            welcome_type = welcome_message.get("type")
            logger.info(f"[{org_id}] Trigger conditions met. Sending welcome message (Type: {welcome_type})")

            if welcome_type == "text":
                raw_content = welcome_message.get("content", "")
                personalized_content = inject_variables(raw_content, template_variables)

                await send_whatsapp_message(
                    org_id=org_id,
                    mode="reply",
                    message={
                        "sender_id": whatsapp_business_id,
                        "conversation_id": conversation_id,
                        "content": personalized_content
                    }
                )

                # It's a plain text welcome, mark as complete immediately
                db[f"conversations_{org_id}"].update_one(
                    {"conversation_id": conversation_id},
                    {"$set": {"onboarding_stage": "completed"}},
                )
                logger.success(f"[{org_id}] Successfully sent text welcome message and completed onboarding.")
                return True

            elif welcome_type == "interactive":
                try:
                    raw_interactive_data = welcome_message.get("interactive", {})
                    personalized_welcome_dict = inject_variables(raw_interactive_data, template_variables)
                    
                    validated_welcome_msg = InteractiveMessageAdapter.validate_python(personalized_welcome_dict)
                    
                    await send_interactive_whatsapp_message(
                        org_id=org_id,
                        conversation_id=conversation_id,
                        sender_id=whatsapp_business_id,
                        interactive_message=validated_welcome_msg
                    )

                    # DO NOT update DB stage here. Keep it as "welcome" until they click a button.
                    logger.success(f"[{org_id}] Successfully sent interactive welcome message. Awaiting user selection.")
                    return True
                    
                except Exception as e:
                    logger.error(f"[{org_id}] Failed to validate welcome interactive data: {e}")
                    return False

        else:
            logger.info(f"[{org_id}] Message did not match welcome flow triggers, routing to AI...")
            return False

    logger.warning(f"[{org_id}] Flow execution fell through all scenarios. Routing to AI...")
    return False