import ast
import re
from pathlib import Path


class ObjectId:
    def __init__(self, value):
        self.value = value

    @staticmethod
    def is_valid(value):
        return isinstance(value, str) and len(value) == 24

    def __str__(self):
        return self.value


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.skipped = None
        self.limited = None
        self.sorted = None

    def sort(self, field, direction=None):
        self.sorted = (field, direction)
        return self

    def skip(self, amount):
        self.skipped = amount
        return self

    def limit(self, amount):
        self.limited = amount
        return self

    def __iter__(self):
        return iter(self.rows[: self.limited])


class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.query = None
        self.projection = None
        self.cursor = None
        self.find_one_calls = 0

    def count_documents(self, query):
        self.query = query
        return 41

    def find_one(self, query, projection, sort):
        self.find_one_calls += 1
        self.query = query
        return {"_id": ObjectId("64b000000000000000000001")}

    def find(self, query, projection):
        self.query = query
        self.projection = projection
        self.cursor = Cursor(self.rows)
        return self.cursor


class Database:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, _name):
        return self.collection


def load_helpers(collection):
    tree = ast.parse(Path("services/contacts_service.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_get_contacts_page"
    ]
    namespace = {"db": Database(collection), "re": re, "ObjectId": ObjectId, "HTTPException": ValueError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])), "contacts", "exec"), namespace)
    return namespace


def test_contact_page_and_bulk_audience():
    collection = Collection([{"conversation_id": "one"}, {"conversation_id": "two"}])
    helpers = load_helpers(collection)

    result = helpers["_get_contacts_page"]("org", 3, 10, "Sam.*", ["lead"], None)
    assert result == {
        "contacts": [{"conversation_id": "one"}, {"conversation_id": "two"}],
        "total": 41,
        "page": 3,
        "limit": 10,
        "snapshot": "64b000000000000000000001",
    }
    assert collection.cursor.skipped == 20
    assert collection.cursor.limited == 10
    assert collection.cursor.sorted == ("_id", -1)
    filtered_query = collection.query["$and"][0]
    assert filtered_query["$and"][0]["$or"][0] == {"categories": {"$in": ["lead"]}}
    assert filtered_query["$and"][1]["$or"][0]["full_name"]["$regex"] == r"Sam\.\*"

    helpers["_get_contacts_page"]("org", 4, 10, "Sam.*", ["lead"], result["snapshot"])
    assert collection.find_one_calls == 1

if __name__ == "__main__":
    test_contact_page_and_bulk_audience()
