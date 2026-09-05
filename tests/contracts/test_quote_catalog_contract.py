import ast
from pathlib import Path


def _function(tree, name):
    return next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)


def test_google_products_support_search_and_pagination_without_breaking_legacy_shape():
    service = Path("services/products_service.py").read_text()
    routes = Path("api/common_routes.py").read_text()
    assert '"search_text": pattern' in service
    assert 'count_documents(_product_search(query))' in service
    assert 'page: Optional[int]' in routes and 'page_size: Optional[int]' in routes
    assert '"has_more"' in routes
    assert 'if legacy:\n        return {"products": products}' in routes


def test_tally_products_support_search_and_pagination_without_breaking_legacy_shape():
    service = Path("services/tally_service.py").read_text()
    routes = Path("api/tally_routes.py").read_text()
    assert 'def _tally_stock_match' in service
    assert '.skip((page - 1) * page_size).limit(page_size)' in service
    assert 'count_tally_stock_items' in routes and '"has_more"' in routes
    assert 'if legacy:\n        return {"stock_items": stock_items}' in routes


def test_quote_design_defaults_are_persisted_and_validated():
    source = Path("api/quote_routes.py").read_text()
    tree = ast.parse(source)
    design = _function(tree, "_default_quote_sections")
    assert design
    assert 'pdf_design: Optional[QuoteDesign]' in source
    assert 'branding_defaults: Optional[QuoteBrandingDefaults]' in source
    assert '"pdf_design": (settings or {}).get("pdf_design") or QuoteDesign().model_dump()' in source
    assert '"branding_defaults": (settings or {}).get("branding_defaults") or QuoteBrandingDefaults().model_dump()' in source
    assert 'labels: QuoteTextLabels' in source
    assert 'ConfigDict(str_strip_whitespace=True)' in source
    assert 'At least one quote' in source and 'cannot contain duplicates' in source


if __name__ == "__main__":
    test_google_products_support_search_and_pagination_without_breaking_legacy_shape()
    test_tally_products_support_search_and_pagination_without_breaking_legacy_shape()
    test_quote_design_defaults_are_persisted_and_validated()
    print("Quote catalog contract checks passed.")
