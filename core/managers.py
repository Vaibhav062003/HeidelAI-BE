from fastapi import WebSocket
from typing import Dict
from loguru import logger


# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket, token: str) -> bool:
        await websocket.accept(subprotocol=token)
        if client_id in self.active_connections:
            await websocket.close(code=1008, reason="Client ID already connected")
            return False
        self.active_connections[client_id] = websocket
        logger.success(f"Client connected: {client_id}")
        return True

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.warning(f"Client disconnected: {client_id}")

    async def broadcast(self, message: dict):
        disconnected_clients = []
        for client_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to {client_id}: {e}")
                disconnected_clients.append(client_id)
        
        for client_id in disconnected_clients:
            self.disconnect(client_id)

# Initialize manager
manager = ConnectionManager()

# WebSocket manager for conversation summary connections
class ConnectionManager2:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, client_id: str, websocket: WebSocket, token: str) -> bool:
        await websocket.accept(subprotocol=token)
        if client_id in self.active_connections:
            await websocket.close(code=1008, reason="Client ID already connected")
            return False
        self.active_connections[client_id] = websocket
        logger.success(f"Client connected: {client_id}")
        return True

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.warning(f"Client disconnected: {client_id}")

    async def broadcast(self, message: dict):
        disconnected_clients = []
        for client_id, websocket in list(self.active_connections.items()):
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error broadcasting to {client_id}: {e}")
                disconnected_clients.append(client_id)
        
        for client_id in disconnected_clients:
            self.disconnect(client_id)

# Initialize manager
manager2 = ConnectionManager2()
