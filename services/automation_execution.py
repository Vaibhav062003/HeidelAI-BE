# === Third-party Modules ===
import uuid
import asyncio
import re
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

# === Internal Modules ===
from database import get_mongo_db
from services.automation_actions import execute_action_node
from loguru import logger

db = get_mongo_db()


# === Helper Functions for Collection Access ===
def _automation_flows_collection(org_id: str):
    """Get the automation flows collection for an organization"""
    return db[f"automation_flows_{org_id}"]


def _automation_executions_collection():
    """Get the global automation executions collection"""
    return db["automation_executions"]


# === Flow Execution Context ===
class FlowExecutionContext:
    """Context for tracking flow execution state"""
    
    def __init__(self, org_id: str, flow_id: str, trigger_type: str, trigger_data: Dict):
        self.execution_id = f"exec_{uuid.uuid4()}"
        self.org_id = org_id
        self.flow_id = flow_id
        self.trigger_type = trigger_type
        self.trigger_data = trigger_data
        self.variables = {
            # Keep full nested structure for advanced users
            "trigger_data": trigger_data,
            
            # Flatten common fields for convenience
            "customer_name": trigger_data.get("customer_name") or trigger_data.get("commenter_username"),
            "customer_username": trigger_data.get("customer_username") or trigger_data.get("commenter_username"),
            "customer_id": trigger_data.get("customer_id") or trigger_data.get("commenter_id"),
            "comment_text": trigger_data.get("comment_text") or trigger_data.get("message_text"),
            "comment_id": trigger_data.get("comment_id"),
            "post_id": trigger_data.get("post_id"),
            "platform": trigger_data.get("platform"),
            "platform_id": trigger_data.get("platform_id"),
            # Google Sheet specific
            "phone_number": trigger_data.get("phone_number"),
            "row": trigger_data.get("row", {}),
            
            # Dynamic variables
            "execution_id": self.execution_id,
            "org_id": org_id,
            "flow_id": flow_id,
        }
        self.logs = []
        self.status = "running"
        self.error = None
        self.created_at = datetime.now(timezone.utc)
        self.completed_at = None
    
    def log(self, node_id: str, node_type: str, action: str, message: str, success: bool = True, details: Dict = None):
        """Add a log entry"""
        log_entry = {
            "timestamp": datetime.now(timezone.utc),
            "node_id": node_id,
            "node_type": node_type,
            "action": action,
            "message": message,
            "success": success
        }
        if details:
            log_entry["details"] = details
        
        self.logs.append(log_entry)
        
        if success:
            logger.info(f"[{self.execution_id}] {node_id}: {message}")
        else:
            logger.error(f"[{self.execution_id}] {node_id}: {message}")
    
    def set_variable(self, key: str, value: Any):
        """Set a variable in the execution context"""
        self.variables[key] = value
    
    def get_variable(self, key: str) -> Any:
        """Get a variable from the execution context"""
        parts = key.split(".")
        value = self.variables
        
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None
        
        return value
    
    def replace_variables(self, text: str) -> str:
        """Replace {{variable}} placeholders in text"""
        if not text or not isinstance(text, str):
            return text
        
        pattern = r'\{\{([^}]+)\}\}'
        
        def replace_match(match):
            var_name = match.group(1).strip()
            value = self.get_variable(var_name)
            return str(value) if value is not None else match.group(0)
        
        return re.sub(pattern, replace_match, text)
    
    def mark_success(self):
        """Mark execution as successful"""
        self.status = "success"
        self.completed_at = datetime.now(timezone.utc)
    
    def mark_failed(self, error_message: str, node_id: str = None):
        """Mark execution as failed"""
        self.status = "failed"
        self.error = {
            "message": error_message,
            "node_id": node_id,
            "timestamp": datetime.now(timezone.utc)
        }
        self.completed_at = datetime.now(timezone.utc)
    
    def to_dict(self) -> Dict:
        """Convert context to dictionary for storage"""
        duration_ms = None
        if self.completed_at:
            duration_ms = int((self.completed_at - self.created_at).total_seconds() * 1000)
        
        return {
            "execution_id": self.execution_id,
            "flow_id": self.flow_id,
            "org_id": self.org_id,
            "status": self.status,
            "trigger_type": self.trigger_type,
            "trigger_data": self.trigger_data,
            "variables": self.variables,
            "logs": self.logs,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "duration_ms": duration_ms
        }


