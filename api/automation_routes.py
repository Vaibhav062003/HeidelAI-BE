from typing import Optional, Any
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
)
from loguru import logger

db = get_mongo_db()

router = APIRouter(prefix="/api/automation_flows", tags=["Automation Flows"])


# ─── Request / Response Models ────────────────────────────────────────────────

class CreateFlowRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    # WaBotEditor sends the entire nested schema here
    flow_data: Optional[Any] = None
    trigger_type: Optional[str] = None


class UpdateFlowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    flow_data: Optional[Any] = None
    status: Optional[str] = None
    trigger_type: Optional[str] = None


# ─── CREATE ───────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_flow(payload: CreateFlowRequest, user: CurrentUser):
    """
    Create a new automation flow (always saved as draft).
    WaBotEditor POSTs here on first save.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await create_automation_flow(
            org_id=org_id,
            name=payload.name,
            description=payload.description,
            flow_data=payload.flow_data,
            trigger_type=payload.trigger_type,
            created_by=user.user_id,
        )
        return {"flow": flow}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating automation flow: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create automation flow: {e}")


# ─── LIST ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_flows(user: CurrentUser, status: Optional[str] = None):
    """
    List all automation flows for the caller's organisation.
    Optional ?status=draft|published|archived filter.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flows = await list_automation_flows(org_id=org_id, status=status)
        return flows  # DashboardView expects a plain list (array)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing automation flows: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list automation flows: {e}")


# ─── GET ONE ──────────────────────────────────────────────────────────────────

@router.get("/{flow_id}")
async def get_flow(flow_id: str, user: CurrentUser):
    """
    Fetch a single flow by its flow_id.
    WaBotEditor calls this when opening an existing flow to hydrate the canvas.
    """
    try:
        org_id = user.org_id
        flow = await get_automation_flow(org_id=org_id, flow_id=flow_id)
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return flow  # WaBotEditor reads response.data directly

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching automation flow {flow_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch automation flow: {e}")


# ─── UPDATE (PATCH) ───────────────────────────────────────────────────────────

@router.patch("/{flow_id}")
async def update_flow(flow_id: str, payload: UpdateFlowRequest, user: CurrentUser):
    """
    Partial update of a flow.
    WaBotEditor PATCHes here on subsequent saves and on unpublish.
    """
    try:
        org_id = user.org_id
        updated = await update_automation_flow(
            org_id=org_id,
            flow_id=flow_id,
            name=payload.name,
            description=payload.description,
            flow_data=payload.flow_data,
            status=payload.status,
            trigger_type=payload.trigger_type,
            updated_by=user.user_id,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="Flow not found")
        return {"flow": updated}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating automation flow {flow_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update automation flow: {e}")


# ─── DELETE ───────────────────────────────────────────────────────────────────

@router.delete("/{flow_id}")
async def delete_flow(flow_id: str, user: CurrentUser):
    """
    Permanently delete a flow.
    DashboardView calls this from the ⋯ menu.
    """
    try:
        org_id = user.org_id
        deleted = await delete_automation_flow(org_id=org_id, flow_id=flow_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Flow not found")
        return {"message": "Flow deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting automation flow {flow_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete automation flow: {e}")


# ─── PUBLISH ──────────────────────────────────────────────────────────────────

@router.post("/{flow_id}/publish")
async def publish_flow(flow_id: str, user: CurrentUser):
    """
    Publish a flow.
    Sets status → "published" / "active" AND flattens the WaBot fields
    (trigger_from, trigger_event, trigger_messages, welcome_message, nodes)
    to the document root so the webhook handler can find them with a simple query.
    """
    try:
        org_id = user.org_id
        flow = await publish_automation_flow(
            org_id=org_id,
            flow_id=flow_id,
            updated_by=user.user_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return {"flow": flow, "message": "Flow published successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error publishing automation flow {flow_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to publish automation flow: {e}")


# ─── UNPUBLISH ────────────────────────────────────────────────────────────────

@router.post("/{flow_id}/unpublish")
async def unpublish_flow(flow_id: str, user: CurrentUser):
    """
    Unpublish a flow — sets status back to draft and removes the
    top-level webhook fields so it stops being triggered.
    """
    try:
        org_id = user.org_id
        flow = await unpublish_automation_flow(
            org_id=org_id,
            flow_id=flow_id,
            updated_by=user.user_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="Flow not found")
        return {"flow": flow, "message": "Flow unpublished successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unpublishing automation flow {flow_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to unpublish automation flow: {e}")