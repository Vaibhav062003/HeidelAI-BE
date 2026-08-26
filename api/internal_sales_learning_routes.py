import hmac

from fastapi import APIRouter, Header, HTTPException, Query

from config import settings
from services.sales_learning_service import internal_learning_report
from services.utils import serialize_mongo


router = APIRouter(prefix="/api/internal/sales-learning", tags=["Internal sales learning"])


@router.get("/report")
async def sales_learning_report(
    org_id: str = Query(min_length=1, max_length=120),
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
):
    expected = str(settings.X_INTERNAL_SECRET or "")
    if not expected or not hmac.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=401, detail="Unauthorized internal access")
    return serialize_mongo(await internal_learning_report(org_id))
