import ast
from pathlib import Path

from services.quote_math import quote_line_amounts


def test_quote_routes_are_authenticated_and_store_private_pricing():
    tree = ast.parse(Path("api/quote_routes.py").read_text())
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
    }
    assert "private_unit_price" in string_values
    assert "public_grand_total" in string_values
    field_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert {"gst_inclusive", "revision_number"} <= string_values
    assert {"category", "component_categories", "design"} <= field_names

    settings_update = next(function for function in route_functions if function.name == "update_quote_settings")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "admin_required"
        for node in ast.walk(settings_update)
    )

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


if __name__ == "__main__":
    test_quote_routes_are_authenticated_and_store_private_pricing()
    test_quote_math_handles_inclusive_and_extra_gst()
