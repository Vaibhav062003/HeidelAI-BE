from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services.payment_service import subscription_status_generator
from services.wa_service import bulk_job_status_generator
import json

router = APIRouter(prefix="/sse", tags=["SSE"])

@router.get("/stream-status/{sub_id}")
async def stream_payment_status(sub_id: str):
    """
    SSE Endpoint consumed by the frontend.
    """
    return StreamingResponse(
        subscription_status_generator(sub_id), 
        media_type="text/event-stream"
    )

@router.get("/bulk-status/{job_id}")
async def stream_bulk_status(job_id: str):
    """
    SSE Endpoint to track bulk job status.
    """
    return StreamingResponse(
        bulk_job_status_generator(job_id),
        media_type="text/event-stream"
    )