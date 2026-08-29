#!/usr/bin/env python3
"""Executable guard for durable WhatsApp broadcast opt-outs."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WA_SERVICE_PATH = ROOT / "services/wa_service.py"
WA_ROUTES = (ROOT / "api/wa_routes.py").read_text()
SHEETS = (ROOT / "services/google_sheet_automation.py").read_text()
WEBHOOK = (ROOT / "api/webhook_routes.py").read_text()
SHOPIFY = (ROOT / "services/shopify_service.py").read_text()


class Collection:
    def __init__(self, suppressed=None, preference_messages=None):
        self.query = None
        self.suppressed = suppressed or []
        self.preference_messages = preference_messages or []
        self.operations = []

    def find(self, query, _projection):
        self.query = query
        return [{"phone": phone} for phone in self.suppressed]

    def find_one(self, query, _projection):
        self.query = query
        return {"_id": 1} if query.get("phone") == "919876543210" else None

    def aggregate(self, _pipeline):
        return self.preference_messages

    def bulk_write(self, operations, ordered=False):
        assert ordered is False
        self.operations.extend(operations)


class Update:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


def main():
    source = WA_SERVICE_PATH.read_text()
    tree = ast.parse(source)
    wanted = {"broadcast_phone_key", "broadcast_message_preference", "get_broadcast_suppressed_phone_keys", "is_broadcast_suppressed"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    suppression_collection = Collection(suppressed=["919876543210"])
    messages_collection = Collection(preference_messages=[{"_id": "whatsapp_919111111111", "content": "STOP"}])
    database = type("DB", (), {
        "whatsapp_suppressions": suppression_collection,
        "__getitem__": lambda self, _name: messages_collection,
    })()
    namespace = {"db": database, "datetime": __import__("datetime").datetime, "timezone": __import__("datetime").timezone, "UpdateOne": Update}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(WA_SERVICE_PATH), "exec"), namespace)

    assert namespace["broadcast_phone_key"]("+91 98765-43210") == "919876543210"
    assert namespace["broadcast_message_preference"](" STOP! ") == "opted_out"
    assert namespace["broadcast_message_preference"]("START") == "subscribed"
    assert namespace["get_broadcast_suppressed_phone_keys"]("org-a", ["+91 98765-43210", "+91 91111-11111"]) == {"919876543210", "919111111111"}
    assert suppression_collection.query["org_id"] == "org-a"
    assert set(suppression_collection.query["phone"]["$in"]) == {"919876543210", "919111111111"}
    assert len(suppression_collection.operations) == 1
    assert namespace["is_broadcast_suppressed"]("org-a", "+91 98765-43210")

    assert "get_broadcast_suppressed_phone_keys" in WA_ROUTES
    assert "get_broadcast_suppressed_phone_keys" in source
    assert "is_broadcast_suppressed" in SHEETS
    assert "await asyncio.to_thread(record_whatsapp_preference, org_id, customer_phone_no, content)" in WEBHOOK
    assert '"marketing_opted_out": True' in SHOPIFY
    assert '"status": "skipped"' in source
    print("BROADCAST_SUPPRESSION_CONTRACT_OK")


if __name__ == "__main__":
    main()
