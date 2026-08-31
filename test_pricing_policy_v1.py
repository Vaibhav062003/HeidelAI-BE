import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.pricing_policy import (
    PriceSnapshot,
    QuoteCandidate,
    decide_pricing_v1,
    gst_inclusive_inr,
    normalize_inr,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def snapshot(**overrides):
    values = {
        "sku": "RTX-5060",
        "cost_price": "₹50,000",
        "approved_floor_price": "59,000",
        "target_selling_price": "61,000",
        "mrp": "70,000",
        "currency": "INR",
        "gst_rate": "18%",
        "selling_gst_inclusive": True,
        "cost_gst_inclusive": False,
        "stock_available": 2,
        "price_source_sheet_id": "sheet-primary",
        "price_verified_at": NOW - timedelta(minutes=5),
        "price_version": "version-1",
    }
    values.update(overrides)
    return PriceSnapshot(**values)


def candidate(**overrides):
    values = {
        "amount": "Rs. 61,000",
        "sku": "RTX-5060",
        "currency": "INR",
        "gst_inclusive": True,
        "gst_rate": 18,
        "quantity": 1,
        "structured": True,
        "exact_product_match": True,
        "actor": "ai",
    }
    values.update(overrides)
    return QuoteCandidate(**values)


class PricingPolicyV1Test(unittest.TestCase):
    def test_inr_and_gst_normalization_are_exact(self):
        self.assertEqual(normalize_inr("₹ 12,345.678"), Decimal("12345.68"))
        self.assertEqual(
            gst_inclusive_inr("100", gst_inclusive=False, gst_rate="18%"),
            Decimal("118.00"),
        )
        with self.assertRaises(ValueError):
            normalize_inr(float("nan"))
        with self.assertRaises(ValueError):
            QuoteCandidate(amount=100, sku="x", currency="USD")

    def test_auto_send_requires_exact_structured_fresh_single_unit_with_stock(self):
        decision = decide_pricing_v1(snapshot(), candidate(), now=NOW)
        self.assertEqual(decision.action, "auto_send")
        self.assertEqual(decision.reasons, ("verified_exact_target",))
        self.assertEqual(decision.target_inr_gst_inclusive, Decimal("61000.00"))

        risky = (
            candidate(structured=False),
            candidate(exact_product_match=False),
            candidate(quantity=2),
            candidate(amount=60_000),
        )
        for quote in risky:
            with self.subTest(quote=quote):
                self.assertEqual(
                    decide_pricing_v1(snapshot(), quote, now=NOW).action,
                    "review_required",
                )
        self.assertEqual(
            decide_pricing_v1(snapshot(stock_available=0), candidate(), now=NOW).action,
            "review_required",
        )

    def test_snapshot_invariants_are_reported_not_guessed(self):
        broken = snapshot(
            approved_floor_price=62_000,
            target_selling_price=61_000,
            mrp=60_000,
            price_source_sheet_id=None,
        )
        self.assertEqual(
            broken.invariant_errors(),
            ("missing_price_source", "target_below_floor", "mrp_below_floor", "mrp_below_target"),
        )
        decision = decide_pricing_v1(broken, candidate(), now=NOW)
        self.assertEqual(decision.action, "review_required")

    def test_human_below_floor_or_unverified_requires_warning(self):
        below = candidate(actor="human", amount=58_999)
        decision = decide_pricing_v1(snapshot(), below, now=NOW)
        self.assertEqual(decision.action, "warning_required")
        self.assertIn("quote_below_floor", decision.reasons)

        unverified = snapshot(price_verified_at=None, price_version=None)
        decision = decide_pricing_v1(unverified, candidate(actor="human"), now=NOW)
        self.assertEqual(decision.action, "warning_required")
        self.assertIn("missing_price_verification", decision.reasons)

    def test_quote_uses_declared_gst_rate_and_warns_on_catalog_mismatch(self):
        price = snapshot(
            cost_price=None,
            cost_gst_inclusive=None,
            approved_floor_price=100,
            target_selling_price=120,
        )
        decision = decide_pricing_v1(
            price,
            candidate(actor="human", amount=90, gst_inclusive=False, gst_rate=5),
            now=NOW,
        )
        self.assertEqual(decision.quote_inr_gst_inclusive, Decimal("94.50"))
        self.assertIn("quote_gst_rate_mismatch", decision.reasons)
        self.assertIn("quote_below_floor", decision.reasons)

    def test_stock_only_update_does_not_refresh_price_evidence(self):
        product = {
            "unique_identifier": "RTX-5060",
            "approved_floor_price": 59_000,
            "target_selling_price": 61_000,
            "mrp": 70_000,
            "cost_price": 50_000,
            "currency": "INR",
            "gst_rate": 18,
            "selling_gst_inclusive": True,
            "cost_gst_inclusive": False,
            "stock": 4,
            "price_source_sheet_id": "sheet-primary",
            "price_verified_at": NOW - timedelta(hours=2),
            "price_version": "old-price",
            # A stock write may update this generic timestamp; it must not make
            # the price snapshot fresh.
            "updated_at": NOW,
        }
        stale = PriceSnapshot.from_product(product)
        decision = decide_pricing_v1(stale, candidate(), now=NOW, max_age=timedelta(hours=1))
        self.assertEqual(decision.action, "review_required")
        self.assertIn("stale_or_future_price", decision.reasons)

    def test_mrp_uses_same_gst_basis_as_exclusive_selling_prices(self):
        exclusive = snapshot(
            cost_price=None,
            cost_gst_inclusive=None,
            approved_floor_price=100,
            target_selling_price=120,
            mrp=130,
            selling_gst_inclusive=False,
        )
        at_target = candidate(amount=120, gst_inclusive=False)
        decision = decide_pricing_v1(exclusive, at_target, now=NOW)
        self.assertEqual(decision.action, "auto_send")
        self.assertEqual(decision.target_inr_gst_inclusive, Decimal("141.60"))

        above_mrp = candidate(amount=131, gst_inclusive=False)
        decision = decide_pricing_v1(exclusive, above_mrp, now=NOW)
        self.assertEqual(decision.action, "review_required")
        self.assertIn("quote_above_mrp", decision.reasons)

    def test_naive_mongo_verification_timestamp_is_treated_as_utc(self):
        mongo_timestamp = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
        normalized = snapshot(price_verified_at=mongo_timestamp)
        self.assertEqual(normalized.price_verified_at.tzinfo, timezone.utc)
        self.assertEqual(decide_pricing_v1(normalized, candidate(), now=NOW).action, "auto_send")

    def test_fractional_mongo_stock_is_rejected_instead_of_truncated(self):
        with self.assertRaisesRegex(ValueError, "whole number"):
            PriceSnapshot.from_product({
                "unique_identifier": "RTX-5060",
                "stock": 1.5,
            })

    def test_missing_selling_gst_basis_fails_closed_without_crashing(self):
        unverified = snapshot(selling_gst_inclusive=None)
        decision = decide_pricing_v1(unverified, candidate(), now=NOW)
        self.assertEqual(decision.action, "review_required")
        self.assertIn("missing_selling_gst_basis", decision.reasons)


if __name__ == "__main__":
    unittest.main()
