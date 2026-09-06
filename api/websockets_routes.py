import asyncio
from contextlib import suppress
from core.managers import manager, manager2
from database import get_mongo_db
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.services import services
from auth.dependencies import authenticate_credentials
from services.websocket_service import (
    InvalidWebSocketControlFrame,
    cleanup_client,
    get_org_presence_snapshot,
    handle_typing_event,
    handle_unread_event,
    receive_websocket_control_frame,
    start_heartbeat,
    validate_websocket_identifier,
    websocket_client_id_in_use,
)

from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger


router = APIRouter(tags=["Websockets"])

db = get_mongo_db()


def _active_account_ids(default_id, connections, field: str) -> set[str]:
    ids = {str(connection.get(field) or "") for connection in connections}
    ids.discard("")
    if not ids and default_id:
        ids.add(str(default_id))
    return ids


# Add a new WebSocket endpoint for Instagram
@router.websocket("/ws2/instagram/{client_id}")
async def websocket_endpoint_instagram(websocket: WebSocket, client_id: str):
    """WebSocket endpoint that uses both client_id and instagram_id for Instagram-specific connections"""

    token = websocket.headers.get("sec-websocket-protocol")
    instagram_id = None

    try:
        validate_websocket_identifier(client_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid client ID")
        return

    try:
        # Validate user before accepting connection
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        user = await authenticate_credentials(credentials)
           
        org_id = user.org_id

        org = await asyncio.to_thread(db.organizations.find_one, {"org_id": org_id}, {"ig_id": 1})
        instagram_id = (org or {}).get("ig_id")
        
        if not instagram_id:
            logger.error("No Instagram ID found for organization")
            await websocket.close(code=1008, reason="No Instagram ID configured")
            return

        # Add to manager after accepting
        if websocket_client_id_in_use(client_id):
            await websocket.close(code=1008, reason="Client ID already connected")
            return
        if not await manager.connect(client_id, websocket, token):
            return

    except Exception as error:
        logger.error(f"Authentication error: {type(error).__name__}")
        await websocket.close(code=1008, reason="Authentication failed")
        return

    try:
        # Store the Instagram ID mapping for notifications
        if instagram_id not in services.instagram_connections_map:
            services.instagram_connections_map[instagram_id] = set()
        services.instagram_connections_map[instagram_id].add(client_id)

        logger.success(f"Registered Instagram connection for ID: {instagram_id}")
        
        # Handle incoming messages
        while True:
            data = await receive_websocket_control_frame(websocket)
            msg_type = data.get("type")
                          
            if msg_type == "typing_indicator":
                if instagram_id in services.instagram_connections_map:
                    for other_client_id in list(services.instagram_connections_map[instagram_id]):
                        if other_client_id != client_id:
                            
                            other_ws = manager.active_connections.get(other_client_id)
                            if other_ws:
                                try:
                                    await other_ws.send_json(data)
                                except Exception:
                                    logger.error(f"Error sending typing indicator to {other_client_id}")
                                    await cleanup_client(other_client_id)

    except InvalidWebSocketControlFrame as error:
        logger.warning(f"Rejected Instagram control frame from {client_id}: {error}")
        await websocket.close(code=error.close_code, reason=str(error))
    except WebSocketDisconnect:
        logger.info(f"Instagram client {client_id} disconnected")
    except Exception as error:
        logger.error(f"Instagram websocket failed: {type(error).__name__}")
    finally:
        await cleanup_client(client_id)

# WebSocket endpoint for WhatsApp
@router.websocket("/ws2/whatsapp/{client_id}/{whatsapp_id}")
async def websocket_endpoint_whatsapp(websocket: WebSocket, client_id: str, whatsapp_id: str):
    """WebSocket endpoint for WhatsApp-specific connections"""

    protocol = websocket.headers.get("sec-websocket-protocol")

    try:
        validate_websocket_identifier(client_id)
        validate_websocket_identifier(whatsapp_id)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=protocol)
        user = await authenticate_credentials(credentials)
    except Exception as error:
        logger.warning(f"WhatsApp websocket rejected: {type(error).__name__}")
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # query whatsapp_connections to retrieve whatsapp_business_id based on user_id
    wa_connection = await asyncio.to_thread(db.whatsapp_connections.find_one,
            {"user_id": whatsapp_id, "org_id": user.org_id, "is_active": {"$ne": False}},
            {"wa_id": 1, "_id": 0}
        )

    if not wa_connection or not wa_connection.get("wa_id"):
        logger.error("No active WhatsApp connection found for authenticated organization")
        await websocket.close(code=1008, reason="Invalid WhatsApp connection")
        return
    
    whatsapp_business_id = str(wa_connection["wa_id"])
    try:
        validate_websocket_identifier(whatsapp_business_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid WhatsApp connection")
        return

    # Create a combined key for WhatsApp connections
    connection_key = f"{client_id}_{whatsapp_business_id}"
    try:
        validate_websocket_identifier(connection_key)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid client ID")
        return
    if websocket_client_id_in_use(connection_key):
        await websocket.close(code=1008, reason="Client ID already connected")
        return
    if not await manager.connect(connection_key, websocket, protocol):
        return
    
    try:
        # Store the WhatsApp ID mapping for notifications
        if whatsapp_business_id:
            # Add to a dictionary that maps WhatsApp IDs to connection keys
            if whatsapp_business_id not in services.whatsapp_connections_map:
                services.whatsapp_connections_map[whatsapp_business_id] = set()
            services.whatsapp_connections_map[whatsapp_business_id].add(connection_key)

            logger.success(f"Registered WhatsApp connection for ID: {connection_key}")
        
        # Handle incoming messages
        while True:
            await receive_websocket_control_frame(websocket)
    except InvalidWebSocketControlFrame as error:
        logger.warning(f"Rejected WhatsApp control frame from {connection_key}: {error}")
        await websocket.close(code=error.close_code, reason=str(error))
    except WebSocketDisconnect:
        logger.info(f"WhatsApp client {connection_key} disconnected")
    except Exception as error:
        logger.error(f"WhatsApp websocket failed: {type(error).__name__}")
    finally:
        await cleanup_client(connection_key)

@router.websocket("/cs/{client_id}")
async def websocket_endpoint(websocket: WebSocket):
    # Retrieve the path parameter from websocket.path_params
    client_id = websocket.path_params["client_id"]
    token = websocket.headers.get("sec-websocket-protocol")
    try:
        validate_websocket_identifier(client_id)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        await authenticate_credentials(credentials)
    except Exception:
        await websocket.close(code=1008, reason="Unauthorized")
        return
    if websocket_client_id_in_use(client_id):
        await websocket.close(code=1008, reason="Client ID already connected")
        return
    if not await manager2.connect(client_id, websocket, token):
        return
    try:
        while True:
            await receive_websocket_control_frame(websocket)
    except InvalidWebSocketControlFrame as error:
        logger.warning(f"Rejected control frame from {client_id}: {error}")
        await websocket.close(code=error.close_code, reason=str(error))
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
    except Exception as error:
        logger.error(f"Websocket failed: {type(error).__name__}")
    finally:
        await cleanup_client(client_id)

#TODO: Implement a ping-pong mechanism to kill dead connections
@router.websocket("/main-ws/{client_id}")
async def main_ws(websocket: WebSocket, client_id: str):
    token = websocket.headers.get("sec-websocket-protocol")

    try:
        validate_websocket_identifier(client_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid client ID")
        return

    # Authenticate using the token
    try:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", 
            credentials=token
        )
        user = await authenticate_credentials(credentials)
    except Exception:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # Step 2: Accept websocket
    await websocket.accept(subprotocol=token)
    if websocket_client_id_in_use(client_id):
        await websocket.close(code=1008, reason="Client ID already connected")
        return
    heartbeat_task = None
    registered = False
    try:
        org = await asyncio.to_thread(db.organizations.find_one,
            {"org_id": user.org_id},
            {"ig_id": 1, "wa_id": 1, "_id": 0}
        ) or {}
        ig_ids = await asyncio.to_thread(_active_account_ids,
            org.get("ig_id"),
            db.instagram_connections.find(
                {"org_id": user.org_id, "is_active": {"$ne": False}},
                {"instagram_id": 1, "_id": 0},
            ),
            "instagram_id",
        )
        wa_ids = await asyncio.to_thread(_active_account_ids,
            org.get("wa_id"),
            db.whatsapp_connections.find(
                {"org_id": user.org_id, "is_active": {"$ne": False}},
                {"wa_id": 1, "_id": 0},
            ),
            "wa_id",
        )
        ig_id = str(org.get("ig_id") or next(iter(ig_ids), "")) or None
        wa_id = str(org.get("wa_id") or next(iter(wa_ids), "")) or None

        # Database awaits allow another handshake with this ID to finish first.
        if websocket_client_id_in_use(client_id):
            await websocket.close(code=1008, reason="Client ID already connected")
            return
        services.active_clients[client_id] = websocket
        registered = True
        services.client_metadata[client_id] = {
            "ig_id": ig_id,
            "wa_id": wa_id,
            "ig_ids": sorted(ig_ids),
            "wa_ids": sorted(wa_ids),
            "user_id": user.user_id,
            "org_id": user.org_id,
            "role": user.role,
            "agent_name": user.full_name,
        }
        from services.work_team_service import record_team_presence
        await record_team_presence(user.org_id, user.user_id)

        services.org_clients[user.org_id].add(client_id)
        if user.user_id:
            services.agent_clients[user.user_id].add(client_id)
        for account_id in ig_ids:
            services.instagram_clients[account_id].add(client_id)
        for account_id in wa_ids:
            services.whatsapp_clients[account_id].add(client_id)

        logger.success(f"Connected client: {client_id}")
        logger.success(f"Registered {len(ig_ids)} Instagram and {len(wa_ids)} WhatsApp account(s)")

        await websocket.send_json({
            "type": "presence_snapshot",
            "data": get_org_presence_snapshot(user.org_id),
        })
        heartbeat_task = asyncio.create_task(start_heartbeat(client_id, websocket))

        while True:
            data = await receive_websocket_control_frame(websocket)
            event_type = data.get("type")
            if event_type in {"join_conversation", "leave_conversation"}:
                await handle_unread_event(user.org_id, client_id, data)
            elif event_type == "typing_indicator":
                await handle_typing_event(client_id, data)
    except InvalidWebSocketControlFrame as error:
        logger.warning(f"Rejected control frame from {client_id}: {error}")
        with suppress(Exception):
            await websocket.close(code=error.close_code, reason=str(error))
    except WebSocketDisconnect:
        logger.warning(f"Client {client_id} disconnected")
    except Exception as error:
        logger.error(f"Critical websocket error: {type(error).__name__}")
        with suppress(Exception):
            await websocket.close(code=1011, reason="Internal websocket error")
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if registered:
            await cleanup_client(client_id)
