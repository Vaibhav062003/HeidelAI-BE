import ast
from pathlib import Path


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def test_transfer_and_reclaim_return_authoritative_conversation():
    tree = ast.parse(Path("api/transfer_routes.py").read_text())
    for name in ("transfer_conversation", "reclaim_conversation"):
        function = _function(tree, name)
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "find_one_and_update"
            and any(
                keyword.arg == "return_document"
                and isinstance(keyword.value, ast.Attribute)
                and keyword.value.attr == "AFTER"
                for keyword in node.keywords
            )
            for node in ast.walk(function)
        )
        assert any(
            isinstance(node, ast.Dict)
            and any(isinstance(key, ast.Constant) and key.value == "conversation" for key in node.keys)
            for node in ast.walk(function)
        )


if __name__ == "__main__":
    test_transfer_and_reclaim_return_authoritative_conversation()
    print("Transfer contract check passed.")
