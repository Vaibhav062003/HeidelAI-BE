import asyncio
from fastapi.encoders import jsonable_encoder
from core.services import services
from fastapi import WebSocket, WebSocketDisconnect
from database import get_mongo_db, get_async_mongo_db
from loguru import logger

db = get_mongo_db()


def _payload_conversation_id(payload: dict) -> str | None:
    for candidate in (payload, payload.get("message"), payload.get("conversation")):
        if isinstance(candidate, dict) and candidate.get("conversation_id"):
            return str(candidate["conversation_id"])
    return None


async def _eligible_conversation_clients(client_ids, conversation_id: str | None):
    if not conversation_id:
        return set(client_ids)
    from services.work_team_service import can_user_access_conversation, list_work_teams
    allowed_clients = set()
    clients_by_org = {}
    for client_id in client_ids:
        org_id = services.client_metadata.get(client_id, {}).get("org_id")
        if org_id:
            clients_by_org.setdefault(org_id, []).append(client_id)
    async_db = get_async_mongo_db()
    for org_id, org_client_ids in clients_by_org.items():
        conversation = await async_db[f"conversations_{org_id}"].find_one(
            {"conversation_id": conversation_id}
        )
        if not conversation:
            continue
        teams = await list_work_teams(org_id)
        for client_id in org_client_ids:
            meta = services.client_metadata.get(client_id, {})
            if can_user_access_conversation(
                meta.get("user_id") or "", meta.get("role") or "org:member", conversation, teams
            ):
                allowed_clients.add(client_id)
    return allowed_clients


async def latest_unread_whatsapp_message_id(org_id: str, conversation_id: str) -> str | None:
    async_db = get_async_mongo_db()
    message = await async_db[f"messages_{org_id}"].find_one(
        {
            "role": "customer",
            "status": {"$ne": "read"},
            "platform": "whatsapp",
            "conversation_id": conversation_id,
        },
        {"message_id": 1, "_id": 0},
        sort=[("timestamp", -1)],
    )
    return message.get("message_id") if message else None

async def cleanup_client(client_id: str):
    """
    Clean up all references to a disconnected client.
    """
    logger.info(f"Disconnecting client {client_id}")

    meta = services.client_metadata.get(client_id)
    agent_name = meta.get("agent_name") if meta else None

    convo_key = services.client_convo_map.get(client_id)
    if convo_key:
        org_id_for_convo, conversation_id = convo_key
        logger.info(f"Cleaning up presence data for conversation {convo_key}")
        leave_conversation_presence(org_id_for_convo, conversation_id, client_id)

        await broadcast_org_ws(
            org_id=org_id_for_convo,
            event_type="leave_conversation",
            payload={
                "conversation_id": conversation_id,
                "client_id": client_id,
                "agent_name": agent_name,
            },
            exclude_client_id=client_id,
        )

    if not meta:
        return
    ig_ids = meta.get("ig_ids") or ([meta.get("ig_id")] if meta.get("ig_id") else [])
    wa_ids = meta.get("wa_ids") or ([meta.get("wa_id")] if meta.get("wa_id") else [])
    user_id = meta.get("user_id")
    org_id = meta.get("org_id")

    if user_id and client_id in services.agent_clients.get(user_id, set()):
        services.agent_clients[user_id].remove(client_id)
        if not services.agent_clients[user_id]:
            del services.agent_clients[user_id]
    if org_id and client_id in services.org_clients.get(org_id, set()):
        services.org_clients[org_id].remove(client_id)
        if not services.org_clients[org_id]:
            del services.org_clients[org_id]
    for ig_id in ig_ids:
        services.instagram_clients[ig_id].discard(client_id)
        if not services.instagram_clients[ig_id]:
            del services.instagram_clients[ig_id]
    for wa_id in wa_ids:
        services.whatsapp_clients[wa_id].discard(client_id)
        if not services.whatsapp_clients[wa_id]:
            del services.whatsapp_clients[wa_id]

    services.active_clients.pop(client_id, None)
    services.client_metadata.pop(client_id, None)
    logger.success(f"Cleaned up client {client_id}")

