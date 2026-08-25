from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from auth.dependencies import CurrentUser
from core.limiter import limiter
from services.entitlement_service import feature_access, require_feature
from services.stock_service import (
    create_stock_change,
    get_stock,
    link_stock_products,
    list_stock,
    list_stock_changes,
    retry_stock_change,
    review_stock_change,
    sync_all_stock,
)


router = APIRouter(prefix="/api/stock", tags=["Stock"])
ADMIN_ROLES = {"admin", "org:admin"}


def require_stock_access(user: CurrentUser, *, write: bool = False) -> None:
    access = feature_access(user.org_id, "TALLY")
    if access["state"] == "locked" or (write and access["state"] != "active"):
        require_feature(user.org_id, "TALLY")


class StockAllocation(BaseModel):
    godown: str = Field(default="Main Location", min_length=1, max_length=120)
    batch: str = Field(default="Primary Batch", min_length=1, max_length=120)
    quantity: float = Field(ge=0, le=10_000_000)


class StockChangeValues(BaseModel):
    price: float | None = Field(default=None, ge=0, le=1_000_000_000)
    description: str | None = Field(default=None, max_length=2000)
    quantity: float | None = Field(default=None, ge=0, le=10_000_000)
    allocations: list[StockAllocation] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def allocations_match_quantity(self):
        if self.allocations and self.quantity is None:
            raise ValueError("Set total quantity when changing locations or batches")
        if self.allocations and abs(sum(row.quantity for row in self.allocations) - float(self.quantity)) > 0.001:
            raise ValueError("Location and batch quantities must equal total quantity")
        return self


class StockChangePayload(BaseModel):
    version: str = Field(min_length=1, max_length=500)
    changes: StockChangeValues
    reason: str = Field(default="", max_length=500)


class ReviewPayload(BaseModel):
    reason: str = Field(default="", max_length=500)


class LinkPayload(BaseModel):
    sheet_product_id: str = Field(min_length=1, max_length=120)
    tally_master_id: str = Field(min_length=1, max_length=120)


@router.get("")
@limiter.limit("60/minute")
async def stock_index(
    request: Request, user: CurrentUser, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
    q: str = Query("", max_length=200), source: str = Query("", max_length=20),
    status: str = Query("", max_length=30), low_stock: float | None = Query(None, ge=0),
    sort: str = Query("sku", max_length=20), direction: Literal["asc", "desc"] = "asc",
):
    require_stock_access(user)
    return await list_stock(
        user.org_id, page=page, page_size=page_size, query=q, source=source,
        status=status, low_stock=low_stock, sort=sort, direction=direction,
    )


@router.get("/changes")
@limiter.limit("60/minute")
async def stock_changes(
    request: Request, user: CurrentUser, status: str = Query("", max_length=30),
    product_id: str = Query("", max_length=120), limit: int = Query(100, ge=1, le=500),
):
    require_stock_access(user)
    return {"changes": await list_stock_changes(user.org_id, status, product_id, limit)}


@router.post("/sync")
@limiter.limit("10/minute")
async def refresh_stock(request: Request, user: CurrentUser):
    require_stock_access(user, write=True)
    return await sync_all_stock(user.org_id, user.user_id)


@router.put("/tally-link")
@limiter.limit("20/minute")
async def link_stock(request: Request, payload: LinkPayload, user: CurrentUser):
    require_stock_access(user, write=True)
    if user.role not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only organization admins can link stock sources")
    return await link_stock_products(user.org_id, payload.sheet_product_id, payload.tally_master_id, user.user_id)


@router.get("/{product_id}")
@limiter.limit("60/minute")
async def stock_detail(
    request: Request, product_id: str, user: CurrentUser,
    from_date: str = Query("", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query("", pattern=r"^$|^\d{4}-\d{2}-\d{2}$"),
):
    require_stock_access(user)
    return await get_stock(user.org_id, product_id, from_date, to_date)


@router.post("/{product_id}/changes")
@limiter.limit("20/minute")
async def propose_stock_change(request: Request, product_id: str, payload: StockChangePayload, user: CurrentUser):
    require_stock_access(user, write=True)
    values = payload.changes.model_dump(exclude_none=True)
    return await create_stock_change(user.org_id, user.model_dump(), product_id, payload.version, values, payload.reason)


@router.post("/changes/{change_id}/approve")
@limiter.limit("20/minute")
async def approve_stock_change(request: Request, change_id: str, payload: ReviewPayload, user: CurrentUser):
    require_stock_access(user, write=True)
    return await review_stock_change(user.org_id, change_id, user.user_id, user.role, True, payload.reason)


@router.post("/changes/{change_id}/reject")
@limiter.limit("20/minute")
async def reject_stock_change(request: Request, change_id: str, payload: ReviewPayload, user: CurrentUser):
    require_stock_access(user, write=True)
    return await review_stock_change(user.org_id, change_id, user.user_id, user.role, False, payload.reason)


@router.post("/changes/{change_id}/retry")
@limiter.limit("20/minute")
async def retry_change(request: Request, change_id: str, user: CurrentUser):
    require_stock_access(user, write=True)
    return await retry_stock_change(user.org_id, change_id, user.user_id, user.role)
