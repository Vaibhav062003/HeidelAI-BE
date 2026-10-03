"""Run with python tests/contracts/test_vcf_inbox_sync_contract.py; no real database."""
import ast
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from io import StringIO
import json
import os
from pathlib import Path
import re
import runpy
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import uuid


class BulkWriteError(Exception):
    def __init__(self, details):
        self.details = details


class UpdateOne:
    def __init__(self, query, update, upsert=False):
        self.query, self.update, self.upsert = query, update, upsert


def field(row, key):
    for part in key.split("."):
        if not isinstance(row, dict) or part not in row:
            return None, False
        row = row[part]
    return row, True


def matches(row, query):
    for key, expected in query.items():
        value, exists = field(row, key)
        if isinstance(expected, dict):
            if "$exists" in expected and exists != expected["$exists"]:
                return False
            if "$in" in expected and value not in expected["$in"]:
                return False
        elif value != expected:
            return False
    return True


class Collection:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))
        self.fail = False
        self.fail_phone = None

    def find(self, query, projection=None, **_kwargs):
        rows = [deepcopy(row) for row in self.rows if matches(row, query)]
        if projection and projection.get("_id") == 0:
            for row in rows:
                row.pop("_id", None)
        return rows

    def find_one(self, query, projection=None, **kwargs):
        return next(iter(self.find(query, projection, **kwargs)), None)

    def update_one(self, query, update, upsert=False, **_kwargs):
        row = next((row for row in self.rows if matches(row, query)), None)
        inserted = row is None and upsert
        if inserted:
            row = {"_id": f"new-{len(self.rows)}", **query, **update.get("$setOnInsert", {})}
            self.rows.append(row)
        if row is None:
            return SimpleNamespace(matched_count=0, inserted=False)
        row.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            row.pop(key, None)
        for key, value in update.get("$addToSet", {}).items():
            if value not in row.setdefault(key, []):
                row[key].append(value)
        return SimpleNamespace(matched_count=0 if inserted else 1, inserted=inserted)

    def bulk_write(self, operations, **kwargs):
        if self.fail:
            raise RuntimeError("simulated conversation database failure")
        inserted = matched = 0
        errors = []
        for index, operation in enumerate(operations):
            if self.fail_phone and operation.query.get("phone_number") == self.fail_phone:
                errors.append({"index": index, "errmsg": "simulated contact write failure"})
                continue
            result = self.update_one(operation.query, operation.update, upsert=operation.upsert, **kwargs)
            inserted += result.inserted
            matched += result.matched_count
        if errors:
            raise BulkWriteError({"nUpserted": inserted, "nMatched": matched, "writeErrors": errors})
        return SimpleNamespace(upserted_count=inserted, matched_count=matched)

    def insert_one(self, row):
        self.rows.append(deepcopy(row))


class Database(dict):
    def __missing__(self, key):
        self[key] = Collection()
        return self[key]

    @property
    def whatsapp_connections(self):
        return self["whatsapp_connections"]


class Session:
    def __init__(self, database):
        self.database = database
        self.before_transaction = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def with_transaction(self, callback):
        if self.before_transaction:
            self.before_transaction()
            self.before_transaction = None
        snapshot = {name: deepcopy(collection.rows) for name, collection in self.database.items()}
        try:
            return callback(self)
        except Exception:
            for name, rows in snapshot.items():
                self.database[name].rows = rows
            raise


def load_functions(path, names, namespace):
    functions = [node for node in ast.parse(Path(path).read_text()).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in functions} == set(names)
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), path, "exec"), namespace)


