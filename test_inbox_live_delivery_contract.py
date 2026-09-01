import ast
from pathlib import Path


websocket_routes = ast.parse(Path("api/websockets_routes.py").read_text())
account_helper = next(
    node for node in websocket_routes.body
    if isinstance(node, ast.FunctionDef) and node.name == "_active_account_ids"
)
namespace = {}
exec(compile(ast.fix_missing_locations(ast.Module(body=[account_helper], type_ignores=[])), "accounts", "exec"), namespace)
assert namespace["_active_account_ids"]("default", [{"wa_id": "one"}, {"wa_id": "two"}], "wa_id") == {"one", "two"}
assert namespace["_active_account_ids"]("default", [], "wa_id") == {"default"}

websocket_service = ast.parse(Path("services/websocket_service.py").read_text())
payload_helper = next(
    node for node in websocket_service.body
    if isinstance(node, ast.FunctionDef) and node.name == "_payload_conversation_id"
)
namespace = {}
exec(compile(ast.fix_missing_locations(ast.Module(body=[payload_helper], type_ignores=[])), "payload", "exec"), namespace)
extract = namespace["_payload_conversation_id"]
assert extract({"conversation_id": "top"}) == "top"
assert extract({"message": {"conversation_id": "message"}}) == "message"
assert extract({"conversation": {"conversation_id": "conversation"}}) == "conversation"
assert extract({"message": {}}) is None

routes_source = Path("api/websockets_routes.py").read_text()
service_source = Path("services/websocket_service.py").read_text()
assert "for account_id in ig_ids" in routes_source
assert "for account_id in wa_ids" in routes_source
assert '"ig_ids": sorted(ig_ids)' in routes_source
assert '"wa_ids": sorted(wa_ids)' in routes_source
assert "services.instagram_clients," in service_source
assert "services.whatsapp_clients," in service_source
assert "_discard_client(index, client_id)" in service_source
assert "_payload_conversation_id(payload)" in service_source

print("Inbox live delivery contract passed.")
