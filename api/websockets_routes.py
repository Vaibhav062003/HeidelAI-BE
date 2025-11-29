from core.managers import manager, manager2
from database import get_mongo_db
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.services import services
from auth.dependencies import get_current_user

from fastapi.security import HTTPAuthorizationCredentials
import json

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
            print("No Instagram ID found for organization")
            await websocket.close(code=1008, reason="No Instagram ID configured")
            return

        # Add to manager after accepting
        await manager.connect(client_id, websocket, token)

    except Exception as e:
        import traceback
        print(f"Authentication error: {str(e)}")
        # print(traceback.format_exc())  # Print full stack trace
        await websocket.close(code=1008, reason="Authentication failed")
        return

    try:
        # Store the Instagram ID mapping for notifications
        if instagram_id not in services.instagram_connections_map:
            services.instagram_connections_map[instagram_id] = set()
        services.instagram_connections_map[instagram_id].add(client_id)

        print(f"Registered Instagram connection for ID: {instagram_id}")
        
        # Handle incoming messages
        while True:
            data = await websocket.receive_json()
            print(f"Received message from Instagram connection {client_id}: {data}")
            msg_type = data.get("type")
                          
            if msg_type == "typing_indicator":
                print(f"✍️ Typing event from Instagram {client_id}: {data}")
                if instagram_id in services.instagram_connections_map:
                    for other_client_id in services.instagram_connections_map[instagram_id]:
                        if other_client_id != client_id:
                            
                            other_ws = manager.active_connections.get(other_client_id)
                            if other_ws:
                                try:
                                    await other_ws.send_json(data)
                                except Exception as e:
                                    print(f"Error sending typing indicator to {other_client_id}: {e}")



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
        print(f"No WhatsApp connection found for user ID: {whatsapp_id}")
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

            print(f"Registered WhatsApp connection for ID: {connection_key}")
        
        # Handle incoming messages
        while True:
            data = await websocket.receive_json()
            print(f"Received message from WhatsApp connection {connection_key}: {data}")
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
            print(f"Received message from {client_id}: {data}")
    except WebSocketDisconnect:
        manager2.disconnect(client_id)
