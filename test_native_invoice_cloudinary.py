import hashlib
import unittest
from unittest.mock import patch

from services.cloudinary_service import (
    native_invoice_pdf_download_url,
    upload_native_invoice_pdf,
)


class NativeInvoiceCloudinaryContractTests(unittest.IsolatedAsyncioTestCase):
    data = b"%PDF-1.7\nproduction-contract\n"
    digest = hashlib.sha256(data).hexdigest()
    public_id = f"native_invoices/org-1/invoice-1/v7-{digest}.pdf"

    def stored_asset(self, **changes):
        value = {
            "public_id": self.public_id,
            "asset_id": "asset-1",
            "version": 42,
            "resource_type": "raw",
            "type": "private",
            "format": "pdf",
            "bytes": len(self.data),
        }
        value.update(changes)
        return value

    async def test_upload_is_private_immutable_and_verified(self):
        with patch("services.cloudinary_service.cloudinary.uploader.upload", return_value=self.stored_asset()) as upload:
            result = await upload_native_invoice_pdf(
                self.data, "org-1", "invoice-1", "INV/26-27/000001",
                renderer_version=7, issued_revision=3,
            )
        kwargs = upload.call_args.kwargs
        self.assertEqual(kwargs["type"], "private")
        self.assertEqual(kwargs["resource_type"], "raw")
        self.assertFalse(kwargs["overwrite"])
        self.assertFalse(kwargs["unique_filename"])
        self.assertEqual(kwargs["context"]["sha256"], self.digest)
        self.assertEqual(result["sha256"], self.digest)
        self.assertEqual(result["byte_size"], len(self.data))

    async def test_ambiguous_upload_failure_recovers_only_the_exact_asset(self):
        with (
            patch("services.cloudinary_service.cloudinary.uploader.upload", side_effect=TimeoutError("unknown outcome")),
            patch("services.cloudinary_service.cloudinary.api.resource", return_value=self.stored_asset()) as resource,
        ):
            result = await upload_native_invoice_pdf(
                self.data, "org-1", "invoice-1", "INV/26-27/000001",
                renderer_version=7, issued_revision=3,
            )
        resource.assert_called_once_with(self.public_id, resource_type="raw", type="private")
        self.assertEqual(result["public_id"], self.public_id)

    async def test_missing_retry_asset_and_metadata_mismatches_fail_closed(self):
        with (
            patch("services.cloudinary_service.cloudinary.uploader.upload", side_effect=TimeoutError("upload failed")),
            patch("services.cloudinary_service.cloudinary.api.resource", side_effect=RuntimeError("not found")),
        ):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                await upload_native_invoice_pdf(
                    self.data, "org-1", "invoice-1", "INV/26-27/000001",
                    renderer_version=7, issued_revision=3,
                )

        for bad in (
            self.stored_asset(public_id="native_invoices/org-1/invoice-1/wrong.pdf"),
            self.stored_asset(bytes=len(self.data) + 1),
        ):
            with patch("services.cloudinary_service.cloudinary.uploader.upload", return_value=bad):
                with self.assertRaises(RuntimeError):
                    await upload_native_invoice_pdf(
                        self.data, "org-1", "invoice-1", "INV/26-27/000001",
                        renderer_version=7, issued_revision=3,
                    )

    def test_download_requires_verified_metadata_and_is_short_lived(self):
        with self.assertRaisesRegex(RuntimeError, "verified server artifact"):
            native_invoice_pdf_download_url({"public_id": self.public_id})
        pdf = {
            "public_id": self.public_id,
            "format": "pdf",
            "resource_type": "raw",
            "delivery_type": "private",
            "sha256": self.digest,
            "version": 42,
            "byte_size": len(self.data),
            "renderer_version": 7,
            "issued_revision": 3,
        }
        with patch("services.cloudinary_service.cloudinary.utils.private_download_url", return_value="https://signed.invalid/invoice") as signed:
            self.assertEqual(native_invoice_pdf_download_url(pdf, expires_in_seconds=60), "https://signed.invalid/invoice")
        kwargs = signed.call_args.kwargs
        self.assertTrue(kwargs["attachment"])
        self.assertEqual(kwargs["type"], "private")


if __name__ == "__main__":
    unittest.main()
