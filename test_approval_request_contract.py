import ast
from pathlib import Path
from typing import List, Optional


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


source = Path("api/transfer_routes.py").read_text()
tree = ast.parse(source)
helpers = [_function(tree, name) for name in (
    "membership_public_data", "approval_recipient_ids", "approval_contact_details"
)]
namespace = {"List": List, "Optional": Optional}
exec(compile(ast.fix_missing_locations(ast.Module(body=helpers, type_ignores=[])), "<test>", "exec"), namespace)

memberships = [
    {"role": "org:admin", "public_user_data": {"user_id": "admin-1"}},
    {"role": "org:manager", "public_user_data": {"user_id": "manager-1"}},
    {"role": "org:member", "public_user_data": {"user_id": "member-1"}},
    {"role": "org:admin", "public_user_data": {"user_id": "admin-2", "banned": True}},
    {"role": "org:admin", "public_user_data": {"user_id": "requester"}},
]
assert namespace["approval_recipient_ids"](memberships, "requester") == ["admin-1", "manager-1"]

details = namespace["approval_contact_details"](
    {"conversation_id": "whatsapp_919876543210", "customer_name": "Fallback"},
    {"full_name": "Lee Computers", "phone_number": "919876543210", "tally": {"gstin": "27abcde1234f1z5"}},
)
assert details == {
    "customer_name": "Lee Computers",
    "customer_phone": "919876543210",
    "customer_gstin": "27ABCDE1234F1Z5",
}
assert namespace["approval_contact_details"]({"conversation_id": "whatsapp_911234567890"})["customer_phone"] == "911234567890"
assert namespace["approval_contact_details"](
    {"conversation_id": "whatsapp_911234567890"},
    {"company_name": "Tally Party", "phone": "911111111111", "gstin": "29ABCDE1234F1Z1"},
)["customer_gstin"] == "29ABCDE1234F1Z1"

endpoint = _function(tree, "request_conversation_approval")
constants = {
    node.value for node in ast.walk(endpoint)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}
assert {
    "approval_request", "recipient_id", "referenced_message_ids", "approval_requested",
    "No other admin or manager is available to approve this request",
} <= constants
contact_helper = _function(tree, "approval_contact_details")
contact_constants = {
    node.value for node in ast.walk(contact_helper)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}
assert {"customer_name", "customer_phone", "customer_gstin"} <= contact_constants
assert any(
    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    and node.func.id == "require_referenced_messages"
    for node in ast.walk(endpoint)
)
assert "tally_customer_links" in source
assert "contact_details.update({key: value for key, value in (contact or {}).items() if value})" in source
assert '"org_id": user.org_id' in source
assert 'now + timedelta(days=30)' in source

print("Approval request backend contract passed.")
