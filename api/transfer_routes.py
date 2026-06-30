"""
Conversation Transfer System API Routes
Includes: Transfer, Reclaim, @Mentions, Team Agents

User instruction: "you are the backend team" - implement complete transfer system
with security fixes for org isolation, optimistic locking, audit trail.
"""

from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Request, status
from bson import ObjectId
from pydantic import BaseModel

from auth.dependencies import CurrentUser
from database import get_mongo_db, get_async_mongo_db
from core.services import services
from core.limiter import limiter
from loguru import logger


router = APIRouter(tags=["Transfer System"])
db = get_mongo_db()


# === Request Models ===

class TransferRequest(BaseModel):
    target_agent_id: str
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

    conv = await collection.find_one({"conversation_id": conversation_id})
    if not conv:
        return False

    history = conv.get("assignment_history", [])
    if not history:
        return False

    cutoff = datetime.utcnow() - timedelta(minutes=5)

    for entry in history:
        if entry.get("to_agent", {}).get("id") == target_agent_id:
            timestamp_str = entry.get("timestamp")
            if timestamp_str:
                try:
                    entry_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    if entry_time > cutoff:
                        return True
                except:
                    pass

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

    async_db = get_async_mongo_db()
    collection = async_db[f"conversations_{user.org_id}"]
    users_col = async_db.users

    # 1. Verify org ownership (CRITICAL: prevents cross-org access)
    conv = await collection.find_one({"conversation_id": conversation_id})
    if not conv:
        raise HTTPException(404, "Conversation not found")

    # 2. Validate target agent exists in same org and is active
    target_agent = await users_col.find_one({
        "_id": ObjectId(payload.target_agent_id),
        "org_id": user.org_id,
        "status": {"$ne": "inactive"}
    })

    if not target_agent:
        raise HTTPException(400, "Target agent not found or inactive")

    # 3. Check transfer loop
    is_loop = await check_transfer_loop(conversation_id, payload.target_agent_id, user.org_id)
    if is_loop:
        raise HTTPException(
            400,
            "Transfer loop detected. This conversation was recently handled by the target agent."
        )

    # 4. Get target agent name
    target_name = target_agent.get("name", target_agent.get("first_name", "Agent"))

    # 5. Optimistic locking update
    current_version = conv.get("version", 0)

    update_result = await collection.update_one(
        {
            "conversation_id": conversation_id,
            "version": current_version
        },
        {
            "$set": {
                "assigned_agent_id": payload.target_agent_id,
                "assigned_agent": {
                    "id": payload.target_agent_id,
                    "name": target_name,
                    "assigned_at": datetime.utcnow().isoformat()
                }
            },
            "$inc": {"version": 1},
            "$push": {
                "assignment_history": {
                    "from_agent": {
                        "id": user.user_id,
                        "name": user.full_name or "Agent"
                    },
                    "to_agent": {
                        "id": payload.target_agent_id,
                        "name": target_name
                    },
                    "timestamp": datetime.utcnow().isoformat(),
                    "reason": payload.reason
                }
            }
        }
    )

    if update_result.modified_count == 0:
        raise HTTPException(409, "Conversation was modified by another user. Refresh and retry.")

    # 6. Audit trail
    await log_audit(
        conversation_id=conversation_id,
        org_id=user.org_id,
        action="transferred",
        from_agent_id=user.user_id,
        to_agent_id=payload.target_agent_id,
        performed_by=user.user_id,
        ip_address=request.client.host,
        reason=payload.reason
    )

    # 7. WebSocket notification
    try:
        await services.send_to_user(payload.target_agent_id, {
            "type": "conversation_transferred",
            "conversation_id": conversation_id,
            "from_agent": user.full_name or "Agent",
            "to_agent": target_name,
            "reason": payload.reason
        })
    except Exception as e:
        logger.warning(f"Failed to send WebSocket notification: {e}")

    logger.info(f"Conversation {conversation_id} transferred from {user.user_id} to {payload.target_agent_id}")

    return {"status": "success", "message": "Conversation transferred"}


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

    # Verify org ownership
    conv = await collection.find_one({"conversation_id": conversation_id})
    if not conv:
        raise HTTPException(404, "Conversation not found")

    # Check user was previous owner
    history = conv.get("assignment_history", [])
    was_owner = any(
        entry.get("from_agent", {}).get("id") == user.user_id
        for entry in history
    )

    if not was_owner:
        raise HTTPException(403, "You were never assigned to this conversation")

    # Transfer back to user
    current_assigned = conv.get("assigned_agent_id")

    update_result = await collection.update_one(
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
        }
    )

    if update_result.modified_count == 0:
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

    return {"status": "success", "message": "Conversation reclaimed"}


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

    # Dedupe
    unique_usernames = list(set(payload.mentioned_usernames))

    # Fetch active agents in same org
    agents_cursor = users_col.find({
        "org_id": user.org_id,
        "status": {"$ne": "inactive"}
    })
    agents = await agents_cursor.to_list(length=None)

    # Build username -> agent map (case insensitive)
    agent_map = {}
    for agent in agents:
        username = agent.get("username", "").lower()
        if username:
            agent_map[username] = agent

    created_count = 0

    for username in unique_usernames:
        agent = agent_map.get(username.lower())
        if not agent:
            continue

        agent_id = str(agent["_id"])

        # Check pool capacity
        pending_count = await mentions_col.count_documents({
            "to_agent_id": agent_id,
            "status": "pending"
        })

        if pending_count >= 25:
            logger.warning(f"Agent {agent_id} has reached mention capacity (25)")
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
            await services.send_to_user(agent_id, {
                "type": "conversation_mention",
                "conversation_id": conversation_id,
                "from_agent": user.full_name or "Agent",
                "message_content": payload.message_content[:100]
            })
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
    users_col = async_db.users

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
        from_agent = await users_col.find_one({"_id": ObjectId(from_agent_id)})

        result.append({
            "id": str(mention["_id"]),
            "conversation_id": mention["conversation_id"],
            "from_agent": {
                "id": from_agent_id,
                "name": from_agent.get("name", "Agent") if from_agent else "Unknown"
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

    # Fetch mention
    mention = await mentions_col.find_one({
        "_id": ObjectId(mention_id),
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
        await services.send_to_user(mention["from_agent_id"], {
            "type": "mention_accepted",
            "conversation_id": conversation_id,
            "accepted_by": user.full_name or "Agent"
        })
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

    # Fetch mention
    mention = await mentions_col.find_one({
        "_id": ObjectId(mention_id),
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
        await services.send_to_user(mention["from_agent_id"], {
            "type": "mention_rejected",
            "conversation_id": mention["conversation_id"],
            "rejected_by": user.full_name or "Agent"
        })
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

    async_db = get_async_mongo_db()
    users_col = async_db.users

    query = {
        "org_id": user.org_id,
        "status": {"$ne": "inactive"}
    }

    if role:
        query["role"] = role

    agents_cursor = users_col.find(query, {
        "_id": 1,
        "name": 1,
        "first_name": 1,
        "username": 1,
        "role": 1,
        "status": 1
    })

    agents = await agents_cursor.to_list(length=200)

    result = []
    for agent in agents:
        name = agent.get("name") or agent.get("first_name") or "Agent"
        result.append({
            "id": str(agent["_id"]),
            "name": name,
            "username": agent.get("username", ""),
            "role": agent.get("role", "agent"),
            "status": agent.get("status", "active")
        })

    return result
