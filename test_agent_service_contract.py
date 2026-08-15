import ast
from pathlib import Path


def main() -> None:
    tree = ast.parse(Path("services/agent_service.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "send_to_agent")
    assert any(isinstance(node, ast.Raise) for node in ast.walk(function)), "missing secret must fail closed"
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "post"
        and any(keyword.arg == "headers" for keyword in node.keywords)
        for node in ast.walk(function)
    ), "chatbot request must include authentication headers"


if __name__ == "__main__":
    main()
