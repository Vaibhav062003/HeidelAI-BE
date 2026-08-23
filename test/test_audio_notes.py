import ast
import asyncio
from pathlib import Path
from typing import Optional


def _load_media_helpers():
    tree = ast.parse(Path("services/cloudinary_service.py").read_text())
    wanted = {
        "AUDIO_RETENTION_DAYS",
        "AUDIO_RETENTION_TAG",
        "_outbound_media_spec",
        "_outbound_upload_options",
        "_audio_retention_expression",
        "_cleanup_expired_audio_notes_sync",
        "_download_whatsapp_media",
    }
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.Assign) and any(getattr(target, "id", None) in wanted for target in node.targets))
        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted)
    ]
    namespace = {"Optional": Optional, "WA_VERSION": "v24.0"}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "media-helpers", "exec"), namespace)
    return namespace


def test_audio_contract():
    helpers = _load_media_helpers()
    media_spec = helpers["_outbound_media_spec"]
    assert media_spec("audio/webm;codecs=opus") == ("audio", "video", True)
    assert media_spec("audio/mp4") == ("audio", "video", False)
    assert media_spec("audio/ogg") == ("audio", "video", True)
    assert media_spec("audio/ogg;codecs=opus") == ("audio", "video", False)
    assert media_spec("image/png") == ("image", "image", False)

    audio_options = helpers["_outbound_upload_options"]("audio", True, "voice_note")
    assert audio_options == {
        "eager": [{"format": "ogg", "audio_codec": "opus"}],
        "tags": ["heidelai-audio-note"],
    }
    assert helpers["_outbound_upload_options"]("audio", True, "audio") == {"eager": [{"format": "mp3"}]}
    assert helpers["_outbound_upload_options"]("image", False) == {}
    assert helpers["_audio_retention_expression"](30) == "tags=heidelai-audio-note AND created_at<=30d"

    pages = {
        None: {
            "resources": [{"public_id": "voice-1", "resource_type": "video"}],
            "next_cursor": "page-2",
        },
        "page-2": {
            "resources": [{"public_id": "voice-2", "resource_type": "raw"}],
        },
    }
    deletions = []
    pages_read = 0

    class FakeSearch:
        def __init__(self):
            self.cursor = None

        def expression(self, value):
            assert value == "tags=heidelai-audio-note AND created_at<=30d"
            return self

        def max_results(self, value):
            assert value == 500
            return self

        def next_cursor(self, value):
            self.cursor = value
            return self

        def execute(self):
            nonlocal pages_read
            pages_read += 1
            return pages[self.cursor]

    class FakeApi:
        @staticmethod
        def delete_resources(public_ids, resource_type):
            assert pages_read == 2
            deletions.append((resource_type, public_ids))
            return {"deleted": {public_id: "deleted" for public_id in public_ids}}

    class FakeCloudinary:
        api = FakeApi()

        @staticmethod
        def Search():
            return FakeSearch()

    helpers["_cleanup_expired_audio_notes_sync"].__globals__["cloudinary"] = FakeCloudinary()
    assert helpers["_cleanup_expired_audio_notes_sync"](30) == 2
    assert deletions == [("video", ["voice-1"]), ("raw", ["voice-2"])]

    class FakeResponse:
        def __init__(self, status_code, data=None, content=b""):
            self.status_code = status_code
            self._data = data or {}
            self.content = content

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self):
            self.urls = []

        async def get(self, url, **_kwargs):
            self.urls.append(url)
            if url.startswith("https://graph.facebook.com/"):
                return FakeResponse(200, {"url": "https://lookaside.example/audio", "mime_type": "audio/ogg"})
            return FakeResponse(200, content=b"voice-bytes")

    client = FakeClient()
    media_bytes, mime_type = asyncio.run(helpers["_download_whatsapp_media"](
        client, None, "media-1", "application/octet-stream", {"Authorization": "Bearer test"}, "phone-1"
    ))
    assert media_bytes == b"voice-bytes"
    assert mime_type == "audio/ogg"
    assert client.urls == [
        "https://graph.facebook.com/v24.0/media-1",
        "https://lookaside.example/audio",
    ]

    routes = ast.parse(Path("api/common_routes.py").read_text())
    forwarded = {
        keyword.arg
        for node in ast.walk(routes)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "send_message"
        for keyword in node.keywords
    }
    assert {"media_kind", "duration_ms"} <= forwarded

    webhook = ast.parse(Path("api/webhook_routes.py").read_text())
    media_type_sets = [
        {item.value for item in comparator.elts if isinstance(item, ast.Constant)}
        for node in ast.walk(webhook)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "message_type"
        and any(isinstance(operator, ast.In) for operator in node.ops)
        for comparator in node.comparators
        if isinstance(comparator, (ast.List, ast.Tuple, ast.Set))
    ]
    assert any({"image", "video", "document", "audio"} <= values for values in media_type_sets)

    background_calls = [
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_task"
        and node.args
        and getattr(node.args[0], "id", None) == "upload_and_send_whatsapp_media_background"
    ]
    assert background_calls
    assert "phone_number_id" in {keyword.arg for keyword in background_calls[0].keywords}
    store_calls = [
        node for node in ast.walk(webhook)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "store_whatsapp_message"
        and any(keyword.arg == "notify_push" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in node.keywords)
    ]
    media_store = next(
        call for call in store_calls
        if 0 < background_calls[0].lineno - call.lineno < 50
    )
    assert {"context", "context_type"} <= {keyword.arg for keyword in media_store.keywords}


if __name__ == "__main__":
    test_audio_contract()
    print("Audio note checks passed.")
