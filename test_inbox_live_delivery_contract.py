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
whatsapp_source = Path("services/wa_service.py").read_text()
assert "for account_id in ig_ids" in routes_source
assert "for account_id in wa_ids" in routes_source
assert '"ig_ids": sorted(ig_ids)' in routes_source
assert '"wa_ids": sorted(wa_ids)' in routes_source
assert "services.instagram_clients," in service_source
assert "services.whatsapp_clients," in service_source
assert "_discard_client(index, client_id)" in service_source
assert "_payload_conversation_id(payload)" in service_source
media_start = whatsapp_source.index("async def send_wa_media_message")
media_sender = whatsapp_source[media_start:whatsapp_source.index("\nasync def ", media_start + 1)]
assert 'recipient_id=connection["wa_id"]' in media_sender
assert '"timestamp": message["timestamp"].isoformat()' in whatsapp_source

webhook_source = Path("api/webhook_routes.py").read_text()
deploy_source = Path(".github/workflows/deploy-main.yml").read_text()
docker_source = Path("Dockerfile").read_text()
common_source = Path("api/common_routes.py").read_text()
assert 'DEV_WHATSAPP_PHONE_IDS = {"1248072875064563"}' in webhook_source
assert 'get("phone_number_id")' in webhook_source
assert "phone_number_ids & DEV_WHATSAPP_PHONE_IDS" in webhook_source
assert "if not await forward_webhook_to_dev" in webhook_source
assert "return Response(status_code=503)" in webhook_source
assert "background_tasks.add_task(forward_webhook_to_dev" in webhook_source
assert "backend:sha-${{ github.sha }}" in deploy_source
assert "koyeb service update backend/backend" in deploy_source
assert 'revision == $revision' in deploy_source
assert "ARG GIT_SHA=unknown" in docker_source
assert "ENV GIT_SHA=${GIT_SHA}" in docker_source
assert '@router.get("/health")' in common_source
assert 'os.getenv("GIT_SHA", "unknown")' in common_source

print("Inbox live delivery contract passed.")
