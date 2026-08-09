import ast
from pathlib import Path


def _load_media_helpers():
    tree = ast.parse(Path("services/cloudinary_service.py").read_text())
    wanted = {
        "AUDIO_RETENTION_DAYS",
        "AUDIO_RETENTION_TAG",
        "_outbound_media_spec",
        "_outbound_upload_options",
        "_audio_retention_expression",
        "_cleanup_expired_audio_notes_sync",
    }
    nodes = [
        node for node in tree.body
        if (isinstance(node, ast.Assign) and any(getattr(target, "id", None) in wanted for target in node.targets))
        or (isinstance(node, ast.FunctionDef) and node.name in wanted)
    ]
    namespace = {}
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

    audio_options = helpers["_outbound_upload_options"]("audio", True)
    assert audio_options == {"eager": [{"format": "mp3"}], "tags": ["heidelai-audio-note"]}
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
            return pages[self.cursor]

    class FakeApi:
        @staticmethod
        def delete_resources(public_ids, resource_type):
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

    routes = ast.parse(Path("api/common_routes.py").read_text())
    forwarded = {
        keyword.arg
        for node in ast.walk(routes)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "send_message"
        for keyword in node.keywords
    }
    assert {"media_kind", "duration_ms"} <= forwarded


if __name__ == "__main__":
    test_audio_contract()
    print("Audio note checks passed.")
