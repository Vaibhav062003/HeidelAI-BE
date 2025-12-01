import asyncio
from fastapi.encoders import jsonable_encoder
from core.services import services
from fastapi import WebSocket, WebSocketDisconnect

async def cleanup_client(client_id: str):
    """
    Clean up all references to a disconnected client.
    """
    print(f"🔴 Disconnecting client {client_id}")

    meta = services.client_metadata.get(client_id)
    if not meta:
        return

    ig_id = meta.get("ig_id")
    wa_id = meta.get("wa_id")

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

    print(f"✅ Cleaned up client {client_id}")

async def handle_typing_event(client_id: str, data: dict):
    """
    Broadcast typing indicators to all users under the same IG or WA account,
    excluding the sender.
    """
    print(f"[Typing Event] Client {client_id} sending typing: {data}")
    
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
    print(f"[Broadcast Main WS] 📡 Broadcasting {event_type} to {platform_type} clients under ID {platform_id}")
    if platform_type == "instagram":
        target_map = services.instagram_clients
    else:
        target_map = services.whatsapp_clients

    if platform_id not in target_map:
        return

    safe_payload = jsonable_encoder(payload)

    disconnected_clients = []

    for client_id in target_map[platform_id]:

        if exclude_client_id and client_id == exclude_client_id:
            continue

        ws = services.active_clients.get(client_id)
        if not ws:
            disconnected_clients.append(client_id)
            continue

        try:
            print(f"[Broadcast Main WS] ➡️ Sending {event_type} to client {client_id}")
            await ws.send_json({
                "type": event_type,
                "data":{
                    "platform": platform_type,
                    **safe_payload
                }
            })
        except Exception as e:
            print(f"[Broadcast Main WS] ⚠️ Failed to send to {client_id}: {e}, marking as disconnected")
        except WebSocketDisconnect:
            print(f"[Broadcast Main WS] ⚠️ Client {client_id} disconnected, marking as disconnected")
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
                    print(f"Start Heartbeat: Failed to ping {client_id}")
                    break
            else:
                break
    except asyncio.CancelledError:
        pass