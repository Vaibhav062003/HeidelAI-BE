import ast
from pathlib import Path


source_path = Path(__file__).parent / "main.py"
tree = ast.parse(source_path.read_text())
function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "get_allowed_origins"
)
namespace = {}
exec(compile(ast.Module([function], type_ignores=[]), source_path, "exec"), namespace)
get_allowed_origins = namespace["get_allowed_origins"]

assert "https://localhost" not in get_allowed_origins("development", "")
assert "https://localhost" in get_allowed_origins(" development ", " TRUE ")
assert "https://localhost" not in get_allowed_origins("prod", "true")
assert "https://localhost" not in get_allowed_origins("production", "true")
assert "https://localhost" not in get_allowed_origins("PROD", "true")
assert "https://localhost" not in get_allowed_origins("typo", "true")

cors_assignment = next(
    node for node in tree.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "alowed_origins" for target in node.targets)
)
assert "ALLOW_CAPACITOR_LOCALHOST_ORIGIN" in {
    item.value for item in ast.walk(cors_assignment) if isinstance(item, ast.Constant)
}
