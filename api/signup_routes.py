import uuid
from datetime import datetime
from pymongo import ReturnDocument
from fastapi import APIRouter, HTTPException, Depends, Response, Cookie, Request

# internal modules
from core.limiter import limiter
from database import get_mongo_db
from schemas.models import InitSignupRequest, InviteTeamMembersRequest, SelectPlanRequest, SetPasswordRequest, VerifyOtpRequest
from services.clerk_service import create_clerk_signup, create_organization, invite_users_to_organization, update_password_and_send_otp, verify_clerk_otp
from services.commercial_mode import billing_enabled

db = get_mongo_db()

router = APIRouter(prefix="/api/signup", tags=["Signup"])

# --- DEPENDENCY ---
async def get_onboarding_session(signup_session: str | None = Cookie(default=None)):
    """
    Dependency to retrieve and validate the session ID from the HttpOnly cookie.
    """
    if not signup_session:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please start over.")
    
    # Optional: You can fetch the session object here to save DB calls in routes
    # For now, we just return the ID to keep changes minimal
    return signup_session

@router.post("/init")
@limiter.limit("30/minute")
async def init_signup(request: Request, data: InitSignupRequest, response: Response):
    """
    Initialize signup process. Returns session_id.
    """
    first_name = data.first_name.strip()
    last_name = data.last_name.strip() if data.last_name else ""
    email = data.email
    clerk_sign_up_id = data.clerk_signup_id
    clerk_user_id = data.clerk_user_id
    provider = data.provider


    existing_session = db.onboarding_sessions.find_one({"email": email})
    if existing_session:
        session_id = existing_session.get("session_id")
        next_step = existing_session.get("next_step")
        if next_step in {"PHONE_NUMBER", "PAYMENT", "SCHEDULE_CALL"}:
            next_step = "PLAN"
            db.onboarding_sessions.update_one({"session_id": session_id}, {"$set": {"next_step": next_step, "updated_at": datetime.utcnow()}})
    else:
        session_id = str(uuid.uuid4())
        next_step = "PASSWORD" if provider == "email" else "PLAN"

        # Store session 
        db.onboarding_sessions.insert_one({
            "session_id": session_id,
            "clerk_id": clerk_sign_up_id,
            "clerk_user_id": clerk_user_id,
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "provider" : provider,
            "email_verified": False if provider == "email" else True,
            "status": "SESSION_CREATED",
            "next_step": next_step,
            "created_at": datetime.utcnow()
        })

    response.set_cookie(
        key="signup_session",
        value=session_id,
        httponly=True,   # JavaScript cannot access this
        max_age=3600,    # 1 hour expiration
        secure=True,    # Set to True in Production (HTTPS)
        samesite="lax",   
        domain=".heidelai.com"
    )

    return {"next_step": next_step}

@router.post("/set-password")
@limiter.limit("30/minute")
async def set_password(request: Request, data: SetPasswordRequest, session_id: str = Depends(get_onboarding_session)):
    """
    Check if the Signup session exists, set password for the signup session & trigger email OTP to the registered email by Clerk.
    """
    # Fetch session from DB
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {
            "clerk_user_id": data.clerk_user_id, 
            "status": "PASSWORD_SET_AND_OTP_SENT", 
            "next_step": "OTP", 
            "updated_at": datetime.utcnow()
        }}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    
    return {"success": True, "next_step": "OTP"}

@router.post("/verify-otp")
@limiter.limit("30/minute")
async def verify_otp(request: Request, data: VerifyOtpRequest, session_id: str = Depends(get_onboarding_session)):
    """
    Verify the OTP code sent to the user's email. If correct, finalize signup.
    """
    # Update session as complete
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {
            "clerk_user_id": data.clerk_user_id, 
            "status": "EMAIL_VERIFIED", 
            "email_verified": True, 
            "next_step": "PLAN", 
            "updated_at": datetime.utcnow()
        }}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")

    return {"success": True, "next_step": "PLAN"}

