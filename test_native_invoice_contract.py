import ast
import asyncio
import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from unittest.mock import patch

from services.feature_flags import native_invoicing_enabled, tally_enabled


class HTTPException(Exception):
    def __init__(self, status_code=None, detail=None):
        super().__init__(detail)
        self.status_code = status_code


source = Path("services/native_invoice_service.py").read_text()
tree = ast.parse(source)
functions = {"_round", "calculate_invoice_totals", "financial_year", "request_fingerprint", "validate_invoice_date", "validate_payment_date", "validate_einvoice_applicability", "quote_claim_id", "claim_quote_revision", "normalize_numbering_config", "format_document_number", "invoice_amounts", "invoice_provider"}
body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions]
namespace = {
    "Decimal": Decimal, "ROUND_HALF_UP": ROUND_HALF_UP, "deepcopy": deepcopy,
    "HTTPException": HTTPException, "date": date, "MAX_PAISE": 999_999_999_00,
    "hashlib": hashlib, "json": json, "datetime": datetime, "timezone": timezone,
    "native_invoicing_enabled": native_invoicing_enabled, "tally_enabled": tally_enabled,
    "re": re, "DEFAULT_NUMBERING": {
        "invoice": {"series": "INV", "prefix": "INV/", "suffix": "", "leading_zeros": 6},
        "credit_note": {"series": "CN", "prefix": "CN/", "suffix": "", "leading_zeros": 6},
        "debit_note": {"series": "DN", "prefix": "DN/", "suffix": "", "leading_zeros": 6},
    },
}
exec(compile(ast.Module(body=body, type_ignores=[]), "native_invoice_math", "exec"), namespace)
calculate_invoice_totals = namespace["calculate_invoice_totals"]
financial_year = namespace["financial_year"]
request_fingerprint = namespace["request_fingerprint"]
validate_invoice_date = namespace["validate_invoice_date"]
validate_payment_date = namespace["validate_payment_date"]
validate_einvoice_applicability = namespace["validate_einvoice_applicability"]
quote_claim_id = namespace["quote_claim_id"]
claim_quote_revision = namespace["claim_quote_revision"]
normalize_numbering_config = namespace["normalize_numbering_config"]
format_document_number = namespace["format_document_number"]
invoice_amounts = namespace["invoice_amounts"]
invoice_provider = namespace["invoice_provider"]


def line(**changes):
    value = {
        "id": "line-1", "description": "Workstation", "hsn_sac": "8471", "quantity": "1",
        "uqc": "NOS", "unit_price_paise": 10_000, "gst_rate_bps": 1800,
        "gst_inclusive": False, "supply_type": "goods",
    }
    value.update(changes)
    return value


def payload(*items, supplier="27", place="27", discount=0):
    return {
        "seller": {"state_code": supplier}, "place_of_supply_state_code": place,
        "items": list(items), "discount_paise": discount,
    }


def test_integer_paise_gst_math():
    intra = calculate_invoice_totals(payload(line(quantity="2")))
    assert intra["taxable_value_paise"] == 20_000
    assert intra["cgst_paise"] == intra["sgst_paise"] == 1_800
    assert intra["total_paise"] == 23_600

    interstate = calculate_invoice_totals(payload(line(unit_price_paise=11_800, gst_inclusive=True), place="29"))
    assert interstate["taxable_value_paise"] == 10_000
    assert interstate["igst_paise"] == 1_800
    assert interstate["total_paise"] == 11_800

    discounted = calculate_invoice_totals(payload(line(), discount=1_000))
    assert discounted["taxable_value_paise"] == 9_000
    assert discounted["gst_paise"] == 1_620
    assert sum(item["discount_paise"] for item in discounted["items"]) == 1_000
    try:
        calculate_invoice_totals(payload(line(), discount=10_001))
    except HTTPException:
        pass
    else:
        raise AssertionError("Discount above subtotal was accepted")


def test_financial_year_and_number_shape():
    assert financial_year("2026-03-31") == "25-26"
    assert financial_year("2026-04-01") == "26-27"
    assert len("INV/26-27/000001") == 16
    config = normalize_numbering_config("credit_note", {"series": "ret", "prefix": "CR/", "suffix": "A", "leading_zeros": 5})
    assert config["series"] == "RET"
    assert format_document_number({**config, "document_kind": "credit_note"}, "26-27", 7) == "CR/26-27/00007A"
    try:
        normalize_numbering_config("invoice", {"prefix": "TOO-LONG", "leading_zeros": 8})
    except HTTPException:
        pass
    else:
        raise AssertionError("An overlong GST document number was accepted")


def test_adjusted_invoice_balances():
    amounts = invoice_amounts({
        "total_paise": 11800, "credit_total_paise": 1800, "debit_total_paise": 500,
        "amount_paid_paise": 11800, "amount_refunded_paise": 1000,
    })
    assert amounts == {
        "adjusted_total_paise": 10500, "net_paid_paise": 10800,
        "balance_paise": 0, "refundable_paise": 300,
    }


