from typing import Dict, Set, List
from collections import OrderedDict
from fastapi import WebSocket

# Constants
EXPIRY_TIME_SECONDS = 86400

# In-memory stores
address_details = []
responded_users_by_media = {}
processed_message_ids = OrderedDict()
active_connections: List[WebSocket] = []
whatsapp_connections_map: Dict[str, Set[str]] = {}
instagram_connections_map: Dict[str, Set[str]] = {}

# === WebSocket State ===
connected_clients: Dict[str, WebSocket] = {}
websocket_product_data = {}
websocket_product_data_ig = {}
flag = False
search_term = ""