from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import Optional
from auth.dependencies import CurrentUser
from services.dashboard_service import aggregate_dashboard_stats
from services.pdf_service import generate_dashboard_pdf
from core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Dashboard Routes"])

@router.get("/api/dashboard/stats/pdf")
async def get_dashboard_stats_pdf(
    user: CurrentUser,
    target_date: Optional[str] = Query(None, description="ISO date string for single day stats"),
    start_date: Optional[str] = Query(None, description="ISO start date string for range"),
    end_date: Optional[str] = Query(None, description="ISO end date string for range")
):
    """
    Download dashboard statistics as a premium PDF.
    Reuses existing stats aggregation logic and formats the output into a PDF with charts.
    """
    try:
        org_id = user.org_id
        if not org_id:
            raise HTTPException(status_code=400, detail="Invalid organization ID")

        # Reuse existing stats aggregation logic
        stats_data = await aggregate_dashboard_stats(
            org_id=org_id,
            target_date_str=target_date,
            start_date_str=start_date,
            end_date_str=end_date
        )

        if not stats_data:
            raise HTTPException(status_code=404, detail="No stats data found for the given parameters")

        # Generate PDF using the presentation layer
        pdf_buffer = generate_dashboard_pdf(stats_data, org_id)

        filename = f"HeidelAI_Dashboard_{org_id}_{datetime.now().strftime('%Y%m%d')}.pdf"
        
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating dashboard PDF for {user.org_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate dashboard PDF")

from datetime import datetime
