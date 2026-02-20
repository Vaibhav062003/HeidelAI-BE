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


# GET SINGLE
async def get_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """
    Get a specific automation flow
    
    Args:
        org_id: Organization ID
        flow_id: Flow ID to retrieve
    
    Returns:
        Flow document or None if not found
    """
    try:
        collection = _automation_flows_collection(org_id)
        
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        
        if flow:
            logger.info(f"Retrieved automation flow: {flow_id} for org: {org_id}")
            return serialize_mongo(flow)
        
        logger.warning(f"Automation flow not found: {flow_id} for org: {org_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error getting automation flow: {str(e)}")
        raise

# Update flow
async def update_automation_flow(
    org_id: str,
    flow_id: str,
    update_data: Dict,
    updated_by: Optional[str] = None
) -> Optional[Dict]:
    """Update an automation flow"""
    try:
        collection = _automation_flows_collection(org_id)
        
        update_doc = {
            "updated_at": datetime.now(timezone.utc),
            "updated_by": updated_by
        }
        
        if "name" in update_data:
            update_doc["name"] = update_data["name"]
        if "description" in update_data:
            update_doc["description"] = update_data["description"]
        if "flow_data" in update_data:
            update_doc["flow_data"] = update_data["flow_data"]
            # Increment version when flow_data changes
            collection.update_one(
                {"flow_id": flow_id, "org_id": org_id},
                {"$inc": {"version": 1}}
            )
        if "status" in update_data:
            update_doc["status"] = update_data["status"]
        
        flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": update_doc},
            return_document=ReturnDocument.AFTER
        )
        
        if flow:
            return serialize_mongo(flow)
        return None
        
    except Exception as e:
        logger.error(f"Error updating automation flow: {str(e)}")
        raise


# DELETE FLOW
async def delete_automation_flow(org_id: str, flow_id: str) -> bool:
    """Delete an automation flow"""
    try:
        collection = _automation_flows_collection(org_id)
        
        result = collection.delete_one({"flow_id": flow_id, "org_id": org_id})
        
        if result.deleted_count > 0:
            logger.info(f"Deleted automation flow: {flow_id} for org: {org_id}")
            return True
        
        logger.warning(f"Automation flow not found for deletion: {flow_id}")
        return False
        
    except Exception as e:
        logger.error(f"Error deleting automation flow: {str(e)}")
        raise


def _automation_triggers_collection():
    return db["automation_triggers"]

async def publish_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """Publish (activate) an automation flow"""
    try:
        collection = _automation_flows_collection(org_id)
        
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return None
        
        # Validate flow has triggers
        flow_data = flow.get("flow_data", {})
        triggers = flow_data.get("triggers", [])
        if not triggers:
            raise ValueError("Flow must have at least one trigger")
        
        # Register triggers
        await _register_flow_triggers(org_id, flow_id, flow_data)
        
        # Update flow status
        now = datetime.now(timezone.utc)
        updated_flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {"status": "published", "published_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER
        )
        
        if updated_flow:
            return serialize_mongo(updated_flow)
        return None
        
    except Exception as e:
        logger.error(f"Error publishing automation flow: {str(e)}")
        raise


async def unpublish_automation_flow(org_id: str, flow_id: str) -> Optional[Dict]:
    """Unpublish (deactivate) an automation flow"""
    try:
        collection = _automation_flows_collection(org_id)
        
        flow = collection.find_one({"flow_id": flow_id, "org_id": org_id})
        if not flow:
            return None
        
        # Deactivate triggers
        await _deactivate_flow_triggers(org_id, flow_id)
        
        # Update flow status
        now = datetime.now(timezone.utc)
        updated_flow = collection.find_one_and_update(
            {"flow_id": flow_id, "org_id": org_id},
            {"$set": {
                "status": "draft", 
                "unpublished_at": now, 
                "updated_at": now
            }},
            return_document=ReturnDocument.AFTER
        )
        
        if updated_flow:
            logger.info(f"Unpublished automation flow: {flow_id}")
            return serialize_mongo(updated_flow)
        return None
        
    except Exception as e:
        logger.error(f"Error unpublishing automation flow: {str(e)}")
        raise


async def _deactivate_flow_triggers(org_id: str, flow_id: str):
    """Deactivate triggers for a flow"""
    triggers_collection = _automation_triggers_collection()
    
    result = triggers_collection.update_many(
        {"flow_id": flow_id, "org_id": org_id},
        {"$set": {
            "status": "inactive", 
            "deactivated_at": datetime.now(timezone.utc)
        }}
    )
    
    logger.info(f"Deactivated {result.modified_count} triggers for flow {flow_id}")


async def increment_execution_count(org_id: str, flow_id: str):
    """Increment the execution count for a flow"""
    try:
        collection = _automation_flows_collection(org_id)
        
        collection.update_one(
            {"flow_id": flow_id, "org_id": org_id},
            {
                "$inc": {"execution_count": 1},
                "$set": {"last_executed_at": datetime.now(timezone.utc)}
            }
        )
        
        logger.info(f"Incremented execution count for flow {flow_id}")
        
    except Exception as e:
        logger.error(f"Error incrementing execution count: {str(e)}")

# REGISTER FLOW TRIGGERS
# automation_service.py - Line ~270

async def _register_flow_triggers(org_id: str, flow_id: str, flow_data: Dict):
    """Register triggers for a flow"""
    triggers_collection = _automation_triggers_collection()
    
    triggers = flow_data.get("triggers", [])
    
    # CRITICAL FIX: Process triggers directly, don't look them up in nodes
    for idx, trigger_config in enumerate(triggers):
        trigger_type = trigger_config.get("type")  # ✅ Get from trigger directly
        config = trigger_config.get("config", {})
        start_node_id = trigger_config.get("start_node_id")
        
        if not start_node_id:
            logger.warning(f"Trigger {idx} has no start_node_id, skipping")
            continue
        
        # Determine platform from trigger type
        platform = "instagram" if "instagram" in trigger_type else "whatsapp"

        # Handle both array and string keywords
        keyword_value = config.get("keyword", "")
        if isinstance(keyword_value, list):
            keyword_value = "|".join(keyword_value)
        
        # Extract filters from config
        filters = {
        "keyword": keyword_value,
        "post_id": config.get("post_id"),
        "story_id": config.get("story_id"),
        "tag": config.get("tag")
}
        
        trigger_doc = {
            "trigger_id": f"trigger_{flow_id}_{idx}",
            "flow_id": flow_id,
            "org_id": org_id,
            "platform": platform,
            "trigger_type": trigger_type,
            "filters": filters,
            "start_node_id": start_node_id,  # Store for execution
            "status": "active",
            "registered_at": datetime.now(timezone.utc),
            "last_triggered_at": None
        }
        
        triggers_collection.update_one(
            {"trigger_id": trigger_doc["trigger_id"]},
            {"$set": trigger_doc},
            upsert=True
        )
        
        logger.info(f"Registered trigger: {trigger_type} for flow {flow_id}")


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