"""Dependency-free contract check for multi-number WhatsApp outbound routing."""

import ast
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).parent


def _source(relative_path: str) -> str:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    ast.parse(source, filename=relative_path)
    return source


def test_outbound_paths_use_bound_sender_and_customer_fields() -> None:
    scheduler = _source("services/automation_scheduler.py")
    google_sheet = _source("services/google_sheet_automation.py")
    oos = _source("services/oos_service.py")
    wa_service = _source("services/wa_service.py")
    wa_routes = _source("api/wa_routes.py")

    assert 'recipient_id=conversation_id.split("_")[-1]' not in scheduler
    assert 'recipient_id=recipient_id' in scheduler
    assert 'conversation_id=conversation_id' in scheduler

    assert "conversation_id = await create_or_update_whatsapp_conversation(" in google_sheet
    assert "business_phone_id=phone_id" in google_sheet
    assert "resolve_outbound_whatsapp_connection(" in google_sheet

    assert 'db.whatsapp_connections.find_one, {"org_id": org_id}' not in oos
    assert "dealer_conversation_id = await create_or_update_whatsapp_conversation(" in oos
    assert '"conversation_id": dealer_conversation_id' in oos

    reaction_start = wa_service.index("async def send_whatsapp_reaction")
    reaction_end = wa_service.index("async def send_message_reaction", reaction_start)
    reaction_source = wa_service[reaction_start:reaction_end]
    assert "resolve_outbound_whatsapp_connection(" in reaction_source
    assert 'conversation.get("customer_id")' in reaction_source
    assert "whatsapp_connections.find_one" not in reaction_source

    bulk_start = wa_routes.index("async def bulk_send_whatsapp_message")
    bulk_source = wa_routes[bulk_start:]
    assert "await asyncio.to_thread(resolve_outbound_whatsapp_connection," in bulk_source
    assert "request.recipients" not in bulk_source
    assert "whatsapp_conversation_id(phone, phone_id)" in wa_service
    create_templates_source = wa_service[wa_service.index("async def create_templates"):wa_service.index("def build_list_message_payload")]
    assert "job_id" not in create_templates_source
    run_bulk_source = wa_service[wa_service.index("async def run_bulk_send_job"):]
    assert '"phone_id": phone_id' in run_bulk_source
    assert "business_phone = sender_connection.get(\"phone\")" in run_bulk_source
    assert '"slot": 1' in wa_service
    assert 'whatsapp_conversation_id(\n            recipient_id,\n            connection.get("phone_id"),' in wa_service
    assert wa_service.count('"business_phone_id": connection.get("phone_id")') >= 2
    assert wa_service.count("upsert=True") >= 3


def test_template_routes_use_the_selected_whatsapp_connection() -> None:
    routes = _source("api/wa_routes.py")
    common = _source("api/common_routes.py")
    models = _source("schemas/models.py")

    fetch = routes[routes.index("async def fetch_templates"):routes.index('@router.post("/templates")')]
    create = routes[routes.index("async def whatsapp_create_template"):routes.index('@router.delete("/templates/{template_name}")')]
    upload = common[common.index("async def upload_media"):common.index('@router.post("/api/whatsapp/upload-media")')]
    template_model = models[models.index("class Template(BaseModel)"):models.index("# Interactive WhatsApp Message Models")]

    assert "phone_id: Optional[str] = Query(None, max_length=128)" in fetch
    assert "template.phone_id" in create
    assert "phone_id: Optional[str] = Form(None, max_length=128)" in upload
    assert "phone_id: Optional[str] = Field(default=None, max_length=128)" in template_model
    for source in (fetch, create, upload):
        assert "resolve_outbound_whatsapp_connection" in source
        assert "org_document" not in source
    assert 'connection.get("wa_id") or connection.get("waba_id")' in fetch
    assert 'connection.get("wa_id") or connection.get("waba_id")' in create


class _Cursor(list):
    def limit(self, count: int):
        return _Cursor(self[:count])


class _Connections:
    def __init__(self, documents: list[dict]):
        self.documents = documents

    def find(self, query: dict):
        def matches(document: dict) -> bool:
            for key, value in query.items():
                if isinstance(value, dict) and "$ne" in value:
                    if document.get(key) == value["$ne"]:
                        return False
                elif document.get(key) != value:
                    return False
            return True

        return _Cursor(document for document in self.documents if matches(document))


class _Db:
    def __init__(self, documents: list[dict]):
        self.whatsapp_connections = _Connections(documents)


def test_outbound_resolver_prefers_exact_then_primary_then_sole_legacy() -> None:
    source = _source("services/wa_service.py")
    module = ast.parse(source)
    resolver = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolve_outbound_whatsapp_connection"
    )
    compiled = compile(ast.Module(body=[resolver], type_ignores=[]), "resolver", "exec")
    namespace = {"Optional": Optional}
    exec(compiled, namespace)
    resolve = namespace["resolve_outbound_whatsapp_connection"]

    primary = {"org_id": "org", "wa_id": "waba", "phone_id": "one", "slot": 1}
    secondary = {"org_id": "org", "wa_id": "waba", "phone_id": "two", "slot": 2}
    namespace["db"] = _Db([primary, secondary])
    assert resolve("org", "waba", "two") == secondary
    assert resolve("org", "waba") == primary

    other_waba = {"org_id": "org", "wa_id": "other-waba", "phone_id": "three", "slot": 2}
    inactive = {"org_id": "org", "wa_id": "other-waba", "phone_id": "inactive", "is_active": False}
    namespace["db"] = _Db([primary, other_waba, inactive])
    assert resolve("org", None, "three") == other_waba
    assert resolve("org", None, "missing") is None
    assert resolve("org", None, "inactive") is None
    assert resolve("another-org", None, "three") is None

    legacy = {"org_id": "org", "wa_id": "waba", "phone_id": "legacy"}
    namespace["db"] = _Db([legacy])
    assert resolve("org", "waba") == legacy

    namespace["db"] = _Db([legacy, {**legacy, "phone_id": "other"}])
    assert resolve("org", "waba") is None


if __name__ == "__main__":
    test_outbound_paths_use_bound_sender_and_customer_fields()
    test_template_routes_use_the_selected_whatsapp_connection()
    test_outbound_resolver_prefers_exact_then_primary_then_sole_legacy()
    print("WhatsApp outbound binding contract passed.")
