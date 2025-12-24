import json
from core.managers import manager, manager2
from database import get_mongo_db
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.services import services
from auth.dependencies import get_current_user

from fastapi.security import HTTPAuthorizationCredentials
import json

from services.websocket_service import *
from core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Websockets"])

db = get_mongo_db()

# Add a new WebSocket endpoint for Instagram
@router.websocket("/ws2/instagram/{client_id}")
async def websocket_endpoint_instagram(websocket: WebSocket, client_id: str):
    """WebSocket endpoint that uses both client_id and instagram_id for Instagram-specific connections"""

    token = websocket.headers.get("sec-websocket-protocol")
    instagram_id = None

    try:
        # Validate user before accepting connection
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        user = await get_current_user(credentials)
           
        org_id = user.org_id

        instagram_id = db.organizations.find_one({"org_id": org_id}, {"ig_id": 1}).get("ig_id")
        
        if not instagram_id:
            logger.error("No Instagram ID found for organization")
            await websocket.close(code=1008, reason="No Instagram ID configured")
            return

        # Add to manager after accepting
        await manager.connect(client_id, websocket, token)

    except Exception as e:
        import traceback
        logger.error(f"Authentication error: {str(e)}")
        # print(traceback.format_exc())  # Print full stack trace
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
            data = await websocket.receive_json()
            logger.info(f"Received message from Instagram connection {client_id}: {data}")
            msg_type = data.get("type")
                          
            if msg_type == "typing_indicator":
                logger.info(f"✍️ Typing event from Instagram {client_id}: {data}")
                if instagram_id in services.instagram_connections_map:
                    for other_client_id in services.instagram_connections_map[instagram_id]:
                        if other_client_id != client_id:
                            
                            other_ws = manager.active_connections.get(other_client_id)
                            if other_ws:
                                try:
                                    await other_ws.send_json(data)
                                except Exception as e:
                                    logger.error(f"Error sending typing indicator to {other_client_id}: {e}")



    except WebSocketDisconnect:
        manager.disconnect(client_id)
        # Clean up Instagram ID mapping
        if instagram_id and instagram_id in services.instagram_connections_map:
            services.instagram_connections_map[instagram_id].discard(client_id)
            if not services.instagram_connections_map[instagram_id]:
                del services.instagram_connections_map[instagram_id]

# WebSocket endpoint for WhatsApp
@router.websocket("/ws2/whatsapp/{client_id}/{whatsapp_id}")
async def websocket_endpoint_whatsapp(websocket: WebSocket, client_id: str, whatsapp_id: str):
    """WebSocket endpoint for WhatsApp-specific connections"""

    protocol = websocket.headers.get("sec-websocket-protocol")

    await websocket.accept(subprotocol=protocol)

    # query whatsapp_connections to retrieve whatsapp_business_id based on user_id
    wa_connection = db.whatsapp_connections.find_one(
            {"user_id": whatsapp_id},
            {"wa_id": 1, "_id": 0}
        )

    if not wa_connection or not wa_connection.get("wa_id"):
        logger.error(f"No WhatsApp connection found for user ID: {whatsapp_id}")
        await websocket.close(code=1008, reason="Invalid WhatsApp connection")
        return
    
    whatsapp_business_id = wa_connection["wa_id"]

    # Create a combined key for WhatsApp connections
    connection_key = f"{client_id}_{whatsapp_business_id}"
    await manager.connect(connection_key, websocket)
    
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
            data = await websocket.receive_json()
            logger.info(f"Received message from WhatsApp connection {connection_key}: {data}")
    except WebSocketDisconnect:
        manager.disconnect(connection_key)
        # Clean up WhatsApp ID mapping
        if whatsapp_business_id and whatsapp_business_id in services.whatsapp_connections_map:
            services.whatsapp_connections_map[whatsapp_business_id].discard(connection_key)
            if not services.whatsapp_connections_map[whatsapp_business_id]:
                del services.whatsapp_connections_map[whatsapp_business_id]

@router.websocket("/cs/{client_id}")
async def websocket_endpoint(websocket: WebSocket):
    # Retrieve the path parameter from websocket.path_params
    client_id = websocket.path_params["client_id"]
    await manager2.connect(client_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            logger.info(f"Received message from {client_id}: {data}")
    except WebSocketDisconnect:
        manager2.disconnect(client_id)

@router.websocket("/main-ws/{client_id}")
async def main_ws(websocket: WebSocket, client_id: str):
    token = websocket.headers.get("sec-websocket-protocol")

    # Step 1: Authenticate using the token
    try:
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer", 
            credentials=token
        )
        user = await get_current_user(credentials)
    except Exception:
        await websocket.close(code=1008, reason="Unauthorized")
        return

    # Step 2: Accept websocket
    await websocket.accept(subprotocol=token)
    
    org = db.organizations.find_one(
        {"org_id": user.org_id},
        {"ig_id": 1, "wa_id": 1, "_id": 0}
    )

    ig_id = org.get("ig_id")
    wa_id = org.get("wa_id")


    # Step 3: Register connection globally
    services.active_clients[client_id] = websocket
    services.client_metadata[client_id] = {
        "ig_id": ig_id,
        "wa_id": wa_id,
    }

    if ig_id:
        services.instagram_clients[ig_id].add(client_id)

    if wa_id:
        services.whatsapp_clients[wa_id].add(client_id)

    logger.success(f"Connected client: {client_id}")
    logger.success(f"IG: {ig_id}, WA: {wa_id}")

    heartbeat_task = asyncio.create_task(start_heartbeat(client_id, websocket))

   # Step 4: Keep listening for incoming messages
    try:
        while True:
            # 1. Use receive_text first to see raw data (debugs JSON errors)
            try:
                raw_data = await websocket.receive_text()
                logger.info(f"Raw Data Received: {raw_data}")

                # 2. Manually parse JSON
                data = json.loads(raw_data)
                
                event_type = data.get("type")

                if event_type == "join_conversation" or event_type == "leave_conversation":
                    org_id = user.org_id
                    handle_unread_event(org_id, client_id, data)

                if event_type == "typing_indicator":
                    await handle_typing_event(client_id, data)
                
            except WebSocketDisconnect:
                # 🛑 STOP the loop if the client disconnects
                logger.warning(f"Client {client_id} disconnected (caught in loop)")
                break

            except json.JSONDecodeError:
                logger.error(f"❌ Failed to decode JSON: {raw_data}")
            except Exception as inner_e:
                logger.error(f"❌ Error processing message loop: {inner_e}")
                # We catch here so the loop CONTINUES even if one message fails

    except WebSocketDisconnect:
        logger.warning(f"Client {client_id} disconnected")
        await cleanup_client(client_id)
    except Exception as e:
        logger.error(f"❗ Critical Websocket Error: {e}")
        await cleanup_client(client_id)
    finally:
        heartbeat_task.cancel()
        await cleanup_client(client_id)