def test_idempotency_fingerprint_and_accounting_dates():
    assert request_fingerprint("payment", {"b": 2, "a": 1}) == request_fingerprint("payment", {"a": 1, "b": 2})
    assert request_fingerprint("payment", {"a": 1}) != request_fingerprint("payment", {"a": 2})
    validate_invoice_date("2026-04-01", "2026-04-01", "2026-03-31", "2026-04-01")
    validate_payment_date("2026-04-02", "2026-04-01", "2026-04-03", "2026-03-31")
    for function, args in (
        (validate_invoice_date, ("2026-04-04", "2026-04-03")),
        (validate_invoice_date, ("2026-03-31", "2026-04-03", "2026-03-31")),
        (validate_invoice_date, ("2026-04-01", "2026-04-03", None, "2026-04-02")),
        (validate_payment_date, ("2026-03-31", "2026-04-01", "2026-04-03")),
        (validate_payment_date, ("2026-04-04", "2026-04-01", "2026-04-03")),
    ):
        try:
            function(*args)
        except HTTPException:
            pass
        else:
            raise AssertionError(f"{function.__name__} accepted invalid dates")


def test_quote_revision_claim_has_one_owner():
    class Claims:
        def __init__(self):
            self.rows = {}

        async def update_one(self, query, update, **_kwargs):
            self.rows.setdefault(query["_id"], {"_id": query["_id"], **update["$setOnInsert"]})

        async def find_one(self, query, **_kwargs):
            return self.rows.get(query["_id"])

    claims = Claims()
    namespace["conversion_claims"] = claims

    async def exercise():
        first = await claim_quote_revision("org", "quote", 2, "native", "invoice-1", "actor", object())
        replay = await claim_quote_revision("org", "quote", 2, "native", "invoice-1", "actor", object())
        assert first["resource_id"] == replay["resource_id"] == "invoice-1"
        try:
            await claim_quote_revision("org", "quote", 2, "tally", "job-1", "actor", object())
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("A second provider claimed the same quote revision")

    asyncio.run(exercise())
    assert quote_claim_id("org", "quote", 2) != quote_claim_id("org", "quote", 3)


def test_accounting_org_allowlists():
    with patch.dict(os.environ, {}, clear=True):
        assert not native_invoicing_enabled("org-1")
        assert not tally_enabled("org-1")
    with patch.dict(os.environ, {
        "NATIVE_INVOICING_ORG_IDS": " org-1,org-2 ",
        "TALLY_ORG_IDS": " org-2,org-3 ",
    }, clear=True):
        assert native_invoicing_enabled("org-1")
        assert not native_invoicing_enabled("org-3")
        assert not tally_enabled("org-1")
        assert tally_enabled("org-3")


def test_invoice_provider_respects_independent_allowlists():
    class Collection:
        def __init__(self, row):
            self.row = row

        async def find_one(self, *_args, **_kwargs):
            return self.row

    class Database:
        def __init__(self, configured=None, has_tally=False):
            self.settings = Collection({"invoice_provider": configured} if configured else None)
            self.tally_integrations = Collection({"_id": 1} if has_tally else None)

        def __getitem__(self, _name):
            return self.settings

    async def exercise():
        with patch.dict(os.environ, {"NATIVE_INVOICING_ORG_IDS": "org-1"}, clear=True):
            namespace["db"] = Database(configured="tally", has_tally=True)
            assert await invoice_provider("org-1") == "native"
        with patch.dict(os.environ, {"TALLY_ORG_IDS": "org-1"}, clear=True):
            namespace["db"] = Database()
            assert await invoice_provider("org-1") == "tally"
        with patch.dict(os.environ, {"NATIVE_INVOICING_ORG_IDS": "org-1", "TALLY_ORG_IDS": "org-1"}, clear=True):
            namespace["db"] = Database(configured="native", has_tally=True)
            assert await invoice_provider("org-1") == "native"
            namespace["db"] = Database(has_tally=True)
            assert await invoice_provider("org-1") == "tally"

    asyncio.run(exercise())


def test_einvoice_applicability_fails_closed():
    b2c = {"seller": {"gstin": "27AAPFU0939F1ZV"}, "customer": {"gstin": ""}}
    b2b = {"seller": b2c["seller"], "customer": {"gstin": "29AAACB2894G1ZJ"}}
    for case in (
        ({"status": "unassessed", "gstin": "27AAPFU0939F1ZV"}, b2c),
        ({"status": "not_required", "gstin": "29AAACB2894G1ZJ"}, b2c),
        ({"status": "required", "gstin": "27AAPFU0939F1ZV"}, b2c),
        ({"status": "required", "gstin": "27AAPFU0939F1ZV", "b2c_dynamic_qr_required": True}, b2c),
        ({"status": "required", "gstin": "27AAPFU0939F1ZV"}, b2b),
        ({"status": "not_required", "gstin": "27AAPFU0939F1ZV"}, b2c, {"gstin": "27AAPFU0939F1ZV", "name": "Different seller"}),
    ):
        assessment, invoice, *extra = case
        try:
            validate_einvoice_applicability(assessment, invoice, *extra)
        except HTTPException:
            pass
        else:
            raise AssertionError("E-invoice applicability gate accepted an unsafe invoice")
    validate_einvoice_applicability(
        {"status": "required", "gstin": "27AAPFU0939F1ZV", "b2c_dynamic_qr_required": False}, b2c,
    )
    validate_einvoice_applicability({
        "status": "not_required", "gstin": "27AAPFU0939F1ZV", "not_required_reason": "below_threshold",
    }, b2c)


