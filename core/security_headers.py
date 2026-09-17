"""Small ASGI middleware for browser-facing response hardening."""

from starlette.datastructures import MutableHeaders


class SecurityHeadersMiddleware:
    """Attach headers that are safe for API responses and development docs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message):
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["content-security-policy"] = "frame-ancestors 'none'"
                headers["cross-origin-opener-policy"] = "same-origin"
                headers["permissions-policy"] = "camera=(), geolocation=(), microphone=()"
                headers["referrer-policy"] = "no-referrer"
                headers["x-content-type-options"] = "nosniff"
                headers["x-frame-options"] = "DENY"
                if scope.get("scheme") == "https":
                    headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
