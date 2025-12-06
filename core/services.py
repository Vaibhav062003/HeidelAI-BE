# services.py
from typing import Dict, Set, List
from collections import OrderedDict, defaultdict
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
        self.processed_message_reaction_ids = OrderedDict()
        self.active_connections: List[WebSocket] = []
        self.whatsapp_connections_map: Dict[str, Set[str]] = {}
        self.instagram_connections_map: Dict[str, Set[str]] = {}

        self.convo_viewers: dict[tuple, set[str]] = {} # {(org_id, convo_id) : ( client_id1, client_id2, ... ), ...}
        # Key: client_id (str).
        # Value: (org_id: str, conversation_id: str) TUPLE this client is viewing.
        self.client_convo_map: dict[str, tuple] = {} # dict[client_id, (org_id, conversation_id)]

        # === WebSocket State ===
        self.connected_clients: Dict[str, WebSocket] = {}
        self.websocket_product_data = {}
        self.websocket_product_data_ig = {}
        self.flag = False
        self.search_term = ""

         # === New Main WebSocket State ===
        self.active_clients = {}    # GLOBAL: All connected WebSocket connections -> { client_id: WebSocket }
        self.instagram_clients = defaultdict(set) # For Instagram event broadcasting -> { ig_id: { client_id1, client_id2 } }
        self.whatsapp_clients = defaultdict(set) # For WhatsApp event broadcasting -> { wa_id: { client_id1, client_id2 } }
        self.client_metadata = {}  # Reverse mapping (for cleanup) -> { client_id: { "ig_id": "...", "wa_id": "..." } }

        # === Agents ===
        self.prompt_cache = {}
        self.agents: Dict[str, CompiledStateGraph] = {}

services = Services()
