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


class UpdateFlowRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=500)
    flow_data: Optional[FlowDocument] = None
    status: Optional[str] = None


# =============================================================================
# FLOW CRUD
# =============================================================================

@router.post("")
async def create_flow(payload: CreateFlowRequest, user: CurrentUser):
    """Create a new automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

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
    """List all automation flows for the organization"""
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


@router.patch("/{flow_id}")
async def update_flow(flow_id: str, payload: UpdateFlowRequest, user: CurrentUser):
    """Update an existing automation flow"""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        if payload.flow_data:
            is_valid, error_message = validate_flow_structure(payload.flow_data.model_dump())
            if not is_valid:
                raise HTTPException(status_code=400, detail=f"Invalid flow structure: {error_message}")

        update_data = {}
        if payload.name is not None:
            update_data["name"] = payload.name
        if payload.description is not None:
            update_data["description"] = payload.description
        if payload.flow_data is not None:
            update_data["flow_data"] = payload.flow_data.model_dump()
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


# =============================================================================
# PUBLISH / UNPUBLISH
# =============================================================================

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


# =============================================================================
# EXECUTION HISTORY
# =============================================================================

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
            executions_collection.find({"flow_id": flow_id, "org_id": org_id})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )

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


# =============================================================================
# GOOGLE SHEET AUTOMATION — SENT NUMBERS STATS
# =============================================================================

@router.get("/{flow_id}/sheet-stats")
async def get_google_sheet_flow_stats(flow_id: str, user: CurrentUser):
    """
    Get statistics for a google_sheet automation flow:
    how many unique phone numbers have been messaged.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        sent_count = db["google_sheet_sent_numbers"].count_documents(
            {"org_id": org_id, "flow_id": flow_id}
        )

        recent_sent = list(
            db["google_sheet_sent_numbers"]
            .find({"org_id": org_id, "flow_id": flow_id}, {"_id": 0, "phone": 1, "sent_at": 1})
            .sort("sent_at", -1)
            .limit(20)
        )

        for record in recent_sent:
            if record.get("sent_at") and hasattr(record["sent_at"], "isoformat"):
                record["sent_at"] = record["sent_at"].isoformat()

        return {
            "flow_id": flow_id,
            "total_sent": sent_count,
            "recent_sent": recent_sent,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching sheet stats for flow {flow_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# FOLLOW-UP MANAGEMENT ENDPOINTS (WhatsApp Scheduler)
# =============================================================================

@router.get("/{flow_id}/followups")
async def get_flow_followups(
    flow_id: str,
    user: CurrentUser,
    status: Optional[str] = None,
    limit: int = 50,
):
    """List scheduled follow-up jobs for a specific flow."""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        from services.automation_scheduler import list_followups_for_flow
        followups = list_followups_for_flow(org_id, flow_id, status=status, limit=limit)

        return {"followups": followups, "total": len(followups)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching followups for flow {flow_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{flow_id}/followups/{schedule_id}")
async def cancel_flow_followup(flow_id: str, schedule_id: str, user: CurrentUser):
    """Cancel a specific pending follow-up job."""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        from services.automation_scheduler import cancel_followup
        cancelled = cancel_followup(schedule_id=schedule_id, org_id=org_id)

        if not cancelled:
            raise HTTPException(
                status_code=404,
                detail="Follow-up not found or already in terminal state (completed/failed/cancelled)"
            )

        return {"message": f"Follow-up {schedule_id} cancelled successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error cancelling followup {schedule_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/followups/pending")
async def get_pending_followups(user: CurrentUser):
    """List all pending and executing follow-up jobs for the organization."""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        from services.automation_scheduler import list_followups_for_org
        pending = list_followups_for_org(org_id, status="pending", limit=100)
        executing = list_followups_for_org(org_id, status="executing", limit=100)

        return {
            "pending": pending,
            "executing": executing,
            "total_active": len(pending) + len(executing),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching pending followups: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{flow_id}/followups/stats")
async def get_followup_stats(flow_id: str, user: CurrentUser):
    """Aggregated statistics for follow-up jobs of a specific flow."""
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        pipeline = [
            {"$match": {"org_id": org_id, "flow_id": flow_id}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]

        raw = list(db["automation_scheduled_followups"].aggregate(pipeline))
        stats = {item["_id"]: item["count"] for item in raw}

        for s in ["pending", "executing", "completed", "failed", "cancelled"]:
            stats.setdefault(s, 0)

        stats["total"] = sum(stats.values())
        return {"flow_id": flow_id, "stats": stats}

    except Exception as e:
        logger.error(f"Error fetching followup stats for flow {flow_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# AVAILABLE TRIGGERS & ACTIONS (discovery endpoints)
# =============================================================================

@router.get("/triggers/available")
async def get_available_triggers(user: CurrentUser):
    """Get list of available trigger types"""
    try:
        triggers_collection = db["automation_trigger_types"]

        triggers = list(
            triggers_collection.find(
                {}, {"_id": 0}
            )
        )

        return {"triggers": triggers}

    except Exception as e:
        logger.error(f"Error fetching triggers: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch triggers")


@router.get("/actions/available")
async def get_available_actions(user: CurrentUser):
    """Get list of available action types"""
    try:
        actions_collection = db["automation_action_types"]

        actions = list(
            actions_collection.find(
                {}, {"_id": 0}
            )
        )

        return {"actions": actions}

    except Exception as e:
        logger.error(f"Error fetching actions: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch actions")