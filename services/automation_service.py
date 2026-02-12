import uuid
from typing import Optional, Dict
from datetime import datetime, timezone

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


# === FLOW VALIDATION ===
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