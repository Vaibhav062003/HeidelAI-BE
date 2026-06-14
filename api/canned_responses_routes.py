from fastapi import APIRouter, Request, HTTPException
from schemas.models import CannedResponse, CannedResponseOut, UpdateCannedResponseRequest

from core.limiter import limiter
from auth.dependencies import CurrentUser
from services.canned_responses_service import (
    get_canned_responses_for_org,
    create_canned_response_for_org,
    delete_canned_response_for_org,
    update_canned_response_for_org
)

router = APIRouter(prefix="/api/canned_responses", tags=["Canned Responses"])

@router.get("")
@limiter.limit("30/minute")
async def get_canned_responses(request: Request, user: CurrentUser):
    """
    Retrieve canned responses for the organization.
    """
    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    canned_responses = await get_canned_responses_for_org(org_id)

    return {"canned_responses": canned_responses}

@router.post("")
@limiter.limit("30/minute")
async def create_canned_response(request: Request, user: CurrentUser, body: CannedResponse):
    """
    Create a single canned respones for the organization.
    """

    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    canned_responses = await create_canned_response_for_org(org_id, body)

    return {"canned_responses": canned_responses}

@router.delete("/{shortcut_id}")
@limiter.limit("30/minute")
async def delete_canned_response(request: Request, user: CurrentUser, shortcut_id: str):
    """
    Delete a single canned response for the organization.
    """

    org_id = user.org_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")
    
    canned_response = await delete_canned_response_for_org(org_id, shortcut_id)
    
    return {"message": f"Canned response '{canned_response['shortcut']}' deleted successfully."}

@router.put("/{shortcut_id}", response_model=CannedResponseOut)
@limiter.limit("30/minute")
async def update_canned_response(
    request: Request,
    shortcut_id: str,
    payload: UpdateCannedResponseRequest,
    user: CurrentUser, 
):
    """
    Updates the response text for a canned response shortcut.
    """
    org_id = user.org_id

    if not org_id:
        raise HTTPException(status_code=400, detail="Invalid organization ID")

    return await update_canned_response_for_org(
        org_id=org_id,
        shortcut_id=shortcut_id,
        new_response=payload.response,
    )