def setup(ids=(), contacts=True):
    database = Database()
    phone = "919876543210"
    database["contacts_org"] = Collection([{"_id": "contact-1", "phone_number": phone, "full_name": "Old profile", "conversation_id": ids[0], "whatsapp_id": "waba"}] if contacts and ids else [])
    database["conversations_org"] = Collection([
        {"_id": str(index), "conversation_id": value, "customer_id": phone, "customer_name": "Old profile", "platform": "whatsapp", "last_message_timestamp": datetime(2026, 9, 20 + index, tzinfo=timezone.utc), "last_message": "Keep history", "unread_count": 7, "assigned_agent_id": "agent", "message_counter": 10}
        for index, value in enumerate(ids)
    ])
    database["whatsapp_connections"] = Collection([{"org_id": "org", "phone": "919999999999", "is_active": True}])
    session = Session(database)
    namespace = {"db": database, "get_mongo_client": lambda: SimpleNamespace(start_session=lambda: session), "asyncio": asyncio, "datetime": datetime, "timezone": timezone, "BytesIO": BytesIO, "re": re, "uuid": uuid, "UpdateOne": UpdateOne, "BulkWriteError": BulkWriteError, "ObjectId": type("ObjectId", (), {}), "logger": SimpleNamespace(error=lambda *_: None, success=lambda *_: None), "EMAIL_PATTERN": re.compile(r"^[^@]+@[^@]+\.[^@]+$")}
    load_functions("services/utils.py", ["normalize_phone", "name_search_prefixes", "serialize_mongo"], namespace)
    load_functions("services/contacts_service.py", ["_validate_email", "get_org_default_country_code", "_vcf_contact_sync_plan", "sync_vcf_contacts_for_org", "broadcast_vcf_contact_sync", "process_bulk_wa_contact_upload", "store_contact"], namespace)
    parser = ModuleType("services.vcard_parser")
    exec(compile(Path("services/vcard_parser.py").read_text(), "vcf-parser", "exec"), parser.__dict__)
    services = ModuleType("services")
    services.__path__ = []
    sys.modules.update({"services": services, "services.vcard_parser": parser})
    return database, namespace, session


def upload(namespace, name="Saved VCF Customer", phone="+91 98765 43210", extra=""):
    payload = f"BEGIN:VCARD\nVERSION:3.0\nFN:{name}\nTEL:{phone}\nEND:VCARD\n{extra}".encode()
    return namespace["process_bulk_wa_contact_upload"]("org", payload, "contacts.vcf")


def test_import_and_links():
    phone = "919876543210"
    for ids in [[f"whatsapp_{phone}"], [f"whatsapp_line1_{phone}"], [f"whatsapp_line1_{phone}", f"whatsapp_line2_{phone}"], [f"whatsapp_{phone}", f"whatsapp_line2_{phone}"]]:
        for has_contact in [True, False]:
            db, ns, _ = setup(ids, has_contact)
            before = deepcopy(db["conversations_org"].rows)
            db["conversations_other_org"] = Collection(before)
            db["conversations_org"].rows.append({"_id": "instagram", "platform": "instagram", "customer_id": phone, "conversation_id": "instagram_1", "customer_name": "Instagram"})
            stats = upload(ns)
            assert stats["successfully_processed"] == 1 and stats["inbox_sync"] == {"status": "synced", "failed_contacts": 0}
            contact = db["contacts_org"].rows[0]
            assert contact["full_name"] == "Saved VCF Customer" and contact["name_locked_by_vcf"] is True
            assert contact["conversation_id"] == (ids[0] if has_contact or f"whatsapp_{phone}" in ids else ids[-1])
            assert contact["conversation_ids"] == sorted(ids)
            for old, new in zip(before, db["conversations_org"].rows):
                assert new["customer_name"] == contact["full_name"]
                assert "customer" in new["customer_name_prefixes"]
                assert {key: new[key] for key in old if key != "customer_name"} == {key: value for key, value in old.items() if key != "customer_name"}
            assert db["conversations_other_org"].rows == before
            assert db["conversations_org"].rows[-1]["customer_name"] == "Instagram"
            assert upload(ns)["_updated_conversation_ids"] == []
            assert len(db["contacts_org"].rows) == 1
            ns["store_contact"]("org", "whatsapp", {"phone_number": phone, "full_name": "New Meta Profile", "conversation_id": ids[0]})
            assert db["contacts_org"].rows[0]["full_name"] == "Saved VCF Customer"

    db, ns, _ = setup()
    stats = upload(ns)
    assert stats["inbox_sync"]["status"] == "synced"
    assert db["contacts_org"].rows[0]["conversation_id"] == f"whatsapp_{phone}"
    assert db["conversations_org"].rows == []
    extra = f"BEGIN:VCARD\nFN:Duplicate\nTEL:{phone}\nEND:VCARD\nBEGIN:VCARD\nFN:Invalid\nTEL:123\nEND:VCARD\n"
    stats = upload(ns, extra=extra)
    assert stats["successfully_processed"] == 1 and stats["skipped_invalid_rows"] == 2


