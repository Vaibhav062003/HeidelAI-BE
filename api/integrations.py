from auth.dependencies import CurrentUser
from fastapi import APIRouter, Depends, HTTPException
from database import get_mongo_db
from core.logger import get_logger

logger = get_logger(__name__)

db = get_mongo_db()

router = APIRouter(prefix="/api/integrations", tags=["Integrations"])

@router.get("/status")
async def get_integrations_status(user: CurrentUser):
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
        integration_status = {
            "instagram": bool(org_data.get("ig_id")),
            "whatsapp": bool(org_data.get("wa_id")),
        }

        return integration_status

    except Exception as e:
        logger.error(f"Error fetching integrations status for {org_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch integrations status: {str(e)}"
        )