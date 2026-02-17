from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import Optional
from auth.dependencies import CurrentUser
from services.dashboard_service import aggregate_dashboard_stats
from core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Dashboard Routes"])

@router.get("/api/dashboard/stats")
async def get_dashboard_stats(
    user: CurrentUser,
    target_date: Optional[str] = Query(None, description="ISO date string for single day stats"),
    start_date: Optional[str] = Query(None, description="ISO start date string for range"),
    end_date: Optional[str] = Query(None, description="ISO end date string for range")
):
    """
    Get full dashboard statistics.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        stats_data = await aggregate_dashboard_stats(
            org_id=org_id,
            target_date_str=target_date,
            start_date_str=start_date,
            end_date_str=end_date
        )

        if not stats_data:
            return {}

        return stats_data

    except Exception as e:
        logger.error(f"Error fetching dashboard stats for {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard stats")

