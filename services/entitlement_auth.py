import base64
import hashlib
import hmac
import time

from fastapi import HTTPException


def verify_entitlement_request(method: str, path: str, timestamp: str, request_id: str, signature: str, body: bytes, secret: str, now_ms: int | None = None) -> None:
    if not secret:
        raise HTTPException(status_code=503, detail={"code": "entitlement_not_configured", "message": "Entitlement synchronization is not configured."})
    try:
        issued_at = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid entitlement timestamp") from exc
    if abs((now_ms if now_ms is not None else int(time.time() * 1000)) - issued_at) > 5 * 60 * 1000:
        raise HTTPException(status_code=401, detail="Expired entitlement request")
    canonical = f"{method.upper()}\n{path}\n{timestamp}\n{request_id}\n".encode() + body
    expected = base64.b64encode(hmac.new(secret.encode(), canonical, hashlib.sha256).digest()).decode()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid entitlement signature")
