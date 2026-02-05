from datetime import datetime
from typing import TypedDict, Literal
import httpx

from config.settings import CLERK_TOKEN
from core.logger import get_logger
from database import get_mongo_db

logger = get_logger(__name__)
db = get_mongo_db()

class OwnerPayload(TypedDict):
    clerk_user_id: str
    owner_email: str
    owner_first_name: str
    owner_last_name: str
    owner_phone_number: str | None

base_url = "https://api.clerk.com/v1"

def clerk_headers():
    return {
        "Authorization": f"Bearer {CLERK_TOKEN}",
        "Content-Type": "application/json"
    }


async def create_clerk_signup(email: str):
    """
    Step 1: Create a Sign-up object in Clerk. 
    This doesn't create a user yet, just a temporary sign-up state.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/sign_ups",
            headers=clerk_headers(),
            json={"email_address": email}
        )
        if response.status_code != 200:
            raise Exception(f"Clerk Error: {response.text}")
        return response.json() # Returns object containing 'id' (sign_up_id)

async def update_password_and_send_otp(sign_up_id: str, password: str):
    """
    Step 2: Add password to the sign-up object AND trigger the OTP email.
    """
    async with httpx.AsyncClient() as client:
        # A. Update with Password
        await client.patch(
            f"{base_url}/sign_ups/{sign_up_id}",
            headers=clerk_headers(),
            json={"password": password}
        )

        # B. Trigger OTP Email (Prepare Verification)
        response = await client.post(
            f"{base_url}/sign_ups/{sign_up_id}/prepare_verification",
            headers=clerk_headers(),
            json={"strategy": "email_code"}
        )
        
        if response.status_code != 200:
            raise Exception("Failed to send OTP via Clerk")
        return True

async def verify_clerk_otp(sign_up_id: str, code: str):
    """
    Step 3: Verify the code. If correct, Clerk converts the Sign-up to a User.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{base_url}/sign_ups/{sign_up_id}/attempt_verification",
            headers=clerk_headers(),
            json={"strategy": "email_code", "code": code}
        )
        
        data = response.json()
        
        logger.info(f"Clerk OTP response: {data}")

        if response.status_code != 200 or data.get("status") != "complete":
             raise Exception("Invalid OTP")
        
             
        # 'created_user_id' is the final Clerk User ID you should save to your DB
        return data.get("created_user_id")
    
    
async def create_organization(org_name: str, session_id: str, owner: OwnerPayload, plan: Literal["free", "growth_monthly", "growth_yearly", "enterprise"], max_allowed_members: int = 2):
    """
    Create an organization in Clerk for the given user and also add the organization details in MongoDB.
    """
    logger.info(f"Creating organization '{org_name}' for user {owner['clerk_user_id']}")
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{base_url}/organizations",
                headers=clerk_headers(),
                json={
                    "name": org_name,
                    "created_by": owner["clerk_user_id"],
                    "max_allowed_memberships": max_allowed_members
                    
                }
            )
            logger.info(f"Clerk create organization response: {response.json()}")
            if response.status_code != 200:
                raise Exception(f"Failed to create organization: {response.text}")

            response_data = response.json()
            org_id = response_data.get("id")

            db.organizations.insert_one({
                "org_id": org_id,
                "org_name": org_name,
                "session_id": session_id,
                "owner" : {
                    "clerk_user_id": owner["clerk_user_id"],
                    "email": owner["owner_email"],
                    "first_name": owner["owner_first_name"],
                    "last_name": owner["owner_last_name"],
                    "phone_number": owner.get("owner_phone_number")
                },
                "wa_id" : None,
                "ig_id" : None,
                "status": "active",
                "onboarding_flow_completed" : False,
                "sign_up_flow_completed" : True,
                "plan": plan,
                "max_allowed_members": max_allowed_members,
                "created_at": datetime.utcnow()
            })

            return response.json()  # Returns organization object
        except Exception as e:
            logger.error(f"Error creating organization: {str(e)}")
            return None
    
async def invite_users_to_organization(clerk_user_id: str, org_id: str, emails: list[str], role: str = "member"):
    """
    Invite users to the organization by email.
    """
    async with httpx.AsyncClient() as client:
        invitations = []
        for email in emails:
            response = await client.post(
                f"{base_url}/organizations/{org_id}/invitations",
                headers=clerk_headers(),
                json={
                    "email_address": email,
                    "role": role,
                    "inviter_user_id": clerk_user_id,
                    "redirect_url" : "https://www.heidelai.com/accept_invitation"
                }
            )
            if response.status_code != 200:
                logger.error(f"Failed to invite {email}: {response.text}")
            else:
                invitations.append(response.json())
        return invitations