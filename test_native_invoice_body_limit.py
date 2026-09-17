import asyncio

from starlette.responses import JSONResponse

from core.request_body_limit import NativeInvoiceBodyLimitMiddleware


async def _status(
    body: bytes,
    content_length: int | None = None,
    path: str = "/api/native-invoices",
    method: str = "POST",
) -> int:
    async def app(scope, receive, send):
        await receive()
        await JSONResponse({"ok": True})(scope, receive, send)

    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    await NativeInvoiceBodyLimitMiddleware(app)(
        {"type": "http", "method": method, "path": path, "headers": headers},
        receive,
        send,
    )
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


async def main() -> None:
    limit = NativeInvoiceBodyLimitMiddleware.JSON_LIMIT
    attachment_limit = NativeInvoiceBodyLimitMiddleware.ATTACHMENT_LIMIT
    assert await _status(b"{}", 2) == 200
    assert await _status(b"x" * (512 * 1024)) == 200
    assert await _status(b"", limit + 1) == 413
    assert await _status(b"x" * (limit + 1)) == 413
    assert await _status(b"", limit + 1, "/api/native-invoices/id/pdf") == 413
    assert await _status(b"x" * (limit + 1), path="/api/native-invoices/id/attachments") == 200
    assert await _status(b"", attachment_limit + 1, "/api/native-invoices/id/attachments") == 413
    for path in ("/api/quotes/settings", "/api/quotes/settings/iris-credentials"):
        assert await _status(b"", limit + 1, path, method="PUT") == 413
    assert await _status(b"", NativeInvoiceBodyLimitMiddleware.QUOTE_ASSET_LIMIT + 1, "/api/quotes/assets") == 413
    for path in ("/api/products/bulk-upload", "/api/contacts/bulk-upload"):
        assert await _status(b"", NativeInvoiceBodyLimitMiddleware.IMPORT_LIMIT + 1, path) == 413
    assert await _status(
        b"",
        NativeInvoiceBodyLimitMiddleware.JSON_LIMIT + 1,
        "/api/native-invoices/masters/import/validate",
    ) == 200
    assert await _status(
        b"",
        NativeInvoiceBodyLimitMiddleware.IMPORT_LIMIT + 1,
        "/api/native-invoices/masters/import/apply",
    ) == 413
    assert await _status(
        b"",
        NativeInvoiceBodyLimitMiddleware.TALLY_PDF_LIMIT + 1,
        "/api/tally/invoices/invoice-1/pdf",
        method="PUT",
    ) == 413
    assert await _status(b"x" * (limit + 1), path="/api/unrelated") == 200


if __name__ == "__main__":
    asyncio.run(main())
    print("Native invoice request body limits passed.")
