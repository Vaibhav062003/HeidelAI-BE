import os
import uuid
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from auth.dependencies import CurrentUser
from core.limiter import limiter

router = APIRouter(prefix="/api/billing", tags=["Billing"])


class BillingProfileRequest(BaseModel):
    legalName: str = Field(min_length=2, max_length=160)
    billingEmail: EmailStr
    billingPhone: str = Field(min_length=7, max_length=20)
    countryCode: str = Field(min_length=2, max_length=2)
    address: str = Field(min_length=8, max_length=500)
    state: str = Field(min_length=2, max_length=100)
    postalCode: str = Field(min_length=3, max_length=12)
    gstin: Optional[str] = Field(default=None, max_length=15)


class CheckoutRequest(BaseModel):
    requestId: Optional[str] = None
    planId: str = Field(min_length=1, max_length=100)
    collectionMethod: Literal["CASHFREE", "BANK"]


def _base_url() -> str:
    value = os.getenv("CRM_COMMERCIAL_BASE_URL", "").strip().rstrip("/")
    if not value.startswith("https://") and not value.startswith("http://localhost"):
        raise HTTPException(status_code=503, detail="Billing service is not configured.")
    return value


async def _crm(request: Request, method: str, path: str, body: dict | None = None, authenticated: bool = True):
    headers = {"accept": "application/json"}
    if authenticated:
        authorization = request.headers.get("authorization")
        if not authorization:
            raise HTTPException(status_code=401, detail="Authentication required.")
        headers["authorization"] = authorization
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.request(method, f"{_base_url()}{path}", headers=headers, json=body)
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail="Billing service is temporarily unavailable.")
    if response.status_code >= 400:
        try:
            message = response.json().get("error")
        except ValueError:
            message = None
        raise HTTPException(status_code=response.status_code if response.status_code < 500 else 503, detail=message or "Billing request failed.")
    return response.json()


@router.get("/plans")
@limiter.limit("60/minute")
async def plans(request: Request):
    return await _crm(request, "GET", "/api/public/plans", authenticated=False)


@router.get("/account")
@limiter.limit("60/minute")
async def billing_account(request: Request, user: CurrentUser):
    return await _crm(request, "GET", "/api/customer/commercial")


@router.put("/profile")
@limiter.limit("20/minute")
async def billing_profile(request: Request, payload: BillingProfileRequest, user: CurrentUser):
    return await _crm(request, "PUT", "/api/customer/commercial", payload.model_dump(exclude_none=True))


@router.post("/trial")
@limiter.limit("5/minute")
async def start_trial(request: Request, user: CurrentUser):
    return await _crm(request, "POST", "/api/customer/trial")


@router.post("/checkout")
@limiter.limit("10/minute")
async def checkout(request: Request, payload: CheckoutRequest, user: CurrentUser):
    body = payload.model_dump(exclude_none=True)
    body["requestId"] = body.get("requestId") or str(uuid.uuid4())
    return await _crm(request, "POST", "/api/customer/checkout", body)