def test_server_side_feature_and_state_contracts():
    service = Path("services/native_invoice_service.py").read_text()
    routes = Path("api/native_invoice_routes.py").read_text()
    entitlements = Path("services/entitlement_service.py").read_text()
    quote_routes = Path("api/quote_routes.py").read_text()
    flags = Path("services/feature_flags.py").read_text()
    assert '"NATIVE_INVOICING_ORG_IDS"' in flags
    assert '"TALLY_ORG_IDS"' in flags
    assert "NATIVE_INVOICING_ENABLED" not in flags
    assert 'invoice_provider(user.org_id) != "native"' in routes
    assert routes.index('@router.get("/reports")') < routes.index('@router.get("/{invoice_id}")')
    assert routes.index('@router.get("/export.csv")') < routes.index('@router.get("/{invoice_id}")')
    assert '@router.get("/{invoice_id}/audit")' in routes
    assert "session.with_transaction(transaction)" in service
    assert "invoice_conversion_claims" in service
    assert "claim_quote_revision" in service
    assert "request_fingerprint" in service
    assert "last_invoice_date" in service
    assert "_validate_einvoice_applicability" in service
    assert "issuance remains blocked until IRP submission is verified" in service
    assert 'db.command("hello")' in service
    assert "replica-set or mongos transaction support" in service
    assert "Native invoicing requires Cloudinary credentials" in service
    assert "session.with_transaction(transaction)" in Path("services/tally_service.py").read_text()
    assert 'method: Literal["bank_transfer", "upi", "cash", "cheque", "card", "other", "unspecified"]' in routes
    assert routes.count("_require_admin_reverification(user)") == 6
    assert 'user.role not in {"admin", "org:admin"}' in routes
    assert 'currency: Literal["INR"]' in routes
    assert 'supply_category: Literal["domestic"]' in routes
    assert 'extra="forbid"' in routes
    assert "Goods require a 4, 6, or 8 digit HSN code" in routes
    assert "Services require a 6 digit SAC code beginning with 99" in routes
    assert '@router.post("/adjustments/{adjustment_id}/pdf/render")' in routes
    assert '@router.get("/adjustments/{adjustment_id}/pdf-link")' in routes
    assert '@router.post("/{invoice_id}/refunds/{refund_id}/reverse")' in routes
    assert '@router.post("/issue-now", status_code=201)' in routes
    assert '@router.put("/{invoice_id}/issue-now")' in routes
    assert "create_and_issue_invoice" in service and "update_and_issue_invoice" in service
    adjustment_service = Path("services/native_invoice_adjustment_service.py").read_text()
    assert "render_native_adjustment_pdf" in adjustment_service
    assert "native_invoice_pdf_download_url" in adjustment_service
    assert '"source_invoice_date": source.get("invoice_date")' in adjustment_service
    assert ".to_list(length=1000)" not in adjustment_service
    assert "artifact_match" in adjustment_service
    assert "async def reverse_refund(" in adjustment_service
    assert '"refund_reversed"' in adjustment_service
    assert "Reverse the related refunds before reversing this payment" in service
    assert "artifact_match" in service
    assert "Add an authorised signature before issuing this invoice" in service
    assert "EINVOICE_EXEMPTION_DECLARATION" in service
    assert "not_required_reason" in quote_routes
    assert "already has a Tally invoice" in service
    assert "already has a native invoice" in Path("services/tally_service.py").read_text()
    assert '"lifecycle": "issued"' in service
    assert '"pdf_state": "failed"' in service
    assert '"NATIVE_INVOICING"' in entitlements
    assert "expected_revision" in quote_routes
    assert "Issued invoices are immutable" in service
    adjustment_service = Path("services/native_invoice_adjustment_service.py").read_text()
    assert "credit_note" in adjustment_service and "debit_note" in adjustment_service
    assert "session.with_transaction(transaction)" in adjustment_service
    assert '"refund_recorded"' in adjustment_service
    assert '"/{invoice_id}/duplicate"' in routes
    assert '"/{invoice_id}/archive"' in routes
    assert '"/{invoice_id}/close"' in routes
    assert '"/adjustments/{adjustment_id}/void"' in routes
    assert 'payload.model_dump(mode="json", exclude_unset=True)' in routes
    assert 'if "signature_image" in payload and payload["signature_image"] is not None' in service


if __name__ == "__main__":
    test_integer_paise_gst_math()
    test_financial_year_and_number_shape()
    test_adjusted_invoice_balances()
    test_idempotency_fingerprint_and_accounting_dates()
    test_quote_revision_claim_has_one_owner()
    test_accounting_org_allowlists()
    test_invoice_provider_respects_independent_allowlists()
    test_einvoice_applicability_fails_closed()
    test_server_side_feature_and_state_contracts()
    print("Native invoice backend contract checks passed.")
