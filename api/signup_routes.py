from datetime import datetime
import uuid
from fastapi import APIRouter, HTTPException, Depends, Response, Cookie, Request
import requests
from pymongo import ReturnDocument


# internal modules
from database import get_mongo_db
from core.logger import get_logger
from schemas.models import InitPaymentRequest, InitSignupRequest, InviteTeamMembersRequest, SelectPlanRequest, SetPasswordRequest, VerifyOtpRequest
from services.clerk_service import create_clerk_signup, create_organization, invite_users_to_organization, update_password_and_send_otp, verify_clerk_otp
from services.payment_service import create_subscription

logger = get_logger(__name__)
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
async def init_signup(data: InitSignupRequest, response: Response, request: Request):
    """
    Initialize signup process. Returns session_id.
    """
    logger.info(f"Incoming Request: {request.cookies}")

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
async def set_password(data: SetPasswordRequest, request: Request, session_id: str = Depends(get_onboarding_session)):
    """
    Check if the Signup session exists, set password for the signup session & trigger email OTP to the registered email by Clerk.
    """
    logger.info(f"Incoming Request: {request.cookies}")

    # Fetch session from DB
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"clerk_user_id": data.clerk_user_id, "status": "PASSWORD_SET_AND_OTP_SENT", "next_step": "OTP", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    
    return {"success": True, "next_step": "OTP"}

@router.post("/verify-otp")
async def verify_otp(data: VerifyOtpRequest, session_id: str = Depends(get_onboarding_session)):
    """
    Verify the OTP code sent to the user's email. If correct, finalize signup.
    """
    # Update session as complete
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"clerk_user_id": data.clerk_user_id, "status": "EMAIL_VERIFIED", "email_verified": True, "next_step": "PLAN", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")

    return {"success": True, "next_step": "PLAN"}

@router.post("/plan")
async def select_plan(data: SelectPlanRequest, session_id: str = Depends(get_onboarding_session)):
    plan_id = data.plan_id

    if plan_id == "free":
        # Update session with free plan
        session = db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": "free", "next_step": "SUCCESS", "updated_at": datetime.utcnow(), "max_allowed_members": 2}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")

        org_name = f"{session.get('first_name')}'s Organization"
        max_allowed_members = 2
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
            plan="free"
        )

        if not organization:
            raise HTTPException(status_code=500, detail="Failed to create organization")
        
        return {"success": True, "next_step": "SUCCESS", "org_id" : organization.get("id")}
    
    elif plan_id in ["growth_monthly", "growth_yearly"]:
        # Update session with paid plan
        session = db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": plan_id, "next_step": "PHONE_NUMBER", "updated_at": datetime.utcnow(), "max_allowed_members": 4}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")
        
        return {"success": True, "next_step": "PHONE_NUMBER"}
    elif plan_id in ["enterprise"]:
        # Update session with enterprise plan
        session = db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": plan_id, "next_step": "SCHEDULE_CALL", "updated_at": datetime.utcnow()}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")
        
        return {"success": True, "next_step": "SCHEDULE_CALL"}
    
    raise HTTPException(status_code=400, detail="Invalid Plan ID")


@router.post("/init-payment")
async def init_payment(data: InitPaymentRequest, session_id: str = Depends(get_onboarding_session)):
    logger.info(f"Initiating payment for phone number: {data.phone_number}")
    phone_number = data.phone_number

    # Update session with phone number
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"phone_number": phone_number, "status": "PHONE_NUMBER_ADDED", "next_step": "PAYMENT", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )

    if not session:
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    plan_id = session.get("plan")
    customer_full_name = f"{session.get('first_name')} {session.get('last_name')}"
    customer_email = session.get("email")
    customer_phone = session.get("phone_number")
    clerk_user_id = session.get("clerk_user_id")

    subscription_data = await create_subscription(
        plan_id=plan_id,
        customer_full_name=customer_full_name,
        customer_email=customer_email,
        customer_phone=customer_phone
    )

    subscription_session_id = subscription_data.get("subscription_session_id") # ID for frontend to initialise payment
    cf_subscription_id = subscription_data.get("cf_subscription_id") # ID from Cashfree
    subscription_id = subscription_data.get("subscription_id") # Our internal ID, matched on frontend payment status page

    if not subscription_session_id:
        raise HTTPException(status_code=500, detail="Failed to initiate payment session")

    db.subscriptions.insert_one({
        "session_id": session_id, # session id from onboarding_sessions collection
        "cf_subscription_id": cf_subscription_id, # Cashfree ID
        "subscription_id": subscription_id, # Our internal ID
        "clerk_user_id": clerk_user_id, # clerk user id
        "plan_id": plan_id,
        "status": "INITIALIZED", # Waiting for user to pay on frontend
        "created_at": datetime.utcnow()
    })

    # Update session with payment session ID
    db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"subscription_session_id": subscription_session_id, "status": "PAYMENT_INITIATED", "next_step": "PAYMENT", "updated_at": datetime.utcnow(), "payment_data": subscription_data}},
    )

    return {"subscription_session_id": subscription_session_id, "next_step": "TEAM_INVITE"}

@router.post("/invite-team-members")
async def invite_team_members(data: InviteTeamMembersRequest, session_id: str = Depends(get_onboarding_session)):
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
    max_allowed_members = session.get("max_allowed_members", 2)
    org_id = organization.get("org_id")

    if team_emails and len(team_emails) > max_allowed_members - 1:
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