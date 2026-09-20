"""ASGI request-size guard for invoice, settings, and bulk-import writes."""

from starlette.responses import JSONResponse

from services.whatsapp_history_import import MAX_REQUEST_BYTES


class RequestBodyTooLarge(Exception):
    pass


class NativeInvoiceBodyLimitMiddleware:
    JSON_LIMIT = 512 * 1024
    ATTACHMENT_LIMIT = 3 * 1024 * 1024 + 64 * 1024
    QUOTE_ASSET_LIMIT = 5 * 1024 * 1024 + 64 * 1024
    IMPORT_LIMIT = 10 * 1024 * 1024 + 64 * 1024
    TALLY_PDF_LIMIT = 10_000_000

    def __init__(self, app):
        self.app = app

    @classmethod
    def limit_for(cls, path: str, method: str) -> int | None:
        method = method.upper()
        if method in {"GET", "HEAD", "OPTIONS"}:
            return None
        if method == "POST" and path.rstrip("/") == "/api/whatsapp/history-imports":
            return MAX_REQUEST_BYTES
        if path.startswith("/api/native-invoices"):
            if path.startswith("/api/native-invoices/master"):
                return cls.IMPORT_LIMIT
            if method == "POST" and path.endswith("/attachments"):
                return cls.ATTACHMENT_LIMIT
            return cls.JSON_LIMIT
        if path in {"/api/quotes/settings", "/api/quotes/settings/iris-credentials"}:
            return cls.JSON_LIMIT
        if path == "/api/quotes/assets":
            return cls.QUOTE_ASSET_LIMIT
        if path in {"/api/products/bulk-upload", "/api/contacts/bulk-upload"}:
            return cls.IMPORT_LIMIT
        if method == "PUT" and path.startswith("/api/tally/invoices/") and path.endswith("/pdf"):
            return cls.TALLY_PDF_LIMIT
        return None

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(path, method)
        if limit is None:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope, receive, send, limit: int) -> None:
        response = JSONResponse(
            status_code=413,
            content={"error": "request_too_large", "detail": f"Request body exceeds {limit} bytes"},
        )
        await response(scope, receive, send)
