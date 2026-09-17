import hashlib
import os
from urllib.parse import urlparse

from slowapi import Limiter
from slowapi.util import get_remote_address


_NON_PRODUCTION_ENVIRONMENTS = {"dev", "development", "test", "testing", "staging"}


def rate_limit_storage_uri(
    *,
    configured_uri: str | None = None,
    environment: str | None = None,
    native_org_ids: str | None = None,
    gateway_enforced: str | None = None,
    koyeb_service_name: str | None = None,
) -> str:
    """Resolve rate-limit storage without silently using process memory in production.

    A trusted gateway may own the distributed limit, but that exception must be
    explicit. Development, test, and staging retain SlowAPI's in-memory backend.
    """

    configured_uri = (configured_uri if configured_uri is not None else os.getenv("RATE_LIMIT_STORAGE_URI", "")).strip()
    environment = (environment if environment is not None else os.getenv("ENV", "prod")).strip().lower()
    native_org_ids = native_org_ids if native_org_ids is not None else os.getenv("NATIVE_INVOICING_ORG_IDS", "")
    gateway_enforced = gateway_enforced if gateway_enforced is not None else os.getenv("RATE_LIMIT_GATEWAY_ENFORCED", "")
    koyeb_service_name = koyeb_service_name if koyeb_service_name is not None else os.getenv("KOYEB_SERVICE_NAME", "")

    if configured_uri:
        parsed = urlparse(configured_uri)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise RuntimeError("RATE_LIMIT_STORAGE_URI must be a redis:// or rediss:// URL")
        return configured_uri

    production = bool(koyeb_service_name.strip()) or (environment and environment not in _NON_PRODUCTION_ENVIRONMENTS)
    native_enabled = bool(str(native_org_ids).strip())
    gateway_is_enforced = str(gateway_enforced).strip().lower() in {"1", "true", "yes"}
    if production and native_enabled and not gateway_is_enforced:
        raise RuntimeError(
            "Native invoicing in production requires RATE_LIMIT_STORAGE_URI or "
            "RATE_LIMIT_GATEWAY_ENFORCED=true"
        )
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
