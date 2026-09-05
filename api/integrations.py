from loguru import logger
from fastapi import APIRouter, Request, HTTPException

from core.limiter import limiter
from database import get_mongo_db
from auth.dependencies import CurrentUser

db = get_mongo_db()

router = APIRouter(prefix="/api/integrations", tags=["Integrations"])

@router.get("/status")
@limiter.limit("30/minute")
async def get_integrations_status(request: Request, user: CurrentUser):
    """
    Fetch all the integrations status for a specific Organization using org_id
    Returns integrations with their status as true or false
    """
    try:
        org_id = user.org_id

        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        org_data = db.organizations.find_one({"org_id": org_id})

        if not org_data:
            raise HTTPException(status_code=404, detail="Organization not found")
        
        # Fetch integration status
        google_connected = False
        if org_data.get("google_id"):
            # Cross-check: verify the connection actually exists and is active
            active_conn = db.google_connections.find_one(
                {"org_id": org_id, "status": "active"},
                {"_id": 1}
            )
            if active_conn:
                google_connected = True
            else:
                # Auto-heal: clear stale google_id from org document
                db.organizations.update_one(
                    {"org_id": org_id},
                    {"$set": {"google_id": None}}
                )
                logger.warning(f"Auto-healed stale google_id for org {org_id}")

        integration_status = {
            "instagram": bool(org_data.get("ig_id")),
            "whatsapp": bool(org_data.get("wa_id")),
            "google": google_connected
        }

        return integration_status

    except Exception as e:
        logger.error(f"Error fetching integrations status for {org_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch integrations status: {str(e)}"
        )