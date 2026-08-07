import requests
import uuid
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr

from core.limiter import limiter
from auth.dependencies import CurrentUser
from config.settings import CLERK_TOKEN
from loguru import logger
from services.push_notification_service import process_member_device_revocation, queue_member_device_revocation



router = APIRouter(prefix="/api/clerk", tags=["clerk"])

if not CLERK_TOKEN:
    raise RuntimeError("CLERK_TOKEN_KEY environment variable not set")
base_url = "https://api.clerk.com/v1"

def clerk_headers():
    return {
        "Authorization": f"Bearer {CLERK_TOKEN}",
        "Content-Type": "application/json"
    }

class InviteAgentRequest(BaseModel):
    email_address: EmailStr
    role: str
    redirect_url:str

class RevokeInviteRequest(BaseModel):
    invitation_id: str
    requesting_user_id: str | None = None

class DeleteMemberRequest(BaseModel):
    user_id: str

class WaitList(BaseModel):
    email: str

@router.post("/invite-agent")
@limiter.limit("30/minute")
def invite_agent(request: Request, req: InviteAgentRequest, user: CurrentUser):  
    ORG_ID = user.org_id
    url = f"{base_url}/organizations/{ORG_ID}/invitations"
    payload = {"email_address": req.email_address, "role": req.role, "redirect_url": req.redirect_url}
    try:
        resp = requests.post(url, headers=clerk_headers(), json=payload)
        logger.info(f"Clerk API response: {resp.status_code}, {resp.text}")  # Add this line
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to invite agent")
            raise HTTPException(status_code=resp.status_code, detail={"error": detail, "details": data})
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "details": str(e)})

@router.post("/revoke-invite")
@limiter.limit("30/minute")
def revoke_invite(request: Request, req: RevokeInviteRequest, user: CurrentUser):
    ORG_ID = user.org_id
    url = f"{base_url}/organizations/{ORG_ID}/invitations/{req.invitation_id}/revoke"
    payload = {"requesting_user_id": req.requesting_user_id}
    try:
        resp = requests.post(url, headers=clerk_headers(), json=payload)
        logger.info(f"Clerk API response: {resp.status_code}, {resp.text}")
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to revoke invite")
            raise HTTPException(status_code=resp.status_code, detail={"error": detail, "details": data})
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "details": str(e)})

@router.delete("/delete-member")
@limiter.limit("30/minute")
def delete_member(request: Request, user: CurrentUser, user_id: str = Query(...)):
    ORG_ID = user.org_id
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    url = f"{base_url}/organizations/{ORG_ID}/memberships/{user_id}"
    try:
        resp = requests.delete(url, headers=clerk_headers())
        logger.info(f"Clerk API response: {resp.status_code}, {resp.text}")
        data = resp.json()
        if resp.status_code == 404:
            detail = data.get("errors", [{}])[0].get("long_message", "Member not found")
            raise HTTPException(status_code=404, detail={"error": detail, "details": data})
        if not resp.ok:
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to delete member")
            raise HTTPException(status_code=resp.status_code, detail={"error": detail, "details": data})
        revocation_id = queue_member_device_revocation(
            org_id=ORG_ID,
            user_id=user_id,
            revocation_id=f"api_{uuid.uuid4().hex}",
        )
        process_member_device_revocation(revocation_id)
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "details": str(e)})

@router.post("/waitlist")
@limiter.limit("30/minute")
def waitlist_enroll(request: Request, email: WaitList):
    url = f"{base_url}/waitlist_entries"
    payload = {
        "email_address": email.email,
        "notify": True
    }

    try:
        resp = requests.post(url, headers=clerk_headers(), json=payload)
        logger.info(f"Clerk API response: {resp.status_code}, {resp.text}")

        data = resp.json()

        # Clerk returns 201 on success
        if resp.status_code not in (200, 201):
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to enroll in waitlist")
            raise HTTPException(status_code=resp.status_code, detail={"error": detail, "details": data})

        return {
            "status": "success",
            "message": "You have been added to the waitlist.",
            "data": data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": "Internal server error", "details": str(e)})
