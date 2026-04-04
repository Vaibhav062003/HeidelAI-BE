from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException, Request

# Internal Modules
from core.limiter import limiter
from database import get_mongo_db
from auth.dependencies import CurrentUser
from services.automation_service import (
    create_automation_flow,
    list_automation_flows,
    validate_flow_structure
)
from loguru import logger

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


# CREATE FLOW ENDPOINT
@router.post("")
@limiter.limit("10/minute")
async def create_flow(request: Request, payload: CreateFlowRequest, user: CurrentUser):
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
@limiter.limit("100/minute")
async def list_flows(request: Request, user: CurrentUser, status: Optional[str] = None):
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
















