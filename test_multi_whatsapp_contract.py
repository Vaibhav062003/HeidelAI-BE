"""Dependency-free contract checks for the two-number WhatsApp boundary."""

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
    assert 'phone_number_id=req.phone_id or req.phone_number_id' in routes
    assert 'requested_phone_id = str(req.phone_number_id or "").strip()' in routes
    assert '(value.get("metadata") or {}).get("phone_number_id")' in webhook
    assert 'connection.get("wa_bot_flow_id")' in webhook
    assert 'business_phone_id=business_phone_id' in webhook
    assert 'len(value) > 24' in connection_service


def test_scoped_archive_and_cleanup_contracts():
    service = source("services/wa_service.py")
    scheduler = source("services/scheduler_service.py")

    assert 'phone_filter = {"platform": "whatsapp", "business_phone_id": phone_id}' in service
    assert 'db[conversations_collection_name].delete_many(' in service
    assert '{"conversation_id": {"$in": conversation_ids}}, session=session' in service
    assert 'archived["note_id"] = str(archived.get("note_id") or archived.get("_id"))' in service
    assert 'delete_one({"phone_id": phone_id, "org_id": org_id})' in service
    assert 'archived_private_notes_' in scheduler
    assert 'drop_collection(archived_notes_col)' in scheduler


def test_number_scoped_query_and_flow_binding_contracts():
    common_routes = source("api/common_routes.py")
    common_service = source("services/common_service.py")
    contacts_service = source("services/contacts_service.py")
    flow_service = source("services/wa_bot_service.py")
    wa_service = source("services/wa_service.py")

    assert common_routes.count("business_phone_id: Optional[str]") >= 4
    assert 'conditions.append({"platform": "whatsapp", "business_phone_id": business_phone_id})' in common_service
    assert 'base_conditions.append({"platform": "whatsapp", "business_phone_id": business_phone_id})' in common_service
    assert '"business_phone_id": doc.get("business_phone_id")' in common_service
    assert '{"platform": "whatsapp", "business_phone_id": business_phone_id}' in common_service
    assert '"wa_bot_flow_id": flow_id' in flow_service
    assert '"phone_ids", "wa_bot_flow_id"' in flow_service
    assert '"conversation_ids": contact_data["conversation_id"]' in contacts_service
    assert '*(contact.get("conversation_ids") or [])' in common_service
    assert 'conversation_phone.get(str(item.get("_id") or ""), "")' in wa_service


if __name__ == "__main__":
    test_connection_and_webhook_contracts()
    test_scoped_archive_and_cleanup_contracts()
    test_number_scoped_query_and_flow_binding_contracts()
    print("Multi-number WhatsApp backend contracts passed.")
