import hmac
import re

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from config import settings
from services.sales_learning_service import (
    approve_sales_learning_policy,
    internal_learning_report,
    internal_learning_reports,
)
from services.utils import serialize_mongo


router = APIRouter(prefix="/api/internal/sales-learning", tags=["Internal sales learning"])
INTERNAL_ID = r"^[A-Za-z0-9_-]+$"


class LearningReportsPayload(BaseModel):
    org_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("org_ids")
    @classmethod
    def valid_org_ids(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not value or len(value) > 120 or not re.fullmatch(INTERNAL_ID, value) for value in cleaned):
            raise ValueError("Each organization ID must be between 1 and 120 characters")
        return cleaned


class ApprovePolicyPayload(BaseModel):
    org_id: str = Field(min_length=1, max_length=120, pattern=INTERNAL_ID)
    actor_user_id: str = Field(min_length=1, max_length=120, pattern=INTERNAL_ID)


def _require_internal_secret(value: str) -> None:
    expected = str(settings.X_INTERNAL_SECRET or "")
    if not expected or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Unauthorized internal access")


@router.get("/report")
async def sales_learning_report(
    org_id: str = Query(min_length=1, max_length=120, pattern=INTERNAL_ID),
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
):
    _require_internal_secret(x_internal_secret)
    return serialize_mongo(await internal_learning_report(org_id))


@router.post("/reports")
async def sales_learning_reports(
    payload: LearningReportsPayload,
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
):
    _require_internal_secret(x_internal_secret)
    return serialize_mongo({"reports": await internal_learning_reports(payload.org_ids)})


@router.post("/policies/{policy_id}/approve")
async def approve_policy(
    policy_id: str,
    payload: ApprovePolicyPayload,
    x_internal_secret: str = Header(default="", alias="X-Internal-Secret"),
):
    _require_internal_secret(x_internal_secret)
    if not policy_id or len(policy_id) > 80 or not re.fullmatch(INTERNAL_ID, policy_id):
        raise HTTPException(status_code=422, detail="Invalid policy ID")
    try:
        approval = await approve_sales_learning_policy(payload.org_id, policy_id, payload.actor_user_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_mongo(approval)
