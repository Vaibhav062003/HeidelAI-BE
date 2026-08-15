import ast
from pathlib import Path


def _search_helpers():
    tree = ast.parse(Path("services/utils.py").read_text())
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"phone_search_parts", "name_search_prefixes"}
    ]
    namespace = {}
    module = ast.Module(body=[ast.Import(names=[ast.alias("re")]), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "phone-search", "exec"), namespace)
    return namespace["phone_search_parts"], namespace["name_search_prefixes"]


def test_phone_search_parts_are_country_agnostic():
    phone_search_parts, name_search_prefixes = _search_helpers()
    digits, suffixes = phone_search_parts("+1 (415) 555-0100")
    assert digits == "14155550100"
    assert "4155550100" in suffixes
    assert not digits.startswith("91")

    digits, suffixes = phone_search_parts("+44 20 7946 0958")
    assert digits == "442079460958"
    assert "2079460958" in suffixes

    digits, suffixes = phone_search_parts("+65 6123 4567")
    assert digits == "6561234567"
    assert suffixes[-1] == digits
    prefixes = name_search_prefixes("Samar Tanwar")
    assert {"sam", "samar", "tan", "tanwar"} <= set(prefixes)
    assert "hayaa" in name_search_prefixes("k_i_n_g_hayaa___")

    service_tree = ast.parse(Path("services/common_service.py").read_text())
    context = next(node for node in service_tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch_message_context")
    assert "raw_data" not in {node.value for node in ast.walk(context) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    lookup = next(node for node in service_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_lookup_scope")
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[lookup], type_ignores=[])), "lookup-scope", "exec"), namespace)
    assert namespace["_lookup_scope"]({"$and": [{"platform": "whatsapp"}]}) == {
        "$and": [{"conversation.platform": "whatsapp"}]
    }

    wa_source = Path("services/wa_service.py").read_text()
    assert '"phone_digits": phone_digits' in wa_source
    assert '"phone_search_suffixes": phone_search_suffixes' in wa_source
    assert '"customer_name_prefixes"' in wa_source
    assert '"customer_name_prefixes"' in Path("services/contacts_service.py").read_text()


if __name__ == "__main__":
    test_phone_search_parts_are_country_agnostic()
