from datetime import datetime
import uuid
from fastapi import APIRouter, File, Form, HTTPException, Depends, Response, Query, UploadFile
import requests
from pymongo import ReturnDocument


# internal modules
from database import get_mongo_db
from core.logger import get_logger
from schemas.models import AddPhoneNumberRequest, InitSignupRequest, InviteTeamMembersRequest, PasswordRequest, OTPRequest, SelectPlanRequest
from services.clerk_service import create_clerk_signup, create_organization, invite_users_to_organization, update_password_and_send_otp, verify_clerk_otp
from services.payment_service import create_subscription

logger = get_logger(__name__)
db = get_mongo_db()

router = APIRouter(prefix="/api/signup", tags=["Signup"])

@router.post("/init")
async def init_signup(data: InitSignupRequest):
    """
    Initialize signup process. Returns session_id.
    """
    first_name = data.first_name.strip()
    last_name = data.last_name.strip()
    email = data.email
    clerk_sign_up_id = data.clerk_signup_id
    provider = data.provider

    session_id = str(uuid.uuid4())

    existing_session = db.onboarding_sessions.find_one({"email": email, "status": {"$ne": "COMPLETED"}})
    if existing_session:
        return {"session_id": existing_session.get("session_id"), "next_step": existing_session.get("next_step"), "message": "Resuming your existing session."}


    # Store session 
    db.onboarding_sessions.insert_one({
        "session_id": session_id,
        "clerk_id": clerk_sign_up_id,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "email_verified": False,
        "status": "SESSION_CREATED",
        "next_step": "PASSWORD",
        "created_at": datetime.utcnow()
    })

    return {"session_id": session_id, "next_step": "PASSWORD"}

@router.post("/set-password")
async def set_password(data: PasswordRequest):
    """
    Check if the Signup session exists, set password for the signup session & trigger email OTP to the registered email by Clerk.
    """
    session_id = data.session_id

    # Fetch session from DB
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"status": "PASSWORD_SET_AND_OTP_SENT", "next_step": "OTP", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    
    return {"success": True, "next_step": "OTP"}

@router.post("/verify-otp")
async def verify_otp(data: OTPRequest):
    """
    Verify the OTP code sent to the user's email. If correct, finalize signup.
    """
    session_id = data.session_id

    # Update session as complete
    session = db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"status": "EMAIL_VERIFIED", "email_verified": True, "next_step": "PLAN", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )
    if not session or not session.get("clerk_id"):
        raise HTTPException(status_code=400, detail="Invalid Session")

    return {"success": True, "next_step": "PLAN"}

@router.post("/plan")
async def select_plan(data: SelectPlanRequest):
    session_id = data.session_id
    plan_id = data.plan_id

    if plan_id == "free":
        # Update session with free plan
        session = await db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": "free", "next_step": "TEAM_INVITE", "updated_at": datetime.utcnow(), "max_allowed_members": 2}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")
        
        return {"success": True, "next_step": "TEAM_INVITE"}
    
    elif plan_id in ["growth_monthly", "growth_yearly"]:
        # Update session with paid plan
        session = await db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": plan_id, "next_step": "PHONE_NUMBER", "updated_at": datetime.utcnow(), "max_allowed_members": 4}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")
        
        return {"success": True, "next_step": "PHONE_NUMBER"}
    elif plan_id in ["enterprise"]:
        # Update session with enterprise plan
        session = await db.onboarding_sessions.find_one_and_update(
            {"session_id": session_id}, 
            {"$set": {"status": "PLAN_SELECTED", "plan": plan_id, "next_step": "SCHEDULE_CALL", "updated_at": datetime.utcnow()}}, 
            return_document=ReturnDocument.AFTER
        )
        if not session:
            raise HTTPException(status_code=400, detail="Invalid Session")
        
        return {"success": True, "next_step": "SCHEDULE_CALL"}
    
    raise HTTPException(status_code=400, detail="Invalid Plan ID")

@router.post("/phone-number")
async def add_phone_number(data: AddPhoneNumberRequest):
    session_id = data.session_id
    phone_number = data.phone_number

    # Update session with phone number
    session = await db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"phone_number": phone_number, "status": "PHONE_NUMBER_ADDED", "next_step": "PAYMENT", "updated_at": datetime.utcnow()}}, 
        return_document=ReturnDocument.AFTER
    )

    if not session:
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    return {"success": True, "next_step": "PAYMENT"}

@router.post("/init-payment")
async def init_payment(data: SelectPlanRequest):
    session_id = data.session_id


    session = await db.onboarding_sessions.find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    plan_id = session.get("plan")
    customer_full_name = f"{session.get('first_name')} {session.get('last_name')}"
    customer_email = session.get("email")
    customer_phone = session.get("phone_number")

    subscription_data = await create_subscription(
        plan_id=plan_id,
        customer_full_name=customer_full_name,
        customer_email=customer_email,
        customer_phone=customer_phone
    )

    subscription_session_id = subscription_data.get("subscription_session_id")
    if not subscription_session_id:
        raise HTTPException(status_code=500, detail="Failed to initiate payment session")

    # Update session with payment session ID
    await db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"subscription_session_id": subscription_session_id, "status": "PAYMENT_INITIATED", "next_step": "TEAM_INVITE", "updated_at": datetime.utcnow(), "payment_data": subscription_data}},
    )

    return {"subscription_session_id": subscription_session_id, "next_step": "TEAM_INVITE"}

@router.post("/invite-team-members")
async def invite_team_members(data: InviteTeamMembersRequest):
    """
    Invite team members by storing their emails in the onboarding session.
    """
    session_id = data.session_id
    team_emails = data.member_emails
    org_name = data.org_name

    session = await db.onboarding_sessions.find_one({"session_id": session_id})
    if not session:
        raise HTTPException(status_code=400, detail="Invalid Session")
    
    clerk_user_id = session.get("clerk_user_id")
    max_allowed_members = session.get("max_allowed_members", 2)

    if team_emails and len(team_emails) > max_allowed_members - 1:
        team_emails = team_emails[:max_allowed_members - 1]  # Limit to max allowed members minus the owner

    # 1. Create organization
    organization = await create_organization(
        clerk_user_id=clerk_user_id,
        org_name=org_name,
        max_allowed_members=max_allowed_members
    )

    org_id = organization.get("id")

    # 2. Invite team members
    invitations = await invite_users_to_organization(
        clerk_user_id=clerk_user_id,
        org_id=org_id,
        emails=team_emails,
        role="member"
    )

    # Update session with team member emails
    session = await db.onboarding_sessions.find_one_and_update(
        {"session_id": session_id}, 
        {"$set": {"team_emails": team_emails, "status": "TEAM_MEMBERS_INVITED", "next_step": "COMPLETED", "updated_at": datetime.utcnow(), "organization_data": organization, "invitation_data": invitations}}, 
        return_document=ReturnDocument.AFTER
    )

    await db.organizations.insert_one({
        "org_id": org_id,
        "org_name": org_name,
        "owner" : {
            "clerk_user_id": clerk_user_id,
            "email": session.get("email"),
            "first_name": session.get("first_name"),
            "last_name": session.get("last_name"),
            "phone_number": session.get("phone_number")
        },
        "member_emails": team_emails,
        "wa_id" : None,
        "ig_id" : None,
        "status": "active",
        "plan": session.get("plan"),
        "max_allowed_members": max_allowed_members,
        "next_billing_date": session.get("payment_data", {}).get("next_scheduled_date"),
        "created_at": datetime.utcnow()
    })
    
    return {"success": True, "next_step": "SUCCESS"}