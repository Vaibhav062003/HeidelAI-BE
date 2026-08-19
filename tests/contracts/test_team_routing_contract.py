from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def source(path: str) -> str:
    return (ROOT / path).read_text()


service = source("services/work_team_service.py")
routes = source("api/transfer_routes.py")
common = source("services/common_service.py")
websocket = source("services/websocket_service.py")
push = source("services/push_notification_service.py")

assert 'team.get("type") == "sales"' in service
assert '"active_work": {"$elemMatch"' in service
assert 'conversation.get("owner_team_id") in team_ids' not in service
assert "record_team_presence" in service and "active_today_member_ids" in service
assert "can_claim_conversation_ownership" in service
assert "if is_admin(role):\n        return True" not in service.split("async def can_claim_conversation_ownership", 1)[1].split("async def", 1)[0]
assert 'updates["assigned_agent_id"] = user_id' in service
assert 'status in {"completed", "cancelled"}' in service
assert '"$pull": {"active_work": {"work_id": work_id}}' in service
assert "except Exception:" in service and "_restore_active_conversation_work" in service
assert "target_team_id" in routes and "target_agent_id" in routes
assert "require_conversation_access" in routes
assert "create_conversation_work(" in routes
assert '"owner_team_id": team_id' not in common
assert "team_queue_condition" in common
assert "if await can_claim_conversation_ownership" in common
assert "_eligible_conversation_clients" in websocket
assert "require_conversation_access" in websocket
assert 'get("org_id") != org_id' in websocket
assert "org_id=user.org_id" in routes
assert "can_user_access_conversation" in push and '"role": role' in push

print("Team routing contract passed.")
