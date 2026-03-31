import httpx
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_mongo_db
from auth.dependencies import CurrentUser
from config.settings import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from loguru import logger
db = get_mongo_db()

router = APIRouter(prefix="/api/google", tags=["Google"])


# ─── Pydantic Models ──────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    code: str
    redirect_uri: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/auth")
async def google_auth(request: GoogleAuthRequest, user: CurrentUser):
    """
    Exchange Google OAuth authorization code for access + refresh tokens.
    Fetches user profile, stores credentials server-side, returns sanitized profile.

    Frontend receives: { email, name, picture, scopes }
    Frontend NEVER receives: access_token, refresh_token, client_secret
    """
    try:
        org_id = user.org_id
        code = request.code
        redirect_uri = request.redirect_uri

        if not code:
            raise HTTPException(status_code=400, detail="Authorization code is required")

        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise HTTPException(status_code=500, detail="Google OAuth credentials not configured")

        # ── Step 1: Exchange code for tokens ──────────────────────────────────
        token_url = "https://oauth2.googleapis.com/token"
        token_data = {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }

        async with httpx.AsyncClient() as client:
            token_response = await client.post(token_url, data=token_data, timeout=10)

        if token_response.status_code != 200:
            logger.error(f"Google token exchange failed: {token_response.text}")
            raise HTTPException(
                status_code=400,
                detail=f"Failed to exchange authorization code: {token_response.json().get('error_description', 'Unknown error')}"
            )

        token_info = token_response.json()
        access_token = token_info.get("access_token")
        refresh_token = token_info.get("refresh_token")
        expires_in = token_info.get("expires_in", 3600)
        scope_str = token_info.get("scope", "")

        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to retrieve access token from Google")

        logger.info(f"Google token exchange successful for org {org_id}")

        # ── Step 2: Fetch user profile ─────────────────────────────────────────
        profile_url = "https://www.googleapis.com/oauth2/v2/userinfo"
        async with httpx.AsyncClient() as client:
            profile_response = await client.get(
                profile_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10
            )

        if profile_response.status_code != 200:
            logger.error(f"Google profile fetch failed: {profile_response.text}")
            raise HTTPException(status_code=400, detail="Failed to fetch Google user profile")

        profile_data = profile_response.json()
        email = profile_data.get("email", "")
        name = profile_data.get("name", "")
        picture = profile_data.get("picture", "")

        if not email:
            raise HTTPException(status_code=400, detail="Could not retrieve email from Google profile")

        # Parse scopes from token response
        scopes = [s.strip() for s in scope_str.split() if s.strip()]

        # ── Step 3: Store connection in MongoDB ───────────────────────────────
        now = datetime.now(timezone.utc)
        token_expiry = datetime.fromtimestamp(
            now.timestamp() + expires_in, tz=timezone.utc
        )

        google_id = f"goog_{uuid.uuid4().hex}"
        connection_doc = {
            "id": google_id,
            "org_id": org_id,
            "status": "active",
            "email": email,
            "name": name,
            "picture": picture,
            "scopes": scopes,
            "credentials": {
                "access_token": access_token,
                "refresh_token": refresh_token,   # May be None if already had one
                "token_expiry": token_expiry.isoformat(),
            },
            "connected_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        # Upsert - update if org already has a connection, insert if new
        # If we had a refresh token before and the new exchange didn't return one,
        # preserve the old refresh token (Google only returns it on first connect / prompt=consent)
        existing = db.google_connections.find_one({"org_id": org_id})
        if existing and not refresh_token:
            old_rt = existing.get("credentials", {}).get("refresh_token")
            if old_rt:
                connection_doc["credentials"]["refresh_token"] = old_rt
                logger.info(f"Preserved existing refresh_token for org {org_id}")

        db.google_connections.update_one(
            {"org_id": org_id},
            {"$set": connection_doc},
            upsert=True
        )

        # Update organization document with google_id
        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"google_id": google_id}}
        )

        logger.info(f"Google connection stored and linked for org {org_id} ({email})")

        # ── Step 4: Return sanitized profile to frontend ──────────────────────
        return {
            "email": email,
            "name": name,
            "picture": picture,
            "scopes": scopes,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Google auth error for org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Google authentication error: {str(e)}")


@router.get("/profile")
async def google_profile(user: CurrentUser):
    """
    Return stored Google profile for this org.
    Returns 404 if no active connection exists.
    Never returns credentials.
    """
    try:
        org_id = user.org_id

        connection = db.google_connections.find_one(
            {"org_id": org_id, "status": "active"},
            {"_id": 0, "credentials": 0}  # Exclude credentials from response
        )

        if not connection:
            raise HTTPException(status_code=404, detail="No active Google connection found")

        return {
            "status": True,
            "email": connection.get("email"),
            "name": connection.get("name"),
            "picture": connection.get("picture"),
            "scopes": connection.get("scopes", []),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching Google profile for org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch Google profile: {str(e)}")


@router.post("/disconnect")
async def google_disconnect(user: CurrentUser):
    """
    Disconnect Google account:
    1. Best-effort token revocation with Google
    2. Mark connection as inactive in MongoDB
    3. Disable polling on all sheets for this org
    """
    try:
        org_id = user.org_id

        connection = db.google_connections.find_one({"org_id": org_id, "status": "active"})

        if not connection:
            # Already disconnected - idempotent, return success
            logger.info(f"Google disconnect called but no active connection for org {org_id}")
            return {"message": "Google account disconnected"}

        # ── Step 1: Revoke token with Google (best-effort) ────────────────────
        credentials = connection.get("credentials", {})
        access_token = credentials.get("access_token")
        refresh_token = credentials.get("refresh_token")

        token_to_revoke = refresh_token or access_token
        if token_to_revoke:
            try:
                async with httpx.AsyncClient() as client:
                    revoke_response = await client.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": token_to_revoke},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=5
                    )
                if revoke_response.status_code == 200:
                    logger.info(f"Google token revoked for org {org_id}")
                else:
                    # Non-fatal: token may already be expired
                    logger.warning(f"Google token revocation returned {revoke_response.status_code} for org {org_id}")
            except Exception as revoke_err:
                # Non-fatal - proceed with disconnection
                logger.warning(f"Token revocation request failed (non-fatal): {revoke_err}")

        # ── Step 2: Mark connection as inactive ───────────────────────────────
        now = datetime.now(timezone.utc)
        db.google_connections.update_one(
            {"org_id": org_id},
            {"$set": {"status": "inactive", "disconnected_at": now.isoformat(), "updated_at": now.isoformat()}}
        )

        # Update organization document to clear google_id
        db.organizations.update_one(
            {"org_id": org_id},
            {"$set": {"google_id": None}}
        )

        # ── Step 3: Disable polling on all sheets ─────────────────────────────
        result = db.google_sheets_connections.update_many(
            {"org_id": org_id},
            {"$set": {"polling_enabled": False, "updated_at": now.isoformat()}}
        )
        logger.info(f"Disabled polling on {result.modified_count} sheets for org {org_id}")

        logger.info(f"Google account disconnected and unlinked for org {org_id}")

        return {"message": "Google account disconnected successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting Google for org {user.org_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect Google: {str(e)}")