# Trigger node types that are just pass-throughs (log and continue)
TRIGGER_NODE_TYPES = {
    "instagram_dm_received",
    "instagram_comment",
    "instagram_story_reply",
    "instagram_story_mention",
    "whatsapp_message_received",
    "contact_tag_added",
    "contact_tag_removed",
    # Google Sheet trigger node
    "google_sheet",
    "trigger_google_sheet",
}


# === Main Execution Function ===
async def execute_automation_flow(
    org_id: str,
    flow_id: str,
    trigger_type: str,
    trigger_data: Dict
) -> Dict:
    """
    Execute an automation flow.
    """
    context = FlowExecutionContext(org_id, flow_id, trigger_type, trigger_data)
    
    try:
        flows_collection = _automation_flows_collection(org_id)
        flow = flows_collection.find_one({"flow_id": flow_id, "org_id": org_id})
        
        if not flow:
            raise Exception(f"Flow {flow_id} not found")
        
        if flow.get("status") != "published":
            raise Exception(f"Flow {flow_id} is not published")
        
        context.log(
            node_id="system",
            node_type="system",
            action="execution_start",
            message=f"Starting execution of flow: {flow.get('name')}",
            success=True
        )
        
        flow_data = flow.get("flow_data", {})
        nodes = flow_data.get("nodes", {})
        connections = flow_data.get("connections", [])
        triggers = flow_data.get("triggers", [])
        
        # Map event type to expected trigger type
        event_to_trigger_map = {
            "post_comment": "instagram_comment",
            "story_reply": "instagram_story_reply",
            "message_received": "instagram_dm_received",
            "google_sheet_row": "google_sheet",
        }

        expected_trigger_type = event_to_trigger_map.get(context.trigger_type, context.trigger_type)

        # Find matching trigger
        start_node_id = None
        for trigger_config in triggers:
            ttype = trigger_config.get("type")
            if ttype == expected_trigger_type or ttype in (context.trigger_type,):
                start_node_id = trigger_config.get("start_node_id")
                logger.info(f"✅ Found matching trigger, starting at node: {start_node_id}")
                break

        if not start_node_id:
            raise Exception(f"No matching trigger found for type: {expected_trigger_type}")

        logger.info(f"🚀 Executing flow from start_node: {start_node_id}")
        await _execute_node(context, start_node_id, nodes, connections)

        context.mark_success()
        
        context.log(
            node_id="system",
            node_type="system",
            action="execution_complete",
            message="Flow execution completed successfully",
            success=True
        )
        
    except Exception as e:
        error_message = str(e)
        context.mark_failed(error_message)
        context.log(
            node_id="system",
            node_type="system",
            action="execution_failed",
            message=f"Flow execution failed: {error_message}",
            success=False
        )
    
    finally:
        # Ensure that failures in saving or incrementing do not crash background tasks
        try:
            await _save_execution_record(context)
        except Exception as e:
            logger.error(f"Failed to save automation execution record: {e}")
        
        if context.status == "success":
            from services.automation_service import increment_execution_count
            try:
                await increment_execution_count(org_id, flow_id)
            except Exception as e:
                logger.error(f"Failed to increment automation execution count: {e}")
    
    return context.to_dict()