@router.post("/plan")
@limiter.limit("30/minute")
async def select_plan(request: Request, data: SelectPlanRequest, session_id: str = Depends(get_onboarding_session)):
    plan_id = data.plan_id

    if plan_id in {"trial", "free"}:
        workspace_plan = "trial_pending" if billing_enabled() else "free"
        max_allowed_members = 1 if billing_enabled() else None
        session = db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {
                "status": "PLAN_SELECTED", 
                "plan": workspace_plan,
                "next_step": "SUCCESS", 
                "updated_at": datetime.utcnow(), 
                "max_allowed_members": max_allowed_members
            }}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")

        org_name = f"{session.get('first_name')}'s Organization"
        owner = {
            "clerk_user_id": session.get("clerk_user_id"),
            "owner_email": session.get("email"),
            "owner_first_name": session.get("first_name"),
            "owner_last_name": session.get("last_name"),
            "owner_phone_number": session.get("phone_number"),
        }

        organization = await create_organization(
            org_name=org_name,
            max_allowed_members=max_allowed_members,
            session_id=session_id,
            owner=owner,
            plan=workspace_plan
        )

        if not organization:
            raise HTTPException(status_code=500, detail="Failed to create organization")
        
        return {"success": True, "next_step": "SUCCESS", "org_id" : organization.get("id")}
    
    raise HTTPException(status_code=410, detail="Create a trial workspace, then choose a published plan in Plan & Billing.")

@router.post("/init-payment")
@limiter.limit("30/minute")
async def init_payment(request: Request, session_id: str = Depends(get_onboarding_session)):
    raise HTTPException(status_code=410, detail="Legacy signup payments are closed. Use Plan & Billing for bank transfer or Cashfree.")

@router.post("/init-payment/retry")
@limiter.limit("30/minute")
async def retry_init_payment(request: Request, session_id: str = Depends(get_onboarding_session)):
    raise HTTPException(status_code=410, detail="Legacy signup payments are closed. Use Plan & Billing for bank transfer or Cashfree.")

@router.get("/subscription/status")
@limiter.limit("60/minute")
async def check_subscription_status(request: Request, session_id: str = Depends(get_onboarding_session)):
    subscription = db.subscriptions.find_one(
        {"session_id": session_id}, 
        {"status": 1, "_id": 0},
        sort=[("created_at", -1)],
    )
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found for this session")
    
    return {"status": subscription.get("status")}

@router.post("/invite-team-members")
@limiter.limit("30/minute")
async def invite_team_members(request: Request, data: InviteTeamMembersRequest, session_id: str = Depends(get_onboarding_session)):
    """
    Invite team members by storing their emails in the onboarding session.
    """
    team_emails = data.member_emails
    org_name = data.org_name

    session = db.onboarding_sessions.find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=400, detail="Invalid Session")

    organization = db.organizations.find_one(
        {"session_id": session_id}, 
        {"org_id": 1, "_id": 0}
    )
    
    clerk_user_id = session.get("clerk_user_id")
    max_allowed_members = session.get("max_allowed_members")
    org_id = organization.get("org_id")

    if max_allowed_members is not None and team_emails and len(team_emails) > max_allowed_members - 1:
        team_emails = team_emails[:max_allowed_members - 1]  # Limit to max allowed members minus the owner

    # 2. Invite team members
    invitations = await invite_users_to_organization(
        clerk_user_id=clerk_user_id,
        org_id=org_id,
        emails=team_emails,
        role="member"
    )

    # Update session with team member emails
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"team_emails": team_emails, "status": "TEAM_MEMBERS_INVITED", "next_step": "COMPLETED", "updated_at": datetime.utcnow(), "organization_data": organization, "invitation_data": invitations}}, 
        return_document=ReturnDocument.AFTER
    )

    db.organizations.update_one(
        {"org_id": org_id},
        {"$set": {
            "org_name": org_name,
            "member_emails": team_emails,
            "next_billing_date": session.get("payment_data", {}).get("next_scheduled_date"),
            "updated_at": datetime.utcnow()
        }
    })
    
    return {"success": True, "next_step": "SUCCESS"}
