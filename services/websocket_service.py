import asyncio
from fastapi.encoders import jsonable_encoder
from core.services import services
from fastapi import WebSocket, WebSocketDisconnect
from database import get_mongo_db, get_async_mongo_db
from loguru import logger

from services.wa_service import read_message_and_typing_indicator

db = get_mongo_db()

async def cleanup_client(client_id: str):
    """
    Clean up all references to a disconnected client.
    """
    logger.info(f"Disconnecting client {client_id}")

    # Retrieve and remove the entry from the reverse map
    convo_key = services.client_convo_map.pop(client_id, None)
    
    if convo_key:
        logger.info(f"Cleaning up presence data for conversation {convo_key}")
        # Remove client_id from the conversation viewers set
        if convo_key in services.convo_viewers:
            services.convo_viewers[convo_key].discard(client_id)
            
            # Cleanup: Remove the conversation entry if the set is empty
            if not services.convo_viewers[convo_key]:
                del services.convo_viewers[convo_key]

    meta = services.client_metadata.get(client_id)
    if not meta:
        return

    ig_id = meta.get("ig_id")
    wa_id = meta.get("wa_id")
    user_id = meta.get("user_id")

    if user_id and client_id in services.agent_clients.get(user_id, set()):
        services.agent_clients[user_id].remove(client_id)
        if not services.agent_clients[user_id]:
            del services.agent_clients[user_id]

    org_id = meta.get("org_id")
    if org_id and client_id in services.org_clients.get(org_id, set()):
        services.org_clients[org_id].remove(client_id)
        if not services.org_clients[org_id]:
            del services.org_clients[org_id]

    if ig_id and client_id in services.instagram_clients[ig_id]:
        services.instagram_clients[ig_id].remove(client_id)
        if not services.instagram_clients[ig_id]:
            del services.instagram_clients[ig_id]

    if wa_id and client_id in services.whatsapp_clients[wa_id]:
        services.whatsapp_clients[wa_id].remove(client_id)
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
        await read_message_and_typing_indicator(
            mode="platform",
            whatsapp_id=wa_id,
        )
        await broadcast_main_ws(
            platform_id=wa_id,
            platform_type="whatsapp",
            event_type="typing_indicator",
            payload=data,
            exclude_client_id=client_id 
        )

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

def handle_unread_event(org_id: str, client_id: str, data: dict):
    """
    Handles join and leave conversation events for unread message tracking.
    """

    if data.get("type") == "join_conversation":
        conversation_id = data.get("conversation_id")
        join_conversation_presence(org_id, conversation_id, client_id)
    elif data.get("type") == "leave_conversation":
        conversation_id = data.get("conversation_id")
        leave_conversation_presence(org_id, conversation_id, client_id)
    else:
        logger.error(f"Unknown unread event type: {data.get('type')}")

def join_conversation_presence(org_id: str, conversation_id: str, client_id: str):
    """
    Adds a client to the presence tracker for a specific conversation 
    using the multi-tenant key (org_id, conversation_id).
    """
    convo_key = (org_id, conversation_id)
    
    # Updating forward map (convo_viewers: (org, convo) -> {clients})
    if convo_key not in services.convo_viewers:
        services.convo_viewers[convo_key] = set()
    services.convo_viewers[convo_key].add(client_id)
    
    # Updating reverse map (client_convo_map: client -> (org, convo))
    services.client_convo_map[client_id] = convo_key
    
    logger.info(f"Client {client_id} JOINED conversation {convo_key}")

def leave_conversation_presence(org_id: str, conversation_id: str, client_id: str):
    """
    Removes a client from the presence tracker and cleans up the maps.
    """
    convo_key = (org_id, conversation_id)
    
    # Updating forward map (convo_viewers)
    if convo_key in services.convo_viewers:
        services.convo_viewers[convo_key].discard(client_id)
        
        # Cleanup: Removing the conversation entry if no one is viewing it
        if not services.convo_viewers[convo_key]:
            del services.convo_viewers[convo_key]
    
    # Removing from reverse map (client_convo_map)
    services.client_convo_map.pop(client_id, None)
    
    logger.info(f"Client {client_id} LEFT conversation {convo_key}")

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
