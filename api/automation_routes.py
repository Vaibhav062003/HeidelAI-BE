from typing import Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

# Internal Modules
from database import get_mongo_db
from auth.dependencies import CurrentUser
from services.automation_service import (
    create_automation_flow,
    list_automation_flows,
    get_automation_flow,
    update_automation_flow,
    delete_automation_flow,
    publish_automation_flow,
    unpublish_automation_flow,
    validate_flow_structure
)
from core.logger import get_logger

logger = get_logger(__name__)
db = get_mongo_db()

router = APIRouter(prefix="/api/automation_flows", tags=["Automation Flows"])


# Request/Response Models
class FlowNodePayload(BaseModel):
    """Represents a node's configuration in the flow"""
    type: str
    app: Optional[str] = None
    config: dict = Field(default_factory=dict)
    position: Optional[dict] = None


class FlowDocument(BaseModel):
    """Complete flow document structure"""
    nodes: dict = Field(default_factory=dict)
    connections: list = Field(default_factory=list)
    triggers: list = Field(default_factory=list)


class CreateFlowRequest(BaseModel):
    name: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    flow_data: Optional[FlowDocument] = None
    status: str = Field(default="draft")


class UpdateFlowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    flow_data: Optional[FlowDocument] = None
    status: Optional[str] = None


# CREATE FLOW ENDPOINT
@router.post("")
async def create_flow(payload: CreateFlowRequest, user: CurrentUser):
    """
    Create a new automation flow
    
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        # Validate flow structure if flow_data is provided
        if payload.flow_data:
            is_valid, error_message = validate_flow_structure(payload.flow_data.dict())
            if not is_valid:
                raise HTTPException(status_code=400, detail=f"Invalid flow structure: {error_message}")

        flow = await create_automation_flow(
            org_id=org_id,
            name=payload.name,
            description=payload.description,
            flow_data=payload.flow_data.dict() if payload.flow_data else None,
            created_by=user.user_id
        )

        return {"flow": flow}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create automation flow: {str(e)}")
    
@router.get("")
async def list_flows(user: CurrentUser, status: Optional[str] = None):
    """
    List all automation flows for the organization
    
    Query parameters:
    - status: Filter by status ("draft" or "published")

    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flows = await list_automation_flows(org_id=org_id, status=status)

        return {"flows": flows}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing automation flows: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list automation flows: {str(e)}")
    

# GET SINGLE 
@router.get("/{flow_id}")
async def get_flow(flow_id: str, user: CurrentUser):
    """Get a specific automation flow by ID"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await get_automation_flow(org_id=org_id, flow_id=flow_id)

        if not flow:
            raise HTTPException(status_code=404, detail="Automation flow not found")

        return {"flow": flow}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get automation flow: {str(e)}")


# UPDATE FLOW
@router.patch("/{flow_id}")
async def update_flow(flow_id: str, payload: UpdateFlowRequest, user: CurrentUser):
    """Update an existing automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        if payload.flow_data:
            is_valid, error_message = validate_flow_structure(payload.flow_data.dict())
            if not is_valid:
                raise HTTPException(status_code=400, detail=f"Invalid flow structure: {error_message}")

        update_data = {}
        if payload.name is not None:
            update_data["name"] = payload.name
        if payload.description is not None:
            update_data["description"] = payload.description
        if payload.flow_data is not None:
            update_data["flow_data"] = payload.flow_data.dict()
        if payload.status is not None:
            update_data["status"] = payload.status

        flow = await update_automation_flow(
            org_id=org_id,
            flow_id=flow_id,
            update_data=update_data,
            updated_by=user.user_id
        )

        if not flow:
            raise HTTPException(status_code=404, detail="Automation flow not found")

        return {"flow": flow}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to update automation flow: {str(e)}")


