# services.py
from typing import Dict, Set, List
from collections import OrderedDict
from fastapi import WebSocket
from langgraph.graph.state import CompiledStateGraph

class Services:

    def __init__(self):
        # Constants
        self.EXPIRY_TIME_SECONDS = 86400
        self.BATCH_SIZE = 1000

        # In-memory stores
        self.address_details = []
        self.responded_users_by_media = {}
        self.processed_message_ids = OrderedDict()
        self.active_connections: List[WebSocket] = []
        self.whatsapp_connections_map: Dict[str, Set[str]] = {}
        self.instagram_connections_map: Dict[str, Set[str]] = {}

        # === WebSocket State ===
        self.connected_clients: Dict[str, WebSocket] = {}
        self.websocket_product_data = {}
        self.websocket_product_data_ig = {}
        self.flag = False
        self.search_term = ""

        # === Agents ===
        self.prompt_cache = {}
        self.agents: Dict[str, CompiledStateGraph] = {}

services = Services()
