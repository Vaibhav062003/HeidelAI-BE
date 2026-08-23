"""
Conversation Transfer System API Routes
Includes: Transfer, Reclaim, @Mentions, Team Agents

User instruction: "you are the backend team" - implement complete transfer system
with security fixes for org isolation, optimistic locking, audit trail.
"""

from datetime import datetime, timedelta
from typing import Optional, List
import httpx
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Request, status
from bson import ObjectId
from pydantic import BaseModel
from pymongo import ReturnDocument

from auth.dependencies import CurrentUser
from database import get_mongo_db, get_async_mongo_db
from config.settings import CLERK_TOKEN
from core.services import services
from services.websocket_service import send_to_user
from services.utils import serialize_mongo
from services.work_team_service import create_conversation_work, list_work_teams, record_team_presence, require_conversation_access, require_team
from core.limiter import limiter
from loguru import logger


router = APIRouter(tags=["Transfer System"])
db = get_mongo_db()



CLERK_CACHE_TTL = 300  # seconds
_clerk_cache: TTLCache = TTLCache(maxsize=500, ttl=CLERK_CACHE_TTL)
_http_client = httpx.AsyncClient(timeout=10.0)

async def get_org_memberships(org_id: str) -> list:
    """Fetch org memberships from Clerk, cached per org for 5 minutes."""
    if org_id in _clerk_cache:
        return _clerk_cache[org_id]

    url = f"https://api.clerk.com/v1/organizations/{org_id}/memberships"
    headers = {
        "Authorization": f"Bearer {CLERK_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        resp = await _http_client.get(url, headers=headers, params={"limit": 500})
        if resp.status_code != 200:
            logger.error(f"Clerk memberships fetch failed: {resp.text}")
            raise HTTPException(500, "Clerk service error")
        data = resp.json().get("data", [])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Clerk connection error: {e}")
        raise HTTPException(500, "Clerk service unavailable")

    _clerk_cache[org_id] = data
    return data


def membership_public_data(membership: dict) -> dict:
    """Clerk explicitly allows public_user_data to be null."""
    return membership.get("public_user_data") or {}


def assignment_target_id(entry) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    return (
        (entry.get("to_team") or {}).get("id")
        or (entry.get("to_agent") or {}).get("id")
        or entry.get("id")
    )



# === Request Models ===

class TransferRequest(BaseModel):
    target_agent_id: Optional[str] = None
    target_team_id: Optional[str] = None
    reason: Optional[str] = None


class MentionCreate(BaseModel):
    mentioned_usernames: List[str]
    message_content: str


# === Helper Functions ===

async def log_audit(
    conversation_id: str,
    org_id: str,
    action: str,
    from_agent_id: Optional[str],
    to_agent_id: str,
    performed_by: str,
    ip_address: str,
    reason: Optional[str] = None
):
    """Create immutable audit log entry."""
    async_db = get_async_mongo_db()
    await async_db.conversation_audit_logs.insert_one({
        "conversation_id": conversation_id,
        "org_id": org_id,
        "action": action,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "performed_by": performed_by,
        "timestamp": datetime.utcnow(),
        "ip_address": ip_address,
        "reason": reason
    })


async def check_transfer_loop(conversation_id: str, target_agent_id: str, org_id: str) -> bool:
    """Check if transfer would create a loop (within 5min window)."""
    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{org_id}"]

    conv = await collection.find_one(
        {"conversation_id": conversation_id},
        {"assignment_history": 1}  # only fetch what we need
    )
    if not conv:
        return False

    history = conv.get("assignment_history") or []
    if not history:
        return False

    cutoff = datetime.utcnow() - timedelta(minutes=5)

    for entry in history:
        # Support both new format (to_agent.id) and legacy format (id field directly)
        entry_agent_id = assignment_target_id(entry)
        if entry_agent_id != target_agent_id:
            continue

        # Support both new format (timestamp str) and legacy format (assigned_at str/datetime)
        ts = entry.get("timestamp") or entry.get("assigned_at")
        if not ts:
            continue
        try:
            if isinstance(ts, datetime):
                entry_time = ts
            else:
                entry_time = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
            # Strip timezone for naive comparison
            if entry_time.tzinfo is not None:
                entry_time = entry_time.replace(tzinfo=None)
            if entry_time > cutoff:
                return True
        except Exception as e:
            logger.warning(f"check_transfer_loop: could not parse timestamp '{ts}': {e}")

    return False


# === Transfer Endpoints ===

@router.post("/api/conversations/{conversation_id}/transfer")
@limiter.limit("20/minute")
async def transfer_conversation(
    request: Request,
    conversation_id: str,
    payload: TransferRequest,
    user: CurrentUser
):
    """
    Transfer conversation to another agent.

    Security:
    - Verifies org ownership (multi-tenant isolation)
    - Validates target agent exists and is active
    - Checks for transfer loops (5min window)
    - Uses optimistic locking (version field)
    - Creates audit trail
    - Emits WebSocket event
    """

    if not payload.target_agent_id and not payload.target_team_id:
        raise HTTPException(422, "Choose a team or team member")

    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{user.org_id}"]
    conv = await require_conversation_access(
        user.org_id, conversation_id, user.user_id, user.role
    )
    team = await require_team(user.org_id, payload.target_team_id) if payload.target_team_id else None

    target_member = None
    target_name = None
    if payload.target_agent_id:
        memberships_data = await get_org_memberships(user.org_id)
        target_member = next((
            membership for membership in memberships_data
            if membership_public_data(membership).get("user_id") == payload.target_agent_id
        ), None)
        if not target_member or membership_public_data(target_member).get("banned"):
            raise HTTPException(400, "Target team member was not found or is inactive")
        if team and payload.target_agent_id not in team.get("member_ids", []):
            raise HTTPException(422, "That person is not a member of the selected team")
        public_data = membership_public_data(target_member)
        target_name = (
            f"{public_data.get('first_name', '')} {public_data.get('last_name', '')}".strip()
            or public_data.get("identifier")
            or "Team member"
        )

    teams = await list_work_teams(user.org_id)
    if payload.target_agent_id and not team and teams:
        member_teams = [item for item in teams if payload.target_agent_id in item.get("member_ids", [])]
        if not any(item.get("type") == "sales" for item in member_teams):
            if len(member_teams) == 1:
                team = member_teams[0]
            elif len(member_teams) > 1:
                raise HTTPException(422, "Choose which team should receive this work")
            else:
                raise HTTPException(422, "Add this person to a team before assigning work")

    if team:
        work, created = await create_conversation_work(
            user.org_id,
            conversation_id,
            user.user_id,
            user.full_name or "Team member",
            user.role,
            {
                "team_id": team["team_id"],
                "assigned_agent_id": payload.target_agent_id,
                "assigned_agent_name": target_name,
                "type": "pricing" if team.get("type") == "sales" else team.get("type"),
                "note": payload.reason,
            },
        )
        updated_conversation = await collection.find_one({"conversation_id": conversation_id})
        return {
            "status": "success",
            "message": "Work assigned",
            "conversation": serialize_mongo(updated_conversation),
            "work_item": work,
            "created": created,
        }

    target_key = payload.target_agent_id
    if await check_transfer_loop(conversation_id, target_key, user.org_id):
        raise HTTPException(400, "This chat was transferred to the same destination recently")

    current_version = conv.get("version")

    # Build query to match current version OR missing version field if current_version is None/0
    query = {"conversation_id": conversation_id}
    if not current_version:
        query["$or"] = [
            {"version": {"$exists": False}},
            {"version": None},
            {"version": 0}
        ]
    else:
        query["version"] = current_version

    previous_owner_id = conv.get("assigned_agent_id") or (conv.get("assigned_agent") or {}).get("id")
    previous_owner_name = (
        (conv.get("assigned_agent") or {}).get("name")
        or (conv.get("assigned_agent") or {}).get("username")
        or "Unassigned"
    )
    now = datetime.utcnow().isoformat()
    updates = {
        "owner_team_id": None,
        "owner_team_name": None,
        "assigned_agent_id": payload.target_agent_id,
        "assigned_agent": ({
            "id": payload.target_agent_id,
            "name": target_name,
            "username": target_name,
            "assigned_at": now,
        } if payload.target_agent_id else None),
    }
    history = {
        "from_agent": {"id": previous_owner_id, "name": previous_owner_name},
        "to_agent": ({"id": payload.target_agent_id, "name": target_name} if payload.target_agent_id else None),
        "to_team": None,
        "timestamp": now,
        "reason": payload.reason,
    }
    updated_conversation = await collection.find_one_and_update(
        query,
        {
            "$set": updates,
            "$inc": {"version": 1},
            "$push": {"assignment_history": history},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_conversation:
        raise HTTPException(409, "Conversation was modified by another user. Refresh and retry.")

    # 6. Audit trail
    await log_audit(
        conversation_id=conversation_id,
        org_id=user.org_id,
        action="transferred",
        from_agent_id=previous_owner_id,
        to_agent_id=target_key,
        performed_by=user.user_id,
        ip_address=request.client.host,
        reason=payload.reason
    )

    # 7. WebSocket notification
    sales_member_ids = [
        member_id
        for sales_team in await list_work_teams(user.org_id)
        if sales_team.get("type") == "sales"
        for member_id in sales_team.get("member_ids", [])
    ]
    recipients = [
        *sales_member_ids,
        payload.target_agent_id,
    ]
    for recipient_id in dict.fromkeys(recipient for recipient in recipients if recipient):
        try:
            await send_to_user(recipient_id, {
                "type": "conversation_transferred",
                "conversation_id": conversation_id,
                "from_agent": user.full_name or "Team member",
                "to_agent": target_name,
                "to_team": None,
                "customer_name": conv.get("customer_name") or "Customer",
                "reason": payload.reason,
            }, org_id=user.org_id)
        except Exception as error:
            logger.warning(f"Failed to notify transferred conversation recipient: {error}")

    logger.info(f"Conversation {conversation_id} transferred from {user.user_id} to {target_key}")

    return {
        "status": "success",
        "message": "Conversation transferred",
        "conversation": serialize_mongo(updated_conversation),
    }


@router.post("/api/conversations/{conversation_id}/reclaim")
@limiter.limit("20/minute")
async def reclaim_conversation(
    request: Request,
    conversation_id: str,
    user: CurrentUser
):
    """
    Reclaim a conversation previously transferred away.
    Validates that user was a previous owner.
    """

    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{user.org_id}"]

    conv = await require_conversation_access(
        user.org_id, conversation_id, user.user_id, user.role
    )

    # Check user was previous owner
    history = conv.get("assignment_history") or []
    was_owner = any(
        (entry.get("from_agent") or {}).get("id") == user.user_id
        for entry in history if isinstance(entry, dict)
    )

    if not was_owner:
        raise HTTPException(403, "You were never assigned to this conversation")

    # Transfer back to user
    current_assigned = conv.get("assigned_agent_id")

    updated_conversation = await collection.find_one_and_update(
        {"conversation_id": conversation_id},
        {
            "$set": {
                "assigned_agent_id": user.user_id,
                "assigned_agent": {
                    "id": user.user_id,
                    "name": user.full_name or "Agent",
                    "assigned_at": datetime.utcnow().isoformat()
                }
            },
            "$push": {
                "assignment_history": {
                    "from_agent": {
                        "id": current_assigned or "system",
                        "name": "System"
                    },
                    "to_agent": {
                        "id": user.user_id,
                        "name": user.full_name or "Agent"
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                    "reason": "Reclaimed"
                }
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_conversation:
        raise HTTPException(500, "Failed to reclaim conversation")

    # Audit
    await log_audit(
        conversation_id=conversation_id,
        org_id=user.org_id,
        action="reclaimed",
        from_agent_id=current_assigned,
        to_agent_id=user.user_id,
        performed_by=user.user_id,
        ip_address=request.client.host,
        reason="Reclaimed"
    )

    logger.info(f"Conversation {conversation_id} reclaimed by {user.user_id}")

    return {
        "status": "success",
        "message": "Conversation reclaimed",
        "conversation": serialize_mongo(updated_conversation),
    }


# === @Mention Pool Endpoints ===

@router.post("/api/conversations/{conversation_id}/mentions")
@limiter.limit("20/minute")
async def create_mentions(
    request: Request,
    conversation_id: str,
    payload: MentionCreate,
    user: CurrentUser
):
    """
    Create @mention pool entries.
    - Deduplicates usernames
    - Validates agents exist in org
    - Checks pool capacity (max 25 pending per agent)
    - Creates mention records
    - Emits WebSocket events
    """

    async_db = get_async_mongo_db()
    users_col = async_db.users
    mentions_col = async_db.conversation_mentions
    await require_conversation_access(
        user.org_id, conversation_id, user.user_id, user.role
    )

    # Dedupe
    unique_usernames = list(set(payload.mentioned_usernames))

    # Fetch active agents in same org via Clerk
    memberships_data = await get_org_memberships(user.org_id)

    # Build username -> agent ID map (case insensitive)
    agent_map = {}
    for m in memberships_data:
        public_data = membership_public_data(m)
        user_id = public_data.get("user_id")
        
        email = public_data.get("identifier", "").lower()
        if email:
            agent_map[email] = user_id
            
        username = public_data.get("username")
        if username:
            agent_map[username.lower()] = user_id

    created_count = 0

    for username in unique_usernames:
        agent_id = agent_map.get(username.lower())
        if not agent_id:
            continue

        # Check pool capacity (scoped to this org to enforce per-org isolation)
        pending_count = await mentions_col.count_documents({
            "to_agent_id": agent_id,
            "org_id": user.org_id,
            "status": "pending"
        })

        if pending_count >= 25:
            logger.warning(f"Agent {agent_id} has reached mention capacity (25) in org {user.org_id}")
            continue

        # Create mention
        mention_doc = {
            "conversation_id": conversation_id,
            "org_id": user.org_id,
            "from_agent_id": user.user_id,
            "to_agent_id": agent_id,
            "status": "pending",
            "message_content": payload.message_content[:500],
            "created_at": datetime.utcnow()
        }

        await mentions_col.insert_one(mention_doc)
        created_count += 1

        # WebSocket notification
        try:
            await send_to_user(agent_id, {
                "type": "conversation_mention",
                "conversation_id": conversation_id,
                "from_agent": user.full_name or "Agent",
                "message_content": payload.message_content[:100]
            }, org_id=user.org_id)
        except Exception as e:
            logger.warning(f"Failed to send WebSocket mention notification: {e}")

    return {"status": "success", "created_count": created_count}


@router.get("/api/conversations/pending-mentions")
@limiter.limit("30/minute")
async def get_pending_mentions(
    request: Request,
    user: CurrentUser
):
    """Fetch all pending mention requests for current agent."""

    async_db = get_async_mongo_db()
    mentions_col = async_db.conversation_mentions
    # Fetch active agents in same org via Clerk to resolve sender names
    members_map = {}
    try:
        memberships_data = await get_org_memberships(user.org_id)
        for m in memberships_data:
            public_data = membership_public_data(m)
            u_id = public_data.get("user_id")
            name = f"{public_data.get('first_name', '')} {public_data.get('last_name', '')}".strip() or public_data.get("identifier") or "Agent"
            members_map[u_id] = name
    except Exception as e:
        logger.error(f"Failed to fetch memberships from Clerk for mentions: {e}")

    # Fetch pending mentions
    mentions_cursor = mentions_col.find({
        "to_agent_id": user.user_id,
        "org_id": user.org_id,
        "status": "pending"
    }).sort("created_at", -1)

    mentions = await mentions_cursor.to_list(length=100)

    # Enrich with from_agent details
    result = []
    for mention in mentions:
        from_agent_id = mention.get("from_agent_id")
        from_name = members_map.get(from_agent_id, "Unknown")

        result.append({
            "id": str(mention["_id"]),
            "conversation_id": mention["conversation_id"],
            "from_agent": {
                "id": from_agent_id,
                "name": from_name
            },
            "message_content": mention["message_content"],
            "created_at": mention["created_at"].isoformat()
        })

    return {"data": result, "count": len(result)}


@router.post("/api/mentions/{mention_id}/accept")
@limiter.limit("20/minute")
async def accept_mention(
    request: Request,
    mention_id: str,
    user: CurrentUser
):
    """Accept mention → transfer conversation ownership."""

    async_db = get_async_mongo_db()
    mentions_col = async_db.conversation_mentions

    # Validate mention_id format before use
    try:
        mention_oid = ObjectId(mention_id)
    except Exception:
        raise HTTPException(400, "Invalid mention ID")

    # Fetch mention — scoped by org_id to prevent cross-tenant access
    mention = await mentions_col.find_one({
        "_id": mention_oid,
        "org_id": user.org_id,
        "to_agent_id": user.user_id,
        "status": "pending"
    })

    if not mention:
        raise HTTPException(404, "Mention not found")

    conversation_id = mention["conversation_id"]
    org_id = mention["org_id"]

    # Transfer conversation
    collection = async_db[f"conversations_{org_id}"]
    await collection.update_one(
        {"conversation_id": conversation_id},
        {
            "$set": {
                "assigned_agent_id": user.user_id,
                "assigned_agent": {
                    "id": user.user_id,
                    "name": user.full_name or "Agent",
                    "assigned_at": datetime.utcnow().isoformat()
                }
            },
            "$push": {
                "assignment_history": {
                    "from_agent": {
                        "id": mention["from_agent_id"],
                        "name": "Via Mention"
                    },
                    "to_agent": {
                        "id": user.user_id,
                        "name": user.full_name or "Agent"
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                    "reason": "Mention accepted"
                }
            }
        }
    )

    # Update mention status
    await mentions_col.update_one(
        {"_id": ObjectId(mention_id)},
        {
            "$set": {
                "status": "accepted",
                "resolved_at": datetime.utcnow()
            }
        }
    )

    # Notify original agent
    try:
        await send_to_user(mention["from_agent_id"], {
            "type": "mention_accepted",
            "conversation_id": conversation_id,
            "accepted_by": user.full_name or "Agent"
        }, org_id=user.org_id)
    except Exception as e:
        logger.warning(f"Failed to send mention_accepted notification: {e}")

    return {"status": "success", "message": "Mention accepted"}


@router.post("/api/mentions/{mention_id}/reject")
@limiter.limit("20/minute")
async def reject_mention(
    request: Request,
    mention_id: str,
    user: CurrentUser
):
    """Reject mention → notify original agent."""

    async_db = get_async_mongo_db()
    mentions_col = async_db.conversation_mentions

    # Validate mention_id format before use
    try:
        mention_oid = ObjectId(mention_id)
    except Exception:
        raise HTTPException(400, "Invalid mention ID")

    # Fetch mention — scoped by org_id to prevent cross-tenant access
    mention = await mentions_col.find_one({
        "_id": mention_oid,
        "org_id": user.org_id,
        "to_agent_id": user.user_id,
        "status": "pending"
    })

    if not mention:
        raise HTTPException(404, "Mention not found")

    # Update status
    await mentions_col.update_one(
        {"_id": ObjectId(mention_id)},
        {
            "$set": {
                "status": "rejected",
                "resolved_at": datetime.utcnow()
            }
        }
    )

    # Notify original agent
    try:
        await send_to_user(mention["from_agent_id"], {
            "type": "mention_rejected",
            "conversation_id": mention["conversation_id"],
            "rejected_by": user.full_name or "Agent"
        }, org_id=user.org_id)
    except Exception as e:
        logger.warning(f"Failed to send mention_rejected notification: {e}")

    return {"status": "success", "message": "Mention rejected"}


# === Team Agents Endpoint ===

@router.get("/api/agents/team")
@limiter.limit("30/minute")
async def get_team_agents(
    request: Request,
    user: CurrentUser,
    role: Optional[str] = None
):
    """
    List all active agents in organization for transfer autocomplete.
    """

    memberships_data = await get_org_memberships(user.org_id)
    await record_team_presence(user.org_id, user.user_id)
    teams = await list_work_teams(user.org_id)
    active_today_ids = {
        member_id
        for team in teams
        for member_id in team.get("active_today_member_ids", [])
    }

    result = []
    for m in memberships_data:
        public_data = membership_public_data(m)
        if public_data.get("banned"):
            continue
            
        user_id = public_data.get("user_id")
        
        clerk_role = m.get("role", "org:member")
        role_name = clerk_role.replace("org:", "")
        
        if role and role.lower() != role_name.lower():
            continue

        if not user_id:
            continue
        identifier = public_data.get("identifier") or ""
        name = f"{public_data.get('first_name') or ''} {public_data.get('last_name') or ''}".strip()
        name = name or public_data.get("username") or identifier or "Agent"

        result.append({
            "id": user_id,
            "name": name,
            "identifier": identifier,
            "role": role_name,
            "active_today": user_id in active_today_ids,
            "teams": [
                {"id": team["team_id"], "name": team["name"], "type": team["type"]}
                for team in teams
                if user_id in team.get("member_ids", [])
            ],
        })

    return result