@router.delete("/{flow_id}")
async def delete_flow(flow_id: str, user: CurrentUser):
    """Delete an automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        success = await delete_automation_flow(org_id=org_id, flow_id=flow_id)

        if not success:
            raise HTTPException(status_code=404, detail="Automation flow not found")

        return {"message": "Automation flow deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete automation flow: {str(e)}")


# PUBLISH FLOW
@router.post("/{flow_id}/publish")
async def publish_flow(flow_id: str, user: CurrentUser):
    """Publish (activate) an automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await publish_automation_flow(org_id=org_id, flow_id=flow_id)

        if not flow:
            raise HTTPException(status_code=404, detail="Automation flow not found")

        return {"flow": flow, "message": "Automation flow published successfully"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error publishing automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to publish automation flow: {str(e)}")

# UNPUBLISH FLOW (around line 175)
@router.post("/{flow_id}/unpublish")
async def unpublish_flow(flow_id: str, user: CurrentUser):
    """Unpublish (deactivate) an automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await unpublish_automation_flow(org_id=org_id, flow_id=flow_id)

        if not flow:
            raise HTTPException(status_code=404, detail="Automation flow not found")

        return {"flow": flow, "message": "Automation flow unpublished successfully"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unpublishing automation flow: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to unpublish automation flow: {str(e)}")


# GET EXECUTION HISTORY
@router.get("/{flow_id}/executions")
async def get_flow_executions(
    flow_id: str, 
    user: CurrentUser,
    limit: int = 50,
    skip: int = 0
):
    """Get execution history for a flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        executions_collection = db["automation_executions"]
        
        executions = list(
            executions_collection.find(
                {"flow_id": flow_id, "org_id": org_id}
            )
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        
        # Serialize for JSON
        for exec in executions:
            exec["_id"] = str(exec["_id"])
            if exec.get("created_at"):
                exec["created_at"] = exec["created_at"].isoformat()
            if exec.get("completed_at"):
                exec["completed_at"] = exec["completed_at"].isoformat()
            if exec.get("error") and exec["error"].get("timestamp"):
                exec["error"]["timestamp"] = exec["error"]["timestamp"].isoformat()
        
        return {"executions": executions, "total": len(executions)}

    except Exception as e:
        logger.error(f"Error fetching executions: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# GET AVAILABLE TRIGGER TYPES
@router.get("/triggers/available")
async def get_available_triggers(user: CurrentUser):
    """Get list of available trigger types"""
    triggers = [
        {
            "type": "instagram_comment",
            "name": "Instagram Post Comment",
            "description": "Triggers when someone comments on your Instagram post",
            "platform": "instagram",
            "requires": ["post_id"],
            "optional": ["keyword"]
        },
        {
            "type": "instagram_dm_received",
            "name": "Instagram DM Received",
            "description": "Triggers when you receive a DM on Instagram",
            "platform": "instagram",
            "requires": [],
            "optional": ["keyword"]
        },
        {
            "type": "instagram_story_reply",
            "name": "Instagram Story Reply",
            "description": "Triggers when someone replies to your story",
            "platform": "instagram",
            "requires": ["story_id"],
            "optional": []
        },
        {
            "type": "whatsapp_message_received",
            "name": "WhatsApp Message Received",
            "description": "Triggers when you receive a WhatsApp message",
            "platform": "whatsapp",
            "requires": [],
            "optional": ["keyword"]
        },
        {
            "type": "contact_tag_added",
            "name": "Tag Added",
            "description": "Triggers when a tag is added to a contact",
            "platform": "both",
            "requires": ["tag"],
            "optional": []
        }
    ]
    
    return {"triggers": triggers}


# GET AVAILABLE ACTION TYPES
@router.get("/actions/available")
async def get_available_actions(user: CurrentUser):
    """Get list of available action types"""
    actions = [
        {
            "type": "reply_to_comment",
            "name": "Reply to Comment",
            "description": "Reply to an Instagram comment",
            "platform": "instagram",
            "requires": ["text"]
        },
        {
            "type": "send_dm",
            "name": "Send DM",
            "description": "Send a direct message on Instagram",
            "platform": "instagram",
            "requires": ["text"],
            "optional": ["link_url", "button_title"]
        },
        {
            "type": "instagram_message",
            "name": "Send Instagram Message",
            "description": "Send a message on Instagram",
            "platform": "instagram",
            "requires": ["messageType", "text"]
        },
        {
            "type": "whatsapp_message",
            "name": "Send WhatsApp Message",
            "description": "Send a WhatsApp message",
            "platform": "whatsapp",
            "requires": ["messageType", "text"]
        },
        {
            "type": "add_tag",
            "name": "Add Tag",
            "description": "Add a tag to a contact",
            "platform": "both",
            "requires": ["tag"]
        },
        {
            "type": "remove_tag",
            "name": "Remove Tag",
            "description": "Remove a tag from a contact",
            "platform": "both",
            "requires": ["tag"]
        },
        {
            "type": "delay",
            "name": "Delay",
            "description": "Wait for a specified duration",
            "platform": "both",
            "requires": ["amount", "unit"]
        },
        {
            "type": "condition",
            "name": "Condition",
            "description": "Branch based on a condition",
            "platform": "both",
            "requires": ["variable", "operator", "value"]
        }
    ]
    
    return {"actions": actions}