def test_failures_and_repair():
    ids = ["whatsapp_line1_919876543210", "whatsapp_line2_919876543210"]
    db, ns, session = setup(ids, False)
    db["conversations_org"].fail = True
    stats = upload(ns)
    assert stats["successfully_processed"] == 1
    assert stats["inbox_sync"] == {"status": "partial", "failed_contacts": 1}
    assert stats["_updated_conversation_ids"] == []
    assert all(row["customer_name"] == "Old profile" for row in db["conversations_org"].rows)
    assert db["contacts_org"].rows[0]["conversation_id"] == "whatsapp_919876543210", "link changes must roll back with name failure"
    db["conversations_org"].fail = False
    contact = db["contacts_org"].rows[0]
    contact["full_name"] = "User Edited Name"
    snapshot = deepcopy({name: value.rows for name, value in db.items()})
    report = ns["sync_vcf_contacts_for_org"]("org")
    assert report["changes"] and not report["errors"] and not report["updated_conversation_ids"]
    assert {name: value.rows for name, value in db.items()} == snapshot
    report = ns["sync_vcf_contacts_for_org"]("org", apply=True)
    assert report["updated_conversation_ids"] == sorted(ids)
    assert all(row["customer_name"] == "User Edited Name" for row in db["conversations_org"].rows)
    assert ns["sync_vcf_contacts_for_org"]("org", apply=True)["changes"] == []
    contact["deleted_at"] = datetime.now(timezone.utc)
    assert ns["sync_vcf_contacts_for_org"]("org")["changes"] == []
    contact.pop("deleted_at")
    session.before_transaction = lambda: contact.update(full_name="Concurrent Manual Edit")
    report = ns["sync_vcf_contacts_for_org"]("org", apply=True)
    assert report["errors"] and not report["updated_conversation_ids"]
    assert all(row["customer_name"] == "User Edited Name" for row in db["conversations_org"].rows)
    db["contacts_org"].rows.append({"_id": "duplicate", "phone_number": contact["phone_number"], "full_name": "Conflict", "name_locked_by_vcf": True})
    assert len(ns["sync_vcf_contacts_for_org"]("org")["errors"]) == 2

    db, ns, _ = setup(ids)
    failing_phone = "919876543211"
    db["contacts_org"].fail_phone = failing_phone
    db["conversations_org"].rows.append({"conversation_id": f"whatsapp_{failing_phone}", "platform": "whatsapp", "customer_id": failing_phone, "customer_name": "Keep failed contact name"})
    stats = upload(ns, extra=f"BEGIN:VCARD\nFN:Failed Import\nTEL:{failing_phone}\nEND:VCARD\n")
    assert stats["successfully_processed"] == 1 and stats["skipped_invalid_rows"] == 1
    assert db["conversations_org"].rows[-1]["customer_name"] == "Keep failed contact name"
    assert f"whatsapp_{failing_phone}" not in stats["_updated_conversation_ids"]


