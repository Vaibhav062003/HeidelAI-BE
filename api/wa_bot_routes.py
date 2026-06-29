from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_mongo_db
from auth.dependencies import CurrentUser
from services.wa_bot_service import (
    create_wa_bot_flow,
    list_wa_bot_flows,
    get_wa_bot_flow,
    update_wa_bot_flow,
    delete_wa_bot_flow,
    publish_wa_bot_flow,
    unpublish_wa_bot_flow,
    duplicate_wa_bot_flow,
)
from loguru import logger

db = get_mongo_db()

router = APIRouter(prefix="/api/wa_bot_flows", tags=["WaBot Flows"])


# ─── Request models ───────────────────────────────────────────────────────────

class WaBotFlowBody(BaseModel):
    """
    Accept the entire flat WaBot payload from WaBotEditor.handleSave.

    The frontend spreads buildSchema() output directly into this object, so
    it contains:
        name, status, flow_type, trigger_type,
        trigger_from, trigger_event, trigger_messages,
        welcome_message, nodes, ui_config
    plus any extra fields.  We use `Any` to stay forward-compatible.
    """
    name: str = Field(..., min_length=1, max_length=255)
    model_config = {"extra": "allow"}  # accept all flat WaBot fields

    def flat_dict(self) -> dict:
        return self.model_dump()


# ─── CREATE ───────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_flow(payload: WaBotFlowBody, user: CurrentUser):
    """
    Create a new WaBot flow (always saved as draft initially).
    WaBotEditor POSTs here on first save.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await create_wa_bot_flow(
            org_id=org_id,
            name=payload.name,
            payload=payload.flat_dict(),
            created_by=user.user_id,
        )
        return {"flow": flow}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create wa_bot_flow error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create WaBot flow: {e}")


# ─── LIST ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_flows(user: CurrentUser, status: Optional[str] = None):
    """
    List all WaBot flows for the caller's organisation.
    DashboardView fetches this alongside /api/automation_flows and merges both.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flows = await list_wa_bot_flows(org_id=org_id, status=status)
        return flows  # plain list — DashboardView handles both arrays

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list wa_bot_flows error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list WaBot flows: {e}")


# ─── GET ONE ──────────────────────────────────────────────────────────────────

@router.get("/{flow_id}")
async def get_flow(flow_id: str, user: CurrentUser):
    """
    Fetch a single WaBot flow.

    WaBotEditor load useEffect calls:
        api.get(`/api/wa_bot_flows/${fIdProp}`)
        const raw  = res.data?.flow || res.data
        const data = raw?.flow_data || raw   ← falls back to raw (our flat doc)

    Since we return the flat document, `raw.flow_data` is undefined and the
    editor correctly reads `raw.welcome_message`, `raw.nodes`, `raw.ui_config`.
    """
    try:
        org_id = user.org_id
        flow = await get_wa_bot_flow(org_id=org_id, flow_id=flow_id)
        if not flow:
            raise HTTPException(status_code=404, detail="WaBot flow not found")
        return flow  # flat document — WaBotEditor reads it directly

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch WaBot flow: {e}")


# ─── UPDATE (PATCH) ───────────────────────────────────────────────────────────

@router.patch("/{flow_id}")
async def update_flow(flow_id: str, payload: WaBotFlowBody, user: CurrentUser):
    """
    Replace mutable fields of an existing WaBot flow.
    WaBotEditor PATCHes here on every subsequent save.
    """
    try:
        org_id = user.org_id
        updated = await update_wa_bot_flow(
            org_id=org_id,
            flow_id=flow_id,
            payload=payload.flat_dict(),
            updated_by=user.user_id,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="WaBot flow not found")
        return {"flow": updated}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update WaBot flow: {e}")


# ─── DELETE ───────────────────────────────────────────────────────────────────

@router.delete("/{flow_id}")
async def delete_flow(flow_id: str, user: CurrentUser):
    """Delete a WaBot flow permanently."""
    try:
        org_id = user.org_id
        deleted = await delete_wa_bot_flow(org_id=org_id, flow_id=flow_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="WaBot flow not found")
        return {"message": "WaBot flow deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete WaBot flow: {e}")


# ─── PUBLISH ──────────────────────────────────────────────────────────────────

@router.post("/{flow_id}/publish")
async def publish_flow(flow_id: str, user: CurrentUser):
    """
    Set status → "active".

    The webhook in webhook_routes.py finds active flows with:
        db[f"wa_bot_flows_{org_id}"].find_one({
            "trigger_from":  "webhook",
            "trigger_event": "message_received",
            "status":        "active",
        })

    Since the flat document already has those three fields at the root (they
    were saved there by WaBotEditor.handleSave), no field promotion is needed —
    we just flip the status.
    """
    try:
        org_id = user.org_id
        flow = await publish_wa_bot_flow(
            org_id=org_id,
            flow_id=flow_id,
            updated_by=user.user_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="WaBot flow not found")
        return {"flow": flow, "message": "WaBot flow published and live"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"publish wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to publish WaBot flow: {e}")


# ─── UNPUBLISH ────────────────────────────────────────────────────────────────

@router.post("/{flow_id}/unpublish")
async def unpublish_flow(flow_id: str, user: CurrentUser):
    """
    Revert status → "draft" so the webhook stops firing this flow.
    """
    try:
        org_id = user.org_id
        flow = await unpublish_wa_bot_flow(
            org_id=org_id,
            flow_id=flow_id,
            updated_by=user.user_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="WaBot flow not found")
        return {"flow": flow, "message": "WaBot flow unpublished"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"unpublish wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to unpublish WaBot flow: {e}")


# ─── DUPLICATE ────────────────────────────────────────────────────────────────

@router.post("/{flow_id}/duplicate")
async def duplicate_flow(flow_id: str, user: CurrentUser):
    """
    Create a server-side copy of a WaBot flow with a fresh identity.
    The copy is always created in 'draft' status.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Organization ID not found")

        flow = await duplicate_wa_bot_flow(
            org_id=org_id,
            flow_id=flow_id,
            created_by=user.user_id,
        )
        if not flow:
            raise HTTPException(status_code=404, detail="Source WaBot flow not found")
        return {"flow": flow, "message": "WaBot flow duplicated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"duplicate wa_bot_flow {flow_id} error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to duplicate WaBot flow: {e}")