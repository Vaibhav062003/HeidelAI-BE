import ast
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path


class ObjectId:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def is_valid(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{24}", value))

    def __eq__(self, other):
        return isinstance(other, ObjectId) and self.value == other.value

    def __str__(self):
        return self.value


class Result:
    def __init__(self, *, deleted_count=0, modified_count=0, matched_count=0):
        self.deleted_count = deleted_count
        self.modified_count = modified_count
        self.matched_count = matched_count


def field(row, dotted_key):
    value = row
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def matches(row, query):
    for key, value in query.items():
        actual = field(row, key)
        if key == "$and":
            if not all(matches(row, part) for part in value): return False
        elif key == "$or":
            if not any(matches(row, part) for part in value): return False
        elif isinstance(value, dict):
            for operator, expected in value.items():
                if operator == "$exists" and (key in row) != expected: return False
                if operator == "$ne" and actual == expected: return False
                if operator == "$in" and actual not in expected: return False
        elif actual != value:
            return False
    return True


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.offset = 0
        self.size = None

    def sort(self, *_args):
        return self

    def skip(self, amount):
        self.offset = amount
        return self

    def limit(self, amount):
        self.size = amount
        return self

    def __iter__(self):
        rows = self.rows[self.offset:]
        return iter(rows if self.size is None else rows[:self.size])


class Collection:
    def __init__(self, rows):
        self.rows = rows

    def count_documents(self, query):
        return sum(matches(row, query) for row in self.rows)

    def find(self, query, projection=None, **kwargs):
        return Cursor([dict(row) for row in self.rows if matches(row, query)])

    def find_one(self, query, projection=None, **kwargs):
        return next((dict(row) for row in self.rows if matches(row, query)), None)

    def update_one(self, query, update, **kwargs):
        row = next((row for row in self.rows if matches(row, query)), None)
        if not row:
            return Result()
        for key in update.get("$unset", {}):
            row.pop(key, None)
        row.update(update.get("$set", {}))
        return Result(modified_count=1, matched_count=1)

    def delete_one(self, query):
        row = next((row for row in self.rows if matches(row, query)), None)
        if not row:
            return Result()
        self.rows.remove(row)
        return Result(deleted_count=1)


class Database:
    def __init__(self, collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections[name]

    def list_collection_names(self):
        return list(self.collections)


class Logger:
    def error(self, *_args):
        pass

    def info(self, *_args):
        pass


def load_functions(database):
    names = {
        "get_uploaded_contacts_paginated",
        "get_upload_batches",
        "delete_uploaded_contact",
        "delete_upload_batch",
        "delete_directory_contact",
    }
    tree = ast.parse(Path("services/contacts_service.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    namespace = {
        "db": database,
        "ObjectId": ObjectId,
        "datetime": datetime,
        "timezone": timezone,
        "re": re,
        "logger": Logger(),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), "contacts", "exec"), namespace)
    return namespace


def test_uploaded_contact_record_ids_and_deletion():
    upload_id = "64b000000000000000000001"
    live_id = "64b000000000000000000002"
    contacts = Collection([
        {"_id": ObjectId(upload_id), "conversation_id": None, "is_bulk_uploaded": True, "upload_metadata": {"batch_id": "batch-1"}},
        {"_id": ObjectId(live_id), "conversation_id": "whatsapp_live", "whatsapp_id": "waba", "is_bulk_uploaded": True, "upload_metadata": {"batch_id": "batch-1"}},
    ])
    functions = load_functions(Database({"contacts_org": contacts, "contact_upload_batches_org": Collection([])}))

    page = asyncio.run(functions["get_uploaded_contacts_paginated"]("org"))
    assert [row["record_id"] for row in page["contacts"]] == [upload_id, live_id]
    assert all("_id" not in row for row in page["contacts"])

    assert functions["delete_uploaded_contact"]("org", upload_id) == {"deleted": True}
    assert functions["delete_uploaded_contact"]("org", "whatsapp_live") == {"deleted": True}
    assert len(contacts.rows) == 2
    assert all(row.get("deleted_at") for row in contacts.rows)
    assert contacts.rows[1]["conversation_id"] == "whatsapp_live"
    assert asyncio.run(functions["get_uploaded_contacts_paginated"]("org"))["contacts"] == []


def test_uploaded_batch_record_id_and_rollback():
    batch_record_id = "64b000000000000000000003"
    batch_id = "batch-2"
    contacts = Collection([
        {"_id": ObjectId("64b000000000000000000004"), "conversation_id": None, "is_bulk_uploaded": True, "upload_metadata": {"batch_id": batch_id}},
        {"_id": ObjectId("64b000000000000000000005"), "conversation_id": "whatsapp_live", "whatsapp_id": "waba", "is_bulk_uploaded": True, "upload_metadata": {"batch_id": batch_id}},
        {"_id": ObjectId("64b000000000000000000006"), "upload_metadata": {"batch_id": batch_id}},
    ])
    batches = Collection([{"_id": ObjectId(batch_record_id), "batch_id": batch_id, "filename": "contacts.csv"}])
    functions = load_functions(Database({"contacts_org": contacts, "contact_upload_batches_org": batches}))

    listed = asyncio.run(functions["get_upload_batches"]("org"))
    assert listed == [{"record_id": batch_record_id, "batch_id": batch_id, "filename": "contacts.csv"}]

    result = functions["delete_upload_batch"]("org", batch_record_id)
    assert result == {"batch_id": batch_id, "deleted_count": 1, "demoted_count": 1}
    assert len(batches.rows) == 0
    assert len(contacts.rows) == 2
    assert sum(bool(row.get("is_bulk_uploaded")) for row in contacts.rows) == 0

    legacy_batch_id = "batch-legacy"
    batches.rows.append({"_id": ObjectId("64b000000000000000000007"), "batch_id": legacy_batch_id})
    contacts.rows.append({
        "_id": ObjectId("64b000000000000000000008"),
        "is_bulk_uploaded": True,
        "upload_metadata": {"batch_id": legacy_batch_id},
    })
    assert functions["delete_upload_batch"]("org", legacy_batch_id) == {
        "batch_id": legacy_batch_id,
        "deleted_count": 1,
        "demoted_count": 0,
    }
    assert functions["delete_upload_batch"]("org", "missing")["reason"] == "not_found"


if __name__ == "__main__":
    test_uploaded_contact_record_ids_and_deletion()
    test_uploaded_batch_record_id_and_rollback()
