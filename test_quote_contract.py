import ast
from pathlib import Path

from pydantic import BaseModel, Field

from services import quote_service
from services.quote_math import quote_line_amounts
from services.quote_service import DEFAULT_QUOTE_FOOTER, build_quote_document


def test_quote_routes_are_authenticated_and_store_private_pricing():
    tree = ast.parse(Path("api/quote_routes.py").read_text())
    service_tree = ast.parse(Path("services/quote_service.py").read_text())
    route_functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]
    assert route_functions
    for function in route_functions:
        annotations = {argument.arg: argument.annotation for argument in function.args.args}
        assert isinstance(annotations.get("user"), ast.Name)
        assert annotations["user"].id == "CurrentUser"

    string_values = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } | {
        node.value for node in ast.walk(service_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "private_unit_price" in string_values
    assert "public_grand_total" in string_values
    field_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert {"gst_inclusive", "revision_number"} <= string_values
    assert "build_quote_document" in {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    assert {"category", "component_categories", "design"} <= field_names

    settings_update = next(function for function in route_functions if function.name == "update_quote_settings")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "admin_required"
        for node in ast.walk(settings_update)
    )
    settings_strings = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"logo_url", "seller", "payment_details", "org_name"} <= settings_strings
    assert {"pdf_design", "branding_defaults", "sections", "columns", "can_edit_footer"} <= settings_strings
    assert "labels" in field_names

    update_quote = next(function for function in route_functions if function.name == "update_quote")
    update_strings = {
        node.value for node in ast.walk(update_quote)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"$push", "revisions", "revision_number", "revision_count"} <= update_strings
    assert any(function.name == "get_quote_revisions" for function in route_functions)

    auth_tree = ast.parse(Path("auth/auth_utils.py").read_text())
    admin_roles = {
        node.value for node in ast.walk(auth_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"admin", "org:admin"} <= admin_roles


def test_quote_math_handles_inclusive_and_extra_gst():
    assert quote_line_amounts(1, 118, 18, True) == (100, 18, 118)
    assert quote_line_amounts(2, 100, 18, False) == (200, 36, 236)


def test_shared_quote_document_keeps_inclusive_gst_total_exact():
    document = build_quote_document({
        "items": [{
            "id": "one", "description": "Product", "quantity": 1,
            "unit_price": 118, "gst_rate": 18, "gst_inclusive": True,
        }],
        "shipping": 0, "discount": 0,
    }, org_id="org", actor_id="ai-sales", actor_name="AI Sales Agent")
    assert document["private_subtotal"] == 100
    assert document["private_gst_total"] == 18
    assert document["public_grand_total"] == 118
    assert document["items"][0]["private_unit_price"] == 118


def test_quote_footer_is_editable_only_for_allowlisted_orgs():
    original = quote_service.FOOTER_EDIT_ORGS
    quote_service.FOOTER_EDIT_ORGS = {"org_allowed"}
    payload = {
        "items": [{"id": "one", "description": "Product", "quantity": 1, "unit_price": 100}],
        "branding": {"footer_text": "Custom footer"},
    }
    try:
        assert build_quote_document(payload, org_id="org_allowed", actor_id="user", actor_name="User")["branding"]["footer_text"] == "Custom footer"
        assert build_quote_document(payload, org_id="org_blocked", actor_id="user", actor_name="User")["branding"]["footer_text"] == DEFAULT_QUOTE_FOOTER
        payload["branding"]["footer_text"] = ""
        assert build_quote_document(payload, org_id="org_allowed", actor_id="user", actor_name="User")["branding"]["footer_text"] == DEFAULT_QUOTE_FOOTER
    finally:
        quote_service.FOOTER_EDIT_ORGS = original


def test_quote_item_accepts_full_laptop_model():
    tree = ast.parse(Path("api/quote_routes.py").read_text())
    quote_item = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QuoteItem")
    namespace = {"__name__": "quote_item_contract", "BaseModel": BaseModel, "Field": Field}
    exec(compile(ast.Module(body=[quote_item], type_ignores=[]), "api/quote_routes.py", "exec"), namespace)
    description = ("Inspiron 7440 Inspiron 7440 2-in-1 OIC74401512F1RINU1O Core™️ 5 120U Win 11 + Office H&S 2024 + M365  16GB DDR5 512GB SSD Intel®️ Graphics 14.0-inch 16:10 FHD +Touch 300nits WVA Display with ComfortView Backlit Keyboard Ice Blue Dell Essential/Ecoloop Essential 1 Year Onsite Hardware Service " + "Additional configuration detail. " * 20)[:750]
    assert len(description) == 750
    assert namespace["QuoteItem"](id="1", description=description, unit_price=92000).description == description


if __name__ == "__main__":
    test_quote_routes_are_authenticated_and_store_private_pricing()
    test_quote_math_handles_inclusive_and_extra_gst()
    test_shared_quote_document_keeps_inclusive_gst_total_exact()
    test_quote_footer_is_editable_only_for_allowlisted_orgs()
    test_quote_item_accepts_full_laptop_model()
    print("Quote contract checks passed.")
