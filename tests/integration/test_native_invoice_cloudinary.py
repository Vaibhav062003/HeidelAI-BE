"""Opt-in live Cloudinary smoke test; skipped unless dedicated test credentials exist."""

import asyncio
import os
import uuid
import unittest


REQUIRED_ENV = (
    "CLOUDINARY_CLOUD_NAME",
    "CLOUDINARY_API_KEY",
    "CLOUDINARY_API_SECRET",
)
LIVE_ENABLED = os.getenv("RUN_CLOUDINARY_INTEGRATION") == "1" and all(os.getenv(name) for name in REQUIRED_ENV)


@unittest.skipUnless(LIVE_ENABLED, "set RUN_CLOUDINARY_INTEGRATION=1 and dedicated Cloudinary credentials")
class NativeInvoiceCloudinaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_upload_lookup_and_signed_delivery(self):
        import cloudinary.api
        import cloudinary.uploader

        from services.cloudinary_service import native_invoice_pdf_download_url, upload_native_invoice_pdf

        token = uuid.uuid4().hex
        data = b"%PDF-1.7\nCloudinary integration sentinel\n"
        stored = None
        try:
            stored = await upload_native_invoice_pdf(
                data,
                f"ci-{token}",
                f"invoice-{token}",
                f"CI-{token}",
                renderer_version=1,
                issued_revision=1,
            )
            remote = await asyncio.to_thread(
                cloudinary.api.resource,
                stored["public_id"],
                resource_type="raw",
                type="private",
            )
            self.assertEqual(remote["public_id"], stored["public_id"])
            self.assertEqual(int(remote["bytes"]), len(data))
            self.assertTrue(native_invoice_pdf_download_url(stored, expires_in_seconds=60).startswith("http"))
        finally:
            if stored:
                await asyncio.to_thread(
                    cloudinary.uploader.destroy,
                    stored["public_id"],
                    resource_type="raw",
                    type="private",
                    invalidate=True,
                )


if __name__ == "__main__":
    unittest.main()
