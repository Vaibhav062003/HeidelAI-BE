import ast
from pathlib import Path


def _load_media_spec():
    tree = ast.parse(Path("services/cloudinary_service.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_outbound_media_spec")
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "media-spec", "exec"), namespace)
    return namespace["_outbound_media_spec"]


def test_audio_contract():
    media_spec = _load_media_spec()
    assert media_spec("audio/webm;codecs=opus") == ("audio", "video", True)
    assert media_spec("audio/mp4") == ("audio", "video", False)
    assert media_spec("audio/ogg") == ("audio", "video", True)
    assert media_spec("audio/ogg;codecs=opus") == ("audio", "video", False)
    assert media_spec("image/png") == ("image", "image", False)

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
