from fastapi import APIRouter, Request

from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.sales_case_service import get_active_sales_case
from services.utils import serialize_mongo
from services.work_team_service import require_conversation_access


router = APIRouter(prefix="/api/sales-cases", tags=["Sales cases"])


@router.get("/conversations/{conversation_id}")
@limiter.limit("60/minute")
async def conversation_sales_case(
    request: Request,
    conversation_id: str,
    user: CurrentUser,
):
    await require_conversation_access(
        user.org_id, conversation_id, user.user_id, user.role,
    )
    case = await get_active_sales_case(user.org_id, conversation_id)
    if case:
        case.pop("org_id", None)
        case.pop("processed_events", None)
    return {"case": serialize_mongo(case) if case else None}
