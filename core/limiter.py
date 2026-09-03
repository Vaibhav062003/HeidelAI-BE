import hashlib

from slowapi import Limiter
from slowapi.util import get_remote_address


def rate_limit_key(request) -> str:
    identity = getattr(request.state, "rate_limit_identity", None)
    if identity:
        return identity
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"bearer:{fingerprint}"
    # Forwarding headers are trusted only when the server has already resolved
    # them into request.client; never use raw X-Forwarded-For here.
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=rate_limit_key, default_limits=["100/minute"])
