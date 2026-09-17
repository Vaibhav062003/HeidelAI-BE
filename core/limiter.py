import hashlib
import os
from urllib.parse import urlparse

from slowapi import Limiter
from slowapi.util import get_remote_address


def rate_limit_storage_uri(
    *,
    configured_uri: str | None = None,
) -> str:
    """Use configured Redis storage, otherwise keep process-local counters."""

    configured_uri = (configured_uri if configured_uri is not None else os.getenv("RATE_LIMIT_STORAGE_URI", "")).strip()
    if configured_uri:
        parsed = urlparse(configured_uri)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RuntimeError("RATE_LIMIT_STORAGE_URI must be a redis:// or rediss:// URL")
        return configured_uri
    # ponytail: counters are process-local; configure Redis only when cross-replica limits are needed.
    return "memory://"


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


limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=["100/minute"],
    storage_uri=rate_limit_storage_uri(),
)