async def handle_typing_event(client_id: str, data: dict):
    """
    Broadcast typing indicators to all users under the same IG or WA account,
    excluding the sender.
    """
    logger.info(f"[Typing Event] Client {client_id} sending typing: {data}")
    
    meta = services.client_metadata.get(client_id)
    if not meta:
        return

    ig_id = meta.get("ig_id")
    wa_id = meta.get("wa_id")

    # Which platform is this typing event for?
    platform = data.get("platform")  # "instagram" or "whatsapp"

    conversation_id = data.get("conversation_id")
    org_id = meta.get("org_id")
    if not conversation_id or not org_id:
        return
    from services.work_team_service import require_conversation_access
    await require_conversation_access(
        org_id, conversation_id, meta.get("user_id") or "", meta.get("role") or "org:member"
    )
    is_active_viewer = services.client_convo_map.get(client_id) == (org_id, conversation_id)
    message_id = None
    if platform == "whatsapp" and wa_id and data.get("is_typing") is True and is_active_viewer:
        message_id = await latest_unread_whatsapp_message_id(org_id, conversation_id)

    # Broadcast to Instagram
    if platform == "instagram" and ig_id:
        await broadcast_main_ws(
            platform_id=ig_id,
            platform_type="instagram",
            event_type="typing_indicator",
            payload=data,
            exclude_client_id=client_id 
        )

    # Broadcast to WhatsApp
    elif platform == "whatsapp" and wa_id:
        await broadcast_main_ws(
            platform_id=wa_id,
            platform_type="whatsapp",
            event_type="typing_indicator",
            payload=data,
            exclude_client_id=client_id 
        )

        if message_id and services.client_convo_map.get(client_id) == (org_id, conversation_id):
            from services.wa_service import read_message_and_typing_indicator
            asyncio.create_task(read_message_and_typing_indicator(
                whatsapp_id=wa_id,
                message_id=message_id,
            ))

async def broadcast_main_ws(platform_id: str, platform_type: str, event_type: str, payload: dict, exclude_client_id: str = None):
    """
    Broadcast events to all /main-ws clients connected under IG or WA.

    Args:
        platform_id     --> ig_id or wa_id
        platform_type   --> "instagram" or "whatsapp"
        event_type      --> "new_message", "conversation_updated", etc.
        payload         --> dict containing data
        exclude_client_id --> client_id to exclude from broadcast (usually the sender) (usually only set for typing indicators)
    """
    logger.info(f"📡 Broadcasting {event_type} to {platform_type} clients under ID {platform_id}")
    if platform_type == "instagram":
        target_map = services.instagram_clients
    else:
        target_map = services.whatsapp_clients

    client_ids = target_map.get(platform_id, [])
    if not client_ids:
        return

    try:
        client_ids = await _eligible_conversation_clients(
            client_ids,
            _payload_conversation_id(payload),
        )
    except Exception as error:
        logger.error(f"WebSocket access filtering failed: {type(error).__name__}")
        return
    if not client_ids:
        return

    safe_payload = jsonable_encoder(payload)

    disconnected_clients = []

    for client_id in client_ids:

        if exclude_client_id and client_id == exclude_client_id:
            continue

        ws = services.active_clients.get(client_id)
        if not ws:
            disconnected_clients.append(client_id)
            continue

        try:
            await ws.send_json({
                "type": event_type,
                "data":{
                    "platform": platform_type,
                    **safe_payload
                }
            })
        except Exception as e:
            logger.error(f"Failed to send to {client_id}: {e}, marking as disconnected")
        except WebSocketDisconnect:
            logger.error(f"Client {client_id} disconnected, marking as disconnected")
            disconnected_clients.append(client_id)

    # Cleanup
    for client_id in disconnected_clients:
        services.active_clients.pop(client_id, None)
        services.client_metadata.pop(client_id, None)
        target_map[platform_id].discard(client_id)

    if not target_map[platform_id]:
        del target_map[platform_id]

async def start_heartbeat(client_id: str, websocket: WebSocket):
    """
    Sends a ping every 20 seconds to keep the connection alive
    and detect dead clients.
    """
    try:
        while True:
            await asyncio.sleep(20) # 20s is safe (under the 60s timeout)
            
            # Check if socket is still open before sending
            if client_id in services.active_clients:
                try:
                    await websocket.send_json({"type": "ping", "data": {}})
                except Exception:
                    logger.error(f"Start Heartbeat: Failed to ping {client_id}")
                    break
            else:
                break
    except asyncio.CancelledError:
        pass

async def handle_unread_event(org_id: str, client_id: str, data: dict):
    """
    Handles join and leave conversation events for unread message tracking,
    then broadcasts presence to the rest of the org.
    """
    event_type = data.get("type")
    conversation_id = data.get("conversation_id")
    meta = services.client_metadata.get(client_id, {})
    agent_name = meta.get("agent_name")

    if not conversation_id or not event_type:
        logger.error(f"Invalid unread event data from {client_id}: {data}")
        return

    from services.work_team_service import require_conversation_access
    await require_conversation_access(
        org_id, conversation_id, meta.get("user_id") or "", meta.get("role") or "org:member"
    )

    if event_type == "join_conversation":
        join_conversation_presence(org_id, conversation_id, client_id)
    elif event_type == "leave_conversation":
        leave_conversation_presence(org_id, conversation_id, client_id)
    else:
        logger.error(f"Unknown unread event type: {event_type}")
        return
    
    payload = {
        "conversation_id": conversation_id,
        "client_id": client_id,
        "agent_name": agent_name,
    }

    await broadcast_org_ws(
        org_id=org_id,
        event_type=event_type,
        payload=payload,
        exclude_client_id=client_id,
    )

