import json
import io
import os
import struct
import sys
import uuid
import wave


required = (
    "MONGODB_URI",
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
    "APP_SECRET",
)
missing = [name for name in required if not os.getenv(name)]
errors = [f"missing {name}" for name in missing]

try:
    ice_servers = json.loads(os.getenv("WHATSAPP_CALL_ICE_SERVERS_JSON", "[]"))
except json.JSONDecodeError:
    ice_servers = []
    errors.append("WHATSAPP_CALL_ICE_SERVERS_JSON is not valid JSON")

has_turn = False
for server in ice_servers if isinstance(ice_servers, list) else []:
    if not isinstance(server, dict):
        continue
    urls = server.get("urls")
    urls = [urls] if isinstance(urls, str) else urls if isinstance(urls, list) else []
    has_turn = has_turn or bool(
        any(url.startswith(("turn:", "turns:")) for url in urls if isinstance(url, str))
        and server.get("username")
        and server.get("credential")
    )
has_turn = has_turn or bool(os.getenv("WHATSAPP_CALL_TURN_SECRET") and os.getenv("WHATSAPP_CALL_TURN_URLS"))
if not has_turn:
    errors.append("configure coturn with WHATSAPP_CALL_TURN_SECRET and WHATSAPP_CALL_TURN_URLS, or provide credentialed TURN JSON")

if errors:
    print("WhatsApp call production preflight failed: " + "; ".join(errors), file=sys.stderr)
    raise SystemExit(1)

if "--live-cloudinary" in sys.argv:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils
    import urllib.request

    cloudinary.config(
        cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
        api_key=os.environ["CLOUDINARY_API_KEY"],
        api_secret=os.environ["CLOUDINARY_API_SECRET"],
    )
    audio = io.BytesIO()
    audio.name = "preflight.wav"
    with wave.open(audio, "wb") as output:
        output.setparams((1, 2, 8000, 800, "NONE", "not compressed"))
        output.writeframes(b"".join(struct.pack("<h", 0) for _ in range(800)))
    audio.seek(0)
    public_id = f"whatsapp_call_preflight/{uuid.uuid4()}"
    uploaded = None
    try:
        uploaded = cloudinary.uploader.upload(audio, public_id=public_id, resource_type="video", type="private")
        url = cloudinary.utils.private_download_url(
            uploaded["public_id"], uploaded.get("format") or "wav", resource_type="video", type="private"
        )
        with urllib.request.urlopen(url, timeout=30) as response:
            if len(response.read()) < 44:
                raise RuntimeError("Cloudinary returned an empty recording")
    finally:
        if uploaded:
            removed = cloudinary.uploader.destroy(uploaded["public_id"], resource_type="video", type="private", invalidate=True)
            if removed.get("result") not in {"ok", "not found"}:
                raise RuntimeError("Cloudinary did not remove its preflight recording")
print("WhatsApp call production preflight passed.")