async def _execute_node(
    context: FlowExecutionContext,
    node_id: str,
    nodes: Dict,
    connections: List[Dict]
) -> bool:
    """Execute a single node and its connected nodes"""
    if node_id not in nodes:
        context.log(
            node_id=node_id,
            node_type="unknown",
            action="node_not_found",
            message=f"Node {node_id} not found in flow",
            success=False
        )
        return False
    
    node = nodes[node_id]
    node_type = node.get("type")
    node_config = node.get("config", {})
    
    context.log(
        node_id=node_id,
        node_type=node_type,
        action="node_start",
        message=f"Executing node: {node_type}",
        success=True
    )
    
    try:
        # Handle TRIGGER nodes - just log and continue
        if node_type in TRIGGER_NODE_TYPES:
            context.log(
                node_id=node_id,
                node_type=node_type,
                action="trigger",
                message=f"Trigger node: {node_type}",
                success=True
            )
        
        # Handle CONDITION nodes
        elif node_type == "condition":
            result = await _execute_condition_node(context, node_id, node_config)
            next_nodes = _get_conditional_next_nodes(node_id, connections, result)
            for next_node_id in next_nodes:
                await _execute_node(context, next_node_id, nodes, connections)
            return True
        
        # Handle RANDOMIZER nodes
        elif node_type == "randomizer":
            next_node_id = await _execute_randomizer_node(context, node_id, connections)
            if next_node_id:
                await _execute_node(context, next_node_id, nodes, connections)
            return True
        
        # Handle all ACTION nodes
        else:
            await execute_action_node(context, node_id, node_type, node_config)
        
        # Find and execute next connected nodes
        next_nodes = _get_next_nodes(node_id, connections)
        for next_node_id in next_nodes:
            await _execute_node(context, next_node_id, nodes, connections)
        
        return True
        
    except Exception as e:
        context.log(
            node_id=node_id,
            node_type=node_type,
            action="node_error",
            message=f"Error executing node: {str(e)}",
            success=False
        )
        raise


async def _execute_condition_node(context: FlowExecutionContext, node_id: str, config: Dict) -> bool:
    variable = config.get("variable")
    operator = config.get("operator")
    value = config.get("value")
    var_value = context.get_variable(variable)
    
    result = False
    if operator == "equals":
        result = str(var_value) == str(value)
    elif operator == "not_equals":
        result = str(var_value) != str(value)
    elif operator == "contains":
        result = value in str(var_value) if var_value else False
    elif operator == "not_contains":
        result = value not in str(var_value) if var_value else True
    elif operator == "greater_than":
        try:
            result = float(var_value) > float(value)
        except (ValueError, TypeError):
            result = False
    elif operator == "less_than":
        try:
            result = float(var_value) < float(value)
        except (ValueError, TypeError):
            result = False
    
    context.log(
        node_id=node_id,
        node_type="condition",
        action="condition_evaluated",
        message=f"Condition evaluated: {variable} {operator} {value} = {result}",
        success=True,
        details={"result": result, "variable_value": var_value}
    )
    
    return result


async def _execute_randomizer_node(context: FlowExecutionContext, node_id: str, connections: List[Dict]) -> Optional[str]:
    import random
    
    next_nodes = [conn["target"] for conn in connections if conn.get("source") == node_id]
    if not next_nodes:
        return None
    
    selected_node = random.choice(next_nodes)
    
    context.log(
        node_id=node_id,
        node_type="randomizer",
        action="path_selected",
        message=f"Randomly selected path to: {selected_node}",
        success=True,
        details={"selected": selected_node, "options": next_nodes}
    )
    
    return selected_node


def _get_next_nodes(node_id: str, connections: List[Dict]) -> List[str]:
    return [conn["target"] for conn in connections if conn.get("source") == node_id]


def _get_conditional_next_nodes(node_id: str, connections: List[Dict], condition_result: bool) -> List[str]:
    result_str = "true" if condition_result else "false"
    
    matching_nodes = []
    for conn in connections:
        if conn.get("source") == node_id:
            source_handle = conn.get("sourceHandle", "")
            if result_str in source_handle.lower():
                matching_nodes.append(conn["target"])
    
    if not matching_nodes:
        matching_nodes = _get_next_nodes(node_id, connections)
    
    return matching_nodes


async def _save_execution_record(context: FlowExecutionContext):
    collection = _automation_executions_collection()
    execution_doc = context.to_dict()
    collection.insert_one(execution_doc)
    logger.info(f"Saved execution record: {context.execution_id}, status: {context.status}")