def join_conversation_presence(org_id: str, conversation_id: str, client_id: str):
    """
    Adds a client to the presence tracker for a specific conversation,
    indexed by org for fast org-scoped lookups.
    """
    previous = services.client_convo_map.get(client_id)
    if previous and previous != (org_id, conversation_id):
        previous_org, previous_conversation = previous
        previous_viewers = services.org_convo_viewers.get(previous_org, {}).get(previous_conversation)
        if previous_viewers:
            previous_viewers.discard(client_id)
            if not previous_viewers:
                del services.org_convo_viewers[previous_org][previous_conversation]
                if not services.org_convo_viewers[previous_org]:
                    del services.org_convo_viewers[previous_org]

    services.org_convo_viewers[org_id][conversation_id].add(client_id)
    services.client_convo_map[client_id] = (org_id, conversation_id)
    logger.info(f"Client {client_id} JOINED conversation ({org_id}, {conversation_id})")


def leave_conversation_presence(org_id: str, conversation_id: str, client_id: str):
    """
    Removes a client from the presence tracker and cleans up empty entries.
    """
    viewers = services.org_convo_viewers.get(org_id, {}).get(conversation_id)
    if viewers:
        viewers.discard(client_id)
        if not viewers:
            del services.org_convo_viewers[org_id][conversation_id]
            # Optional: clean up empty org entry too
            if not services.org_convo_viewers[org_id]:
                del services.org_convo_viewers[org_id]

    if services.client_convo_map.get(client_id) == (org_id, conversation_id):
        services.client_convo_map.pop(client_id, None)
    logger.info(f"Client {client_id} LEFT conversation ({org_id}, {conversation_id})")


def get_org_presence_snapshot(org_id: str) -> list[dict]:
    """
    Builds a full presence snapshot for an org: which agents currently
    have which conversations open.
    """
    snapshot = []
    convo_map = services.org_convo_viewers.get(org_id, {})
    for conversation_id, viewer_client_ids in convo_map.items():
        for cid in viewer_client_ids:
            meta = services.client_metadata.get(cid, {})
            snapshot.append({
                "conversation_id": conversation_id,
                "client_id": cid,
                "agent_name": meta.get("agent_name"),
            })
    return snapshot

async def broadcast_org_ws(org_id: str, event_type: str, payload: dict, exclude_client_id: str = None):
    """
    Broadcast an event to every client connected under the same org, excluding the sender.
    """
    client_ids = await _eligible_conversation_clients(
        services.org_clients.get(org_id, set()), payload.get("conversation_id")
    )

    disconnected_clients = []

    for cid in list(client_ids):
        if cid == exclude_client_id:
            continue
        ws = services.active_clients.get(cid)
        if ws:
            try:
                await ws.send_json({"type": event_type, "data": payload})
            except Exception as e:
                logger.error(f"Failed to send {event_type} to {cid}, marking as disconnected")
            except WebSocketDisconnect:
                logger.error(f"Client {cid} disconnected, marking as disconnected")
                disconnected_clients.append(cid)

    for client_id in disconnected_clients:
        services.org_clients[org_id].discard(client_id)
        services.active_clients.pop(client_id, None)
        services.client_metadata.pop(client_id, None)


async def broadcast_org_admin_ws(org_id: str, event_type: str, payload: dict):
    """Broadcast an organization event only to its administrators."""
    safe_payload = jsonable_encoder(payload)
    for client_id in list(services.org_clients.get(org_id, set())):
        if services.client_metadata.get(client_id, {}).get("role") not in {"admin", "org:admin"}:
            continue
        websocket = services.active_clients.get(client_id)
        if websocket:
            try:
                await websocket.send_json({"type": event_type, "data": safe_payload})
            except Exception:
                logger.error(f"Failed to send {event_type} to an organization administrator")

