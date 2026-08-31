"""Dependency-free contract checks for the multi-number WhatsApp boundary."""

import ast
from pathlib import Path


ROOT = Path(__file__).parent


def source(path: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    ast.parse(text, filename=path)
    return text


def test_connection_and_webhook_contracts():
    routes = source("api/wa_routes.py")
    webhook = source("api/webhook_routes.py")
    connection_service = source("services/whatsapp_connection_service.py")

    assert '@router.get("/connections")' in routes
    assert '@router.patch("/connections/{connection_id}")' in routes
    assert '"connections": public_connections(connections)' in routes
    assert '"historical_connections": public_historical_connections(historical)' in routes
    assert 'phone_number_id=req.phone_id or req.phone_number_id' in routes
    assert '@router.post("/connections/{phone_id}/primary")' in routes
    assert routes.count("await admin_required(user)") >= 4
    auth_source = routes[routes.index("async def whatsapp_auth_exchange"):routes.index('@router.post("/disconnect")')]
    assert "await meta_client.post" in auth_source
    assert "await meta_client.get" in auth_source
    assert "token_response = requests" not in auth_source
    assert "phone_response = requests" not in auth_source
    assert "webhook_response = requests" not in auth_source
    assert auth_source.index("pending_reconnect") < auth_source.index("token_url")
    details_source = routes[routes.index("async def get_whatsapp_details"):routes.index('@router.get("/{whatsapp_id}/analytics/conversations")')]
    assert "user: CurrentUser" in details_source
    assert "_find_owned_whatsapp_connection(user.org_id" in details_source
    assert "httpx.AsyncClient" in details_source
    assert "requests.get" not in details_source
    assert 'headers = {"Authorization": f"Bearer {connection[\'access_token\']}"}' in details_source
    assert 'status_code=409, detail="Meta access expired. Reconnect this WhatsApp number."' in routes
    assert 'error.get("code") == 190' in routes
    assert 'requested_phone_id = str(req.phone_number_id or "").strip()' in routes
    assert '(value.get("metadata") or {}).get("phone_number_id")' in webhook
    assert "active_whatsapp_connection_candidates(whatsapp_business_id)" in webhook
    assert "status_code=503" in webhook
    assert 'connection.get("wa_bot_flow_id")' in webhook
    assert 'business_phone_id=business_phone_id' in webhook
    assert 'len(value) > 24' in connection_service
    assert 'MAX_WHATSAPP_CONNECTIONS = 50' in connection_service


def test_scoped_archive_and_cleanup_contracts():
    service = source("services/wa_service.py")
    platform = source("services/platforms/whatsapp_service.py")
    scheduler = source("services/scheduler_service.py")

    assert 'phone_filter = {"platform": "whatsapp", "business_phone_id": phone_id}' in service
    assert 'primary_phone_id == str(business_phone_id) and (primary or {}).get("slot") == 1' in service
    assert '"business_phone_id": str(connection.get("phone_id")' in service
    assert 'db[conversations_collection_name].delete_many(' in service
    assert '{"conversation_id": {"$in": conversation_ids}}, session=session' in service
    assert 'archived["note_id"] = str(archived.get("note_id") or archived.get("_id"))' in service
    assert "async def deactivate_whatsapp_connection" in service
    assert '"is_active": False' in service
    assert "archive_whatsapp_data" not in platform
    assert 'archived_private_notes_' in scheduler
    assert 'drop_collection(archived_notes_col)' in scheduler
    assert "async def finalize_whatsapp_revocation" in service
    assert '"provider_revoke_status": {"$in": ["pending", "failed"]}' in service
    assert "async def run_whatsapp_revocation_reconciler" in service
    assert "asyncio.create_task(run_whatsapp_revocation_reconciler())" in source("core/lifespan.py")


def test_deployment_runs_the_verified_migration_before_serving():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    migration = source("scripts/migrate_multi_whatsapp.py")

    assert "python -m scripts.migrate_multi_whatsapp --apply && exec uvicorn" in dockerfile
    assert migration.index('plan["verified_indexes"] = True') < migration.index(
        '{"_id": MIGRATION_ID},\n        {"$set": {"status": "complete"'
    )


def test_number_scoped_query_and_flow_binding_contracts():
    common_routes = source("api/common_routes.py")
    common_service = source("services/common_service.py")
    contacts_service = source("services/contacts_service.py")
    flow_service = source("services/wa_bot_service.py")
    wa_service = source("services/wa_service.py")

    assert common_routes.count("business_phone_id: Optional[str]") >= 4
    assert 'conditions.append(_business_phone_scope(org_id, business_phone_id))' in common_service
    assert 'base_conditions.append(_business_phone_scope(org_id, business_phone_id))' in common_service
    assert '"business_phone_id": doc.get("business_phone_id")' in common_service
    assert common_service.count("_business_phone_scope(org_id, business_phone_id)") == 4
    assert '"wa_bot_flow_id": flow_id' in flow_service
    assert '"phone_ids", "wa_bot_flow_id"' in flow_service
    assert '"conversation_ids": contact_data["conversation_id"]' in contacts_service
    assert "conversation_match.update(whatsapp_conversation_scope(" in contacts_service
    assert '*(contact.get("conversation_ids") or [])' in common_service
    assert 'conversation_phone.get(str(item.get("_id") or ""), "")' in wa_service


def test_primary_phone_scope_keeps_legacy_conversations_visible():
    from services.whatsapp_connection_service import (
        choose_connection_slot,
        public_historical_connections,
        public_connections,
        whatsapp_conversation_scope,
    )

    assert whatsapp_conversation_scope("primary", include_legacy=True) == {
        "platform": "whatsapp",
        "$or": [
            {"business_phone_id": "primary"},
            {"business_phone_id": {"$exists": False}},
            {"business_phone_id": None},
        ],
    }
    assert whatsapp_conversation_scope("secondary") == {
        "platform": "whatsapp",
        "business_phone_id": "secondary",
    }
    legacy = [
        {"phone_id": "one", "wa_id": "waba", "slot": 1},
        {"phone_id": "two", "wa_id": "waba", "slot": 1},
        {"phone_id": "three", "wa_id": "waba", "slot": 1},
    ]
    assert [item["slot"] for item in public_connections(legacy)] == [1, 2, 3]
    assert [item["is_primary"] for item in public_connections(legacy)] == [True, False, False]
    historical = public_historical_connections([
        {"phone_id": "one", "is_active": False, "last_slot": 1},
        {"phone_id": "one", "is_active": False, "last_slot": 2},
    ])
    assert len(historical) == 1 and historical[0]["is_active"] is False
    assert choose_connection_slot(
        legacy,
        phone_number_id="four",
        waba_id="waba",
        limit=50,
    )[0] == 4


if __name__ == "__main__":
    test_connection_and_webhook_contracts()
    test_scoped_archive_and_cleanup_contracts()
    test_deployment_runs_the_verified_migration_before_serving()
    test_number_scoped_query_and_flow_binding_contracts()
    test_primary_phone_scope_keeps_legacy_conversations_visible()
    print("Multi-number WhatsApp backend contracts passed.")
