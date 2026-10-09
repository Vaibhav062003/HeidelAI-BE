import requests
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, field_validator

from core.limiter import limiter
from auth.dependencies import CurrentUser
from config.settings import CLERK_TOKEN
from database import get_mongo_db
from loguru import logger
from services.push_notification_service import process_member_device_revocation, queue_member_device_revocation
from services.entitlement_service import release_capacity, require_feature, reserve_capacity



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


class RenameOrganizationRequest(BaseModel):
    name: str

    @field_validator("name", mode="before")
    @classmethod
    def valid_name(cls, value):
        if not isinstance(value, str):
            raise ValueError("Name must be a string")
        name = " ".join(value.split())
        if not 1 <= len(name) <= 100:
            raise ValueError("Name must be 1-100 characters")
        return name


@router.patch("/organizations/{child_org_id}/name")
@limiter.limit("30/minute")
def rename_child_organization(request: Request, child_org_id: str, data: RenameOrganizationRequest, user: CurrentUser):
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Only parent workspace admins can rename children")
    try:
        db = get_mongo_db()
        parent = db.organizations.find_one({
            "org_id": user.org_id, "workspace_kind": "management", "workspace_group_id": user.org_id,
        })
        if not parent:
            raise HTTPException(status_code=403, detail="Select the parent management workspace first")
        child_query = {
            "org_id": child_org_id, "workspace_kind": "child",
            "parent_org_id": user.org_id, "workspace_group_id": user.org_id,
        }
        child = db.organizations.find_one(child_query)
        if not child:
            raise HTTPException(status_code=404, detail="Child workspace not found")
        # shortcut: request-time duplicate check; coordinate one rename at a time across admin tabs.
        siblings = db.organizations.find({"parent_org_id": user.org_id, "workspace_kind": "child"})
        if any(
            item.get("org_id") != child_org_id
            and " ".join(str(item.get("org_name") or "").split()).casefold() == data.name.casefold()
            for item in siblings
        ):
            raise HTTPException(status_code=409, detail="Another child workspace already has this name")
        url = f"{base_url}/organizations/{child_org_id}"
        response = requests.get(url, headers=clerk_headers(), timeout=15)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="Child workspace not found")
        if not response.ok:
            raise HTTPException(status_code=502, detail="Could not verify the child workspace. Retry shortly.")
        organization = response.json()
        metadata = organization.get("public_metadata") or {}
        if any(metadata.get(key) != child_query[key] for key in ("workspace_kind", "parent_org_id", "workspace_group_id")):
            raise HTTPException(status_code=409, detail="Workspace grouping is inconsistent; repair it before renaming")
        if organization.get("name") == data.name and child.get("org_name") == data.name:
            return {"org_id": child_org_id, "name": data.name}
        if organization.get("name") != data.name:
            response = requests.patch(url, headers=clerk_headers(), json={"name": data.name}, timeout=15)
            if not response.ok or response.json().get("name") != data.name:
                raise HTTPException(status_code=502, detail="Could not rename the workspace. Retry the same name.")
        result = db.organizations.update_one(child_query, {"$set": {
            "org_name": data.name, "updated_at": datetime.now(timezone.utc),
        }})
        if not result.matched_count:
            raise HTTPException(status_code=404, detail="Child workspace no longer exists in this parent")
        return {"org_id": child_org_id, "name": data.name}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Workspace rename failed for {} ({})", child_org_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Rename could not be completed. Retry the same name to synchronize.") from exc

@router.post("/invite-agent")
@limiter.limit("30/minute")
def invite_agent(request: Request, req: InviteAgentRequest, user: CurrentUser):  
    ORG_ID = user.org_id
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Admins only")
    require_feature(ORG_ID, "SEATS")
    url = f"{base_url}/organizations/{ORG_ID}/invitations"
    payload = {"email_address": str(req.email_address), "role": "org:member", "redirect_url": "https://www.heidelai.com/accept_invitation"}
    reservation_id = None
    try:
        memberships = requests.get(f"{base_url}/organizations/{ORG_ID}/memberships", headers=clerk_headers(), params={"limit": 1}, timeout=15)
        invitations = requests.get(f"{base_url}/organizations/{ORG_ID}/invitations", headers=clerk_headers(), params={"status": "pending", "limit": 1}, timeout=15)
        if not memberships.ok or not invitations.ok:
            raise HTTPException(status_code=502, detail="Clerk membership capacity could not be verified")
        used = int(memberships.json().get("total_count", len(memberships.json().get("data", [])))) + int(invitations.json().get("total_count", len(invitations.json().get("data", []))))
        reservation_id = reserve_capacity(ORG_ID, "SEATS", "seats", str(req.email_address).lower(), used)
        resp = requests.post(url, headers=clerk_headers(), json=payload)
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to invite agent")
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Clerk invitation failed for org {} ({})", ORG_ID, type(e).__name__)
        raise HTTPException(status_code=502, detail="Clerk invitation failed") from e
    finally:
        release_capacity(reservation_id)

@router.post("/revoke-invite")
@limiter.limit("30/minute")
def revoke_invite(request: Request, req: RevokeInviteRequest, user: CurrentUser):
    ORG_ID = user.org_id
    url = f"{base_url}/organizations/{ORG_ID}/invitations/{req.invitation_id}/revoke"
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Admins only")
    payload = {"requesting_user_id": user.user_id}
    try:
        resp = requests.post(url, headers=clerk_headers(), json=payload)
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("errors", [{}])[0].get("long_message", "Failed to revoke invite")
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Clerk invitation revoke failed for org {} ({})", ORG_ID, type(e).__name__)
        raise HTTPException(status_code=502, detail="Clerk invitation revoke failed") from e

@router.delete("/delete-member")
@limiter.limit("30/minute")
def delete_member(request: Request, user: CurrentUser, user_id: str = Query(...)):
    ORG_ID = user.org_id
    if user.role not in {"admin", "org:admin"}:
        raise HTTPException(status_code=403, detail="Admins only")
    url = f"{base_url}/organizations/{ORG_ID}/memberships/{user_id}"
    try:
        resp = requests.delete(url, headers=clerk_headers())
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