def test_events_and_authorization():
    db, ns, _ = setup(["whatsapp_line_919876543210"])
    stats = upload(ns)
    events = []
    async def broadcast(org, event_type, payload):
        events.append((org, event_type, payload))
    ns["broadcast_org_ws"] = broadcast
    asyncio.run(ns["broadcast_vcf_contact_sync"]("org", stats["_updated_conversation_ids"]))
    org, event, payload = events[0]
    assert org == "org" and event == "conversation_updated" and payload["reason"] == "contact_name_updated"
    assert payload["conversation_id"] == payload["conversation"]["conversation_id"]
    assert payload["conversation"]["customer_name"] == "Saved VCF Customer" and "_id" not in payload["conversation"]
    assert isinstance(payload["conversation"]["last_message_timestamp"], str)
    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail
    ns.update(HTTPException=HTTPException, CurrentUser=SimpleNamespace, Request=object, Query=lambda *a, **k: None)
    load_functions("auth/auth_utils.py", ["admin_required"], ns)
    load_functions("api/common_routes.py", ["repair_vcf_inbox_contacts"], ns)
    for role, org_id in [("org:member", "org"), ("org:admin", "other")]:
        try:
            asyncio.run(ns["repair_vcf_inbox_contacts"](None, SimpleNamespace(role=role, org_id=org_id), org_id="org"))
        except HTTPException as error:
            assert error.status_code == 403
        else:
            raise AssertionError("unauthorized repair was allowed")
    report = asyncio.run(ns["repair_vcf_inbox_contacts"](None, SimpleNamespace(role="org:admin", org_id="org"), org_id="org"))
    assert report["apply"] is False and "updated_conversation_ids" not in report
    assert len(events) == 1

    delivered = []
    async def send_json(value):
        delivered.append(value)
    async def eligible(clients, conversation_id):
        assert clients == {"allowed", "denied"}
        assert conversation_id == payload["conversation_id"]
        return {"allowed"}
    ns.update(services=SimpleNamespace(org_clients={"org": {"allowed", "denied"}}, active_clients={"allowed": SimpleNamespace(send_json=send_json)}), _eligible_conversation_clients=eligible, WebSocketDisconnect=type("WebSocketDisconnect", (Exception,), {}))
    load_functions("services/websocket_service.py", ["broadcast_org_ws"], ns)
    asyncio.run(ns["broadcast_vcf_contact_sync"]("org", stats["_updated_conversation_ids"]))
    assert len(delivered) == 1 and delivered[0]["data"]["conversation_id"] == payload["conversation_id"]

    # Execute the real WhatsApp name-resolution block with a later Meta profile name.
    tree = ast.parse(Path("services/wa_service.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and any(isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "final_customer_name" for target in item.targets) for item in node.body))
    start = next(index for index, node in enumerate(function.body) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "final_customer_name" for target in node.targets))
    end = next(index for index, node in enumerate(function.body[start:], start) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "update_set" for target in node.targets))
    wrapper = ast.parse("async def resolve_name():\n    pass\n").body[0]
    wrapper.body = [*function.body[start:end], ast.Return(value=ast.Name(id="final_customer_name", ctx=ast.Load()))]
    ns.update(org_id="org", customer_phone_no="919876543210", customer_name="Later Meta Profile", existing_conversation=None, last_sender="customer", record_activity=True)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), "wa-name-resolution", "exec"), ns)
    assert asyncio.run(ns["resolve_name"]()) == "Saved VCF Customer"


def test_repair_cli():
    module = runpy.run_path("scripts/repair_vcf_inbox_contacts.py", run_name="repair_cli_test")
    assert module["NoRedirect"]().redirect_request(None, None, None, None, None, None) is None
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 60 and request.get_header("Authorization") == "Bearer test-token"
        return BytesIO(json.dumps({"apply": "apply=true" in request.full_url, "changes": [], "errors": []}).encode())
    for flags in [[], ["--apply"]]:
        with patch.dict(os.environ, {"HEIDELAI_API_TOKEN": "test-token"}), patch.object(sys, "argv", ["repair", "--api-url", "https://example.invalid", "--org-id", "org", *flags]), patch.dict(module["main"].__globals__, {"build_opener": lambda *_: SimpleNamespace(open=open_request)}), redirect_stdout(StringIO()) as output:
            module["main"]()
            assert json.loads(output.getvalue())["apply"] == bool(flags)
        assert requests[-1].method == "POST" and "org_id=org" in requests[-1].full_url


if __name__ == "__main__":
    test_import_and_links()
    test_failures_and_repair()
    test_events_and_authorization()
    test_repair_cli()
    print("VCF import, repair, failure, event, and organization-access checks passed.")
