import ast
from pathlib import Path


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

    settings_update = next(function for function in route_functions if function.name == "update_quote_settings")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "admin_required"
        for node in ast.walk(settings_update)
    )

    auth_tree = ast.parse(Path("auth/auth_utils.py").read_text())
    admin_roles = {
        node.value for node in ast.walk(auth_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"admin", "org:admin"} <= admin_roles