async def broadcast_on_stats_update(
    org_id: str, 
    target_date: str = None, 
    start_date: str = None, 
    end_date: str = None
):
    """
    Unified broadcast function for the HeidelAI Dashboard.
    Fetches full dashboard stats and broadcasts to connected clients.
    """
    from services.dashboard_service import aggregate_dashboard_stats
    async_db = get_async_mongo_db()
    
    import time
    start_time = time.time()
    logger.info(f"🚀 Starting dashboard stats broadcast for org: {org_id} (Range: {start_date} to {end_date} or Date: {target_date or 'today'})")
    
    full_payload = await aggregate_dashboard_stats(
        org_id, 
        target_date_str=target_date, 
        start_date_str=start_date, 
        end_date_str=end_date
    )
    if not full_payload:
        logger.warning(f"Empty payload for org {org_id}, skipping broadcast")
        return

    # Create a reduced payload for WebSocket to minimize traffic
    # Only includes summary metrics and alert headers
    stats_summary = full_payload.get("stats", {})
    status_counters = full_payload.get("statusCounters", {})
    # Remove heavy details from status counters if present
    status_counters.pop("expiringWhatsAppDetails", None)

    payload = {
        "stats": stats_summary,
        "leadPatrol": full_payload.get("leadPatrol", []),
        "overallMetrics": full_payload.get("overallMetrics", {}),
        "statusCounters": status_counters,
        "conversationFlow": full_payload.get("conversationFlow", []),
        "aiMetrics": full_payload.get("aiMetrics", {}),
        "alerts": full_payload.get("alerts", []), # Include full alert details (like hot leads)
        "newLeads": full_payload.get("newLeads", []) # Include the list of new leads
    }

    # Get organization metadata to find platform IDs
    org_data = await async_db.organizations.find_one({"org_id": org_id}, {"ig_id": 1, "wa_id": 1})
    if not org_data:
        logger.error(f"❌ Could not find organization data for broadcast: {org_id}")
        return
        
    ig_id = org_data.get("ig_id")
    wa_id = org_data.get("wa_id")
    
    if ig_id:
        await broadcast_main_ws(
            platform_id=ig_id,
            platform_type="instagram",
            event_type="onStatsUpdate",
            payload=payload
        )
    else:
        logger.warning(f"⚠️ No ig_id found for org {org_id}")
        
    if wa_id:
        await broadcast_main_ws(
            platform_id=wa_id,
            platform_type="whatsapp",
            event_type="onStatsUpdate",
            payload=payload
        )
    else:
        logger.warning(f"⚠️ No wa_id found for org {org_id}")

    end_time = time.time()
    logger.success(f"✅ Dashboard stats broadcast completed for {org_id} in {end_time - start_time:.2f}s")

async def broadcast_team_message(org_id: str, conversation_id: str, participants: list, payload: dict):
    """
    Broadcast an internal message to all online participants in a team chat.
    """
    logger.info(f"📡 Broadcasting team message to participants: {participants}")
    
    if not participants:
        logger.warning(f"⚠️ No participants found for broadcasting in conversation {conversation_id}")
        return

    safe_payload = jsonable_encoder(payload)
    
    for user_id in participants:
        if user_id in services.agent_clients:
            for client_id in list(services.agent_clients[user_id]):
                ws = services.active_clients.get(client_id)
                if ws:
                    try:
                        await ws.send_json({
                            "type": "new_team_message",
                            "data": {
                                "org_id": org_id,
                                "conversation_id": conversation_id,
                                **safe_payload
                            }
                        })
                        logger.success(f"Sent team message to agent {user_id} (client {client_id})")
                    except Exception as e:
                        logger.error(f"Failed to send team message to {client_id}: {e}")
                else:
                    logger.warning(f"WebSocket not found for active client {client_id}")
        else:
            pass

async def broadcast_ai_escalation(org_id: str, payload: dict):
    """
    Broadcast a new AI escalation to all online agents in an organization.
    """
    logger.info(f"📡 Broadcasting AI escalation to organization: {org_id}")
    
    client_ids = services.org_clients.get(org_id, [])
    if not client_ids:
        logger.warning(f"⚠️ No active clients found for organization {org_id}")
        return

    safe_payload = jsonable_encoder(payload)
    
    for client_id in list(client_ids):
        ws = services.active_clients.get(client_id)
        if ws:
            try:
                await ws.send_json({
                    "type": "new_ai_escalation",
                    "data": safe_payload
                })
                logger.success(f"Sent escalation alert to terminal {client_id}")
            except Exception as e:
                logger.error(f"Failed to send escalation to {client_id}: {e}")
        else:
            logger.warning(f"WebSocket not found for active client {client_id}")

async def send_to_user(user_id: str, payload: dict, org_id: str | None = None):
    """
    Sends a WebSocket notification payload directly to all active connection channels of a user.
    """
    if user_id in services.agent_clients:
        # Auto-wrap payload to ensure it matches the {type, data} format expected by frontend
        ws_payload = payload
        if "type" in payload and "data" not in payload:
            ws_payload = {
                "type": payload["type"],
                "data": {k: v for k, v in payload.items() if k != "type"}
            }

        safe_payload = jsonable_encoder(ws_payload)
        for client_id in list(services.agent_clients[user_id]):
            if org_id and services.client_metadata.get(client_id, {}).get("org_id") != org_id:
                continue
            ws = services.active_clients.get(client_id)
            if ws:
                try:
                    await ws.send_json(safe_payload)
                except Exception as e:
                    logger.error(f"Failed to send websocket message to user {user_id}: {e}")
