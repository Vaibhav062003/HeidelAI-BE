"""Real replica-set integration tests for invoice transactions and backups."""

import asyncio
import base64
import copy
import importlib
import os
import sys
import tempfile
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


MONGO_URI = os.getenv("MONGODB_TEST_URI", "").strip()


@unittest.skipUnless(MONGO_URI, "set MONGODB_TEST_URI to run replica-set integration tests")
class NativeInvoiceMongoIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = f"heidelai_invoice_ci_{uuid.uuid4().hex}"
        self.restore_database = f"{self.database}_restore"
        os.environ["MONGODB_URI"] = MONGO_URI
        os.environ["MONGODB_CLUSTER"] = self.database
        os.environ["NATIVE_INVOICING_ORG_IDS"] = "ci-org"
        settings = importlib.import_module("config.settings")
        settings.MONGODB_URI = MONGO_URI
        settings.MONGODB_CLUSTER = self.database
        database = importlib.import_module("database")
        database.MONGODB_URI = MONGO_URI
        database.MONGODB_CLUSTER = self.database
        if database._async_client is not None:
            database._async_client.close()
        database._async_client = None
        database._async_db = None
        for module in (
            "services.native_invoice_adjustment_service",
            "services.native_invoice_asset_service",
            "services.native_invoice_service",
        ):
            sys.modules.pop(module, None)
        self.service = importlib.import_module("services.native_invoice_service")
        self.service.CLOUDINARY_CLOUD_NAME = "integration-test"
        self.service.CLOUDINARY_API_KEY = "integration-test"
        self.service.CLOUDINARY_API_SECRET = "integration-test"
        await self.service.db.client.drop_database(self.database)
        await self.service.db.client.drop_database(self.restore_database)
        await self.service.ensure_native_invoice_indexes()
        self.seller = {
            "name": "CI Seller",
            "gstin": "27AAPFU0939F1ZV",
            "address": "Mumbai, Maharashtra",
            "state_code": "27",
        }
        await self.service.db["quote_settings_ci-org"].insert_one({
            "_id": "branding",
            "seller": self.seller,
            "e_invoice_applicability": {
                "status": "not_required", "gstin": self.seller["gstin"], "not_required_reason": "below_threshold",
            },
        })

    async def asyncTearDown(self):
        await self.service.db.client.drop_database(self.database)
        await self.service.db.client.drop_database(self.restore_database)
        self.service.db.client.close()
        database = importlib.import_module("database")
        database._async_client = None
        database._async_db = None

    def payload(self, description: str) -> dict:
        signature_file = BytesIO()
        Image.new("RGB", (32, 8), "white").save(signature_file, format="PNG")
        signature = signature_file.getvalue()
        return {
            "invoice_date": self.service._today(),
            "due_date": self.service._today(),
            "seller": copy.deepcopy(self.seller),
            "customer": {
                "name": "CI Customer",
                "gstin": "",
                "address": "Bengaluru, Karnataka",
                "state_code": "29",
            },
            "place_of_supply_state_code": "29",
            "shipping_address": "Bengaluru, Karnataka",
            "shipping_state_code": "29",
            "reverse_charge": False,
            "authorized_signatory": "CI Signatory",
            "signature_image": {
                "filename": "signature.png", "content_type": "image/png", "byte_size": len(signature),
                "data_base64": base64.b64encode(signature).decode("ascii"),
            },
            "items": [{
                "id": uuid.uuid4().hex,
                "description": description,
                "hsn_sac": "8471",
                "quantity": "1",
                "uqc": "NOS",
                "unit_price_paise": 10_000,
                "gst_rate_bps": 1800,
                "gst_inclusive": False,
                "supply_type": "goods",
            }],
            "discount_paise": 0,
            "notes": "",
            "terms": [],
        }

    async def _exercise_concurrency(self):
        payload = self.payload("Idempotent draft")
        repeated = await asyncio.gather(*(
            self.service.create_invoice("ci-org", "actor", "CI", copy.deepcopy(payload), "create-shared")
            for _ in range(8)
        ))
        self.assertEqual(len({row["invoice_id"] for row in repeated}), 1)
        first = repeated[0]
        second = await self.service.create_invoice(
            "ci-org", "actor", "CI", self.payload("Second invoice"), "create-second",
        )
        with patch.object(self.service, "render_invoice_pdf", new=AsyncMock(return_value={"state": "ready"})):
            replays = await asyncio.gather(*(
                self.service.issue_invoice("ci-org", first["invoice_id"], "actor", 1, "issue-shared")
                for _ in range(6)
            ))
            issued_second = await self.service.issue_invoice(
                "ci-org", second["invoice_id"], "actor", 1, "issue-second",
            )
        year = self.service.financial_year(self.service._today())
        self.assertEqual({row["invoice_number"] for row in replays}, {f"INV/{year}/000001"})
        self.assertEqual(issued_second["invoice_number"], f"INV/{year}/000002")
        sequence = await self.service.sequences.find_one({"_id": f"ci-org:{year}:invoice:INV"})
        self.assertEqual(sequence["value"], 2)

    async def test_transactional_numbering_idempotency_and_backup_restore(self):
        from scripts.native_invoice_backup import BackupError, export_backup, restore_backup, verify_backup

        await self._exercise_concurrency()
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp) / "native-invoice-backup"
            manifest = await asyncio.to_thread(export_backup, MONGO_URI, self.database, backup, org_id="ci-org")
            verified = await asyncio.to_thread(verify_backup, backup)
            self.assertEqual(verified, manifest)
            restored = await asyncio.to_thread(
                restore_backup, MONGO_URI, self.restore_database, backup, org_id="ci-org",
            )
            self.assertEqual(restored["collections"]["native_invoices"]["documents"], 2)
            self.assertIn("native_invoice_adjustments", restored["collections"])
            self.assertIn("native_invoice_attachments", restored["collections"])
            self.assertIn("iris_irp_credentials", restored["collections"])
            self.assertIn("quote_settings_ci-org", restored["collections"])
            with self.assertRaises(BackupError):
                await asyncio.to_thread(
                    restore_backup, MONGO_URI, self.restore_database, backup, org_id="ci-org",
                )

    async def test_refund_reversal_updates_invoice_and_credit_note_atomically(self):
        adjustment_service = importlib.import_module("services.native_invoice_adjustment_service")
        draft = await self.service.create_invoice(
            "ci-org", "actor", "CI", self.payload("Refund reversal"), "create-refund-case",
        )
        with patch.object(self.service, "render_invoice_pdf", new=AsyncMock(return_value={"state": "ready"})):
            issued = await self.service.issue_invoice(
                "ci-org", draft["invoice_id"], "actor", draft["revision"], "issue-refund-case",
            )
        paid = await self.service.record_payment(
            "ci-org", issued["invoice_id"], "actor",
            {"amount_paise": issued["total_paise"], "paid_on": self.service._today(), "method": "bank_transfer", "reference": "PAY-1", "note": ""},
            "payment-refund-case",
        )
        source_item = paid["items"][0]
        adjustment = await adjustment_service.create_adjustment(
            "ci-org", paid["invoice_id"], "actor", {
                "kind": "credit_note",
                "note_date": self.service._today(),
                "reason": "Partial return",
                "discount_paise": 0,
                "items": [{
                    "id": uuid.uuid4().hex,
                    "source_item_id": source_item["id"],
                    "description": source_item["description"],
                    "hsn_sac": source_item["hsn_sac"],
                    "quantity": "0.5",
                    "uqc": source_item["uqc"],
                    "unit_price_paise": source_item["unit_price_paise"],
                    "gst_rate_bps": source_item["gst_rate_bps"],
                    "gst_inclusive": source_item["gst_inclusive"],
                    "supply_type": source_item["supply_type"],
                }],
            }, "adjustment-refund-case",
        )
        with patch.object(adjustment_service, "render_adjustment_pdf", new=AsyncMock(return_value={"state": "ready"})):
            note = await adjustment_service.issue_adjustment(
                "ci-org", adjustment["adjustment_id"], "actor", adjustment["revision"], "issue-adjustment-refund",
            )
        refunded = await adjustment_service.record_refund(
            "ci-org", paid["invoice_id"], "actor", {
                "credit_note_id": note["adjustment_id"],
                "amount_paise": note["total_paise"],
                "refunded_on": self.service._today(),
                "method": "bank_transfer",
                "reference": "REFUND-1",
                "note": "",
            }, "record-refund-case",
        )
        payment_id = paid["payments"][0]["payment_id"]
        with self.assertRaisesRegex(HTTPException, "Reverse the related refunds"):
            await self.service.reverse_payment(
                "ci-org", paid["invoice_id"], payment_id, "actor", "Payment entered twice", "reverse-payment-before-refund",
            )
        refund_id = refunded["refunds"][0]["refund_id"]
        reversed_invoice = await adjustment_service.reverse_refund(
            "ci-org", paid["invoice_id"], refund_id, "actor", "Refund entered twice", "reverse-refund-case",
        )
        self.assertEqual(reversed_invoice["amount_refunded_paise"], 0)
        self.assertIsNotNone(reversed_invoice["refunds"][0]["reversed_at"])
        stored_note = await adjustment_service.get_adjustment("ci-org", note["adjustment_id"])
        self.assertEqual(stored_note["refunded_paise"], 0)

    async def test_issued_pdf_render_cannot_overwrite_void_transition(self):
        draft = await self.service.create_invoice(
            "ci-org", "actor", "CI", self.payload("PDF race"), "create-pdf-race",
        )
        with patch.object(self.service, "render_invoice_pdf", new=AsyncMock(return_value={"state": "pending"})):
            issued = await self.service.issue_invoice(
                "ci-org", draft["invoice_id"], "actor", draft["revision"], "issue-pdf-race",
            )

        async def upload_after_void(data, org_id, invoice_id, invoice_number, *, renderer_version, issued_revision):
            await self.service.invoices.update_one(
                {"org_id": org_id, "invoice_id": invoice_id},
                {"$set": {"lifecycle": "void", "voided_revision": issued_revision + 1, "pdf_state": "pending"}, "$inc": {"revision": 1}},
            )
            return {
                "public_id": "native_invoices/race.pdf", "version": 1, "resource_type": "raw", "delivery_type": "private",
                "format": "pdf", "sha256": "a" * 64, "byte_size": len(data), "renderer_version": renderer_version,
                "issued_revision": issued_revision, "generated_at": self.service.datetime.now(self.service.timezone.utc),
            }

        with patch.object(self.service, "upload_native_invoice_pdf", new=upload_after_void):
            result = await self.service.render_invoice_pdf("ci-org", issued["invoice_id"], "actor")
        stored = await self.service.get_invoice("ci-org", issued["invoice_id"])
        self.assertEqual(result["state"], "pending")
        self.assertNotIn("pdf", stored)


if __name__ == "__main__":
    unittest.main()
