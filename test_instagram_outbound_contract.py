import ast
from pathlib import Path


def _function(tree: ast.Module, name: str):
    return next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _calls(function, name: str):
    return [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name)
    ]


def test_instagram_outbound_messages_are_confirmed_persisted_and_deduplicated():
    instagram = ast.parse(Path("services/ig_service.py").read_text())
    for sender_name in ("send_ig_message", "send_ig_media_message"):
        sender = _function(instagram, sender_name)
        assert _calls(sender, "raise_for_status")
        assert _calls(sender, "_claim_instagram_send")
        assert _calls(sender, "_record_confirmed_instagram_outbound")

    token_lookup = _function(instagram, "_instagram_access_token")
    assert any(
        isinstance(node, ast.Dict)
        and {key.value for key in node.keys if isinstance(key, ast.Constant)} >= {"org_id", "instagram_id", "is_active"}
        for node in ast.walk(token_lookup)
    )

    store = _function(instagram, "store_instagram_message")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "update_one"
        and any(keyword.arg == "upsert" for keyword in node.keywords)
        for node in ast.walk(store)
    )
    assert _calls(store, "_instagram_message_storage_id")

    outbound_claim = _function(instagram, "_claim_instagram_send")
    assert "fingerprint" in {arg.arg for arg in outbound_claim.args.args}
    assert any(
        isinstance(node, ast.Constant) and node.value == "claim_token"
        for node in ast.walk(outbound_claim)
    )
    webhook_claim = _function(instagram, "claim_instagram_webhook_message")
    assert any(
        isinstance(node, ast.Constant) and node.value == "message_id"
        for node in ast.walk(webhook_claim)
    )
    upload = _function(instagram, "upload_and_send_ig_media_message")
    assert _calls(upload, "_claim_instagram_send")[0].lineno < _calls(upload, "upload_media_to_cloudinary")[0].lineno
    downstream_claim = _function(instagram, "claim_instagram_webhook_downstream")
    assert any(
        isinstance(node, ast.Set)
        and {item.value for item in node.elts if isinstance(item, ast.Constant)} == {"pending", "processing"}
        for node in ast.walk(downstream_claim)
    )
    assert _calls(_function(instagram, "complete_instagram_webhook_message"), "update_one")

    webhook = ast.parse(Path("api/webhook_routes.py").read_text())
    handler = _function(webhook, "handle_webhook")
    claim_line = _calls(handler, "claim_instagram_webhook_message")[0].lineno
    conversation_line = next(
        node.lineno for node in _calls(handler, "create_or_update_instagram_conversation")
        if node.lineno > claim_line
    )
    assert claim_line < conversation_line

    routes = ast.parse(Path("api/common_routes.py").read_text())
    get_conversation = _function(routes, "get_conversation")
    assert not ({"platform", "mode", "temp_id"} & {
        node.id for node in ast.walk(get_conversation) if isinstance(node, ast.Name)
    })
    send_endpoint = _function(routes, "send_message_endpoint")
    assert {"platform", "mode", "temp_id"} <= {
        node.id for node in ast.walk(send_endpoint) if isinstance(node, ast.Name)
    }


if __name__ == "__main__":
    test_instagram_outbound_messages_are_confirmed_persisted_and_deduplicated()
