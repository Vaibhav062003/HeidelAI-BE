import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

from starlette.responses import JSONResponse

from core.limiter import rate_limit_key, rate_limit_storage_uri
from core.security_headers import SecurityHeadersMiddleware


async def _response_headers(scheme: str) -> dict[str, str]:
    async def app(_scope, _receive, send):
        await JSONResponse({"ok": True})(_scope, _receive, send)

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await SecurityHeadersMiddleware(app)(
        {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "scheme": scheme,
            "headers": [],
            "client": ("127.0.0.1", 1234),
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    return {key.decode().lower(): value.decode() for key, value in start["headers"]}


def test_security_headers() -> None:
    http = asyncio.run(_response_headers("http"))
    https = asyncio.run(_response_headers("https"))
    assert http["content-security-policy"] == "frame-ancestors 'none'"
    assert http["cross-origin-opener-policy"] == "same-origin"
    assert http["permissions-policy"] == "camera=(), geolocation=(), microphone=()"
    assert http["referrer-policy"] == "no-referrer"
    assert http["x-content-type-options"] == "nosniff"
    assert http["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in http
    assert https["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_rate_limit_identity_does_not_trust_forwarding_headers() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(rate_limit_identity="auth:org-1:user-1"),
        headers={"authorization": "Bearer secret", "x-forwarded-for": "203.0.113.5"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert rate_limit_key(request) == "auth:org-1:user-1"
    request.state = SimpleNamespace()
    assert rate_limit_key(request).startswith("bearer:")
    assert "secret" not in rate_limit_key(request)
    request.headers = {"x-forwarded-for": "203.0.113.5"}
    assert rate_limit_key(request) == "ip:127.0.0.1"


def test_rate_limit_storage_is_optional() -> None:
    assert rate_limit_storage_uri(configured_uri="") == "memory://"
    assert rate_limit_storage_uri(configured_uri="rediss://cache.internal:6380/0") == "rediss://cache.internal:6380/0"
    try:
        rate_limit_storage_uri(configured_uri="http://cache.internal")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Rate-limit storage accepted a non-Redis URL")


def test_every_native_invoice_route_has_an_explicit_limit() -> None:
    tree = ast.parse(Path("api/native_invoice_routes.py").read_text())
    routes = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorators = [ast.unparse(decorator) for decorator in node.decorator_list]
        if any(value.startswith("router.") for value in decorators):
            routes.append((node.name, decorators))
    assert routes
    assert all(any(value.startswith("limiter.limit(") for value in decorators) for _, decorators in routes)


def test_container_runs_non_root_with_database_readiness() -> None:
    routes = Path("api/common_routes.py").read_text()
    dockerfile = Path("Dockerfile").read_text()
    assert '@router.get("/ready")' in routes
    assert 'async_db.command("ping")' in routes
    assert "asyncio.wait_for" in routes
    assert "USER app" in dockerfile
    assert "/ready" in dockerfile


if __name__ == "__main__":
    test_security_headers()
    test_rate_limit_identity_does_not_trust_forwarding_headers()
    test_rate_limit_storage_is_optional()
    test_every_native_invoice_route_has_an_explicit_limit()
    test_container_runs_non_root_with_database_readiness()
    print("Security headers and rate-limit contracts passed.")
