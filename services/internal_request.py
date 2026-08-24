import base64
import hashlib
import hmac
import json
import time
import uuid

from services.ai_reply_guard import AIProviderUnavailable


def signed_json(payload: dict, path: str, secret: str | None, request_id: str | None = None) -> tuple[bytes, dict[str, str]]:
    if not secret or len(secret) < 32:
        raise AIProviderUnavailable("configuration_error")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    if request_id:
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"heidelai:{path}:{request_id}"))
    else:
        request_id = str(uuid.uuid4())
    timestamp = str(int(time.time() * 1000))
    canonical = f"POST\n{path}\n{timestamp}\n{request_id}\n".encode() + body
    signature = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
    return body, {
        "Content-Type": "application/json",
        "X-Request-Id": request_id,
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }
