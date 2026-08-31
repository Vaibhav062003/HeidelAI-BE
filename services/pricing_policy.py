"""Pure, deterministic pricing decisions.

This module deliberately has no database, framework, or provider imports. It is
the shared contract between catalogue ingestion and channel-specific senders.
Legacy ``product.price`` is a display field and is never accepted as an
approved floor by :meth:`PriceSnapshot.from_product`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, Mapping, TypeAlias


MoneyInput: TypeAlias = Decimal | int | float | str
Actor: TypeAlias = Literal["ai", "human"]
PricingAction: TypeAlias = Literal["auto_send", "review_required", "warning_required"]

_CENT = Decimal("0.01")
_ONE_HUNDRED = Decimal("100")
_INR_TEXT = re.compile(r"(?i)(?:\binr\b|\brs\.?)|₹|,|\s")


def normalize_currency(value: object) -> str:
    """Return an ISO-like currency code; pricing V1 accepts INR only."""
    currency = str(value or "INR").strip().upper()
    if currency in {"₹", "RS", "RS.", "RUPEE", "RUPEES"}:
        currency = "INR"
    if currency != "INR":
        raise ValueError("currency must be INR")
    return currency


def normalize_inr(value: MoneyInput, *, field: str = "amount", allow_zero: bool = False) -> Decimal:
    """Parse an INR amount exactly and quantize it to paise.

    Floats are converted through ``str`` so binary floating-point noise is not
    incorporated into a business decision.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be an INR amount")
    cleaned = _INR_TEXT.sub("", str(value))
    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be an INR amount") from exc
    if not amount.is_finite():
        raise ValueError(f"{field} must be finite")
    if amount < 0 or (amount == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{field} must be {qualifier}")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def normalize_gst_inclusive(value: object, *, field: str = "gst_inclusive") -> bool | None:
    """Normalize an explicit GST basis without guessing an absent value."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold().replace("_", " ").replace("-", " ")
    if normalized in {"yes", "true", "1", "included", "inclusive", "gst included", "incl gst"}:
        return True
    if normalized in {"no", "false", "0", "extra", "exclusive", "gst extra", "excluding gst"}:
        return False
    raise ValueError(f"{field} must explicitly say whether GST is included")


def normalize_gst_rate(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("gst_rate must be a percentage")
    try:
        rate = Decimal(str(value).replace("%", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("gst_rate must be a percentage") from exc
    if not rate.is_finite() or rate < 0 or rate > 100:
        raise ValueError("gst_rate must be between 0 and 100")
    return rate.quantize(_CENT, rounding=ROUND_HALF_UP)


def gst_inclusive_inr(amount: MoneyInput, *, gst_inclusive: bool, gst_rate: object) -> Decimal:
    """Normalize an amount to its GST-inclusive INR value."""
    normalized = normalize_inr(amount)
    rate = normalize_gst_rate(gst_rate)
    if gst_inclusive:
        return normalized
    return (normalized * (Decimal("1") + rate / _ONE_HUNDRED)).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )


def _optional_inr(value: MoneyInput | None, field: str) -> Decimal | None:
    return None if value is None or str(value).strip() == "" else normalize_inr(value, field=field)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # PyMongo commonly returns BSON datetimes as naive UTC unless
        # ``tz_aware`` is enabled on the client.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    sku: str
    approved_floor_price: MoneyInput | None
    target_selling_price: MoneyInput | None
    mrp: MoneyInput | None = None
    cost_price: MoneyInput | None = None
    currency: str = "INR"
    gst_rate: object = Decimal("18")
    selling_gst_inclusive: object = None
    cost_gst_inclusive: object = None
    stock_available: int | None = None
    price_source_sheet_id: str | None = None
    price_verified_at: datetime | None = None
    price_version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sku", str(self.sku or "").strip())
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "gst_rate", normalize_gst_rate(self.gst_rate))
        object.__setattr__(
            self, "selling_gst_inclusive",
            normalize_gst_inclusive(self.selling_gst_inclusive, field="selling_gst_inclusive"),
        )
        object.__setattr__(
            self, "cost_gst_inclusive",
            normalize_gst_inclusive(self.cost_gst_inclusive, field="cost_gst_inclusive"),
        )
        for field in ("approved_floor_price", "target_selling_price", "mrp", "cost_price"):
            object.__setattr__(self, field, _optional_inr(getattr(self, field), field))
        object.__setattr__(self, "price_source_sheet_id", str(self.price_source_sheet_id or "").strip() or None)
        object.__setattr__(self, "price_version", str(self.price_version or "").strip() or None)
        object.__setattr__(self, "price_verified_at", _as_utc(self.price_verified_at))
        if isinstance(self.stock_available, bool):
            raise ValueError("stock_available must be a whole number")
        if self.stock_available is not None:
            try:
                stock = int(self.stock_available)
            except (TypeError, ValueError) as exc:
                raise ValueError("stock_available must be a whole number") from exc
            if stock != self.stock_available or stock < 0:
                raise ValueError("stock_available must be a non-negative whole number")
            object.__setattr__(self, "stock_available", stock)

    @classmethod
    def from_product(cls, product: Mapping[str, object]) -> "PriceSnapshot":
        """Build only from canonical, provenance-bearing safety fields."""
        stock = product.get("stock_available", product.get("stock", product.get("quantity")))
        return cls(
            sku=str(
                product.get("unique_identifier") or product.get("model_no")
                or product.get("sku") or product.get("sku_full") or ""
            ),
            approved_floor_price=product.get("approved_floor_price"),
            target_selling_price=product.get("target_selling_price"),
            mrp=product.get("mrp"),
            cost_price=product.get("cost_price"),
            currency=str(product.get("currency") or "INR"),
            gst_rate=product.get("gst_rate", 18),
            selling_gst_inclusive=product.get("selling_gst_inclusive"),
            cost_gst_inclusive=product.get("cost_gst_inclusive"),
            # Preserve the original numeric value until __post_init__ verifies
            # it is a whole number; pre-casting would silently turn 1.5 into 1.
            stock_available=stock,  # type: ignore[arg-type]
            price_source_sheet_id=product.get("price_source_sheet_id"),
            price_verified_at=product.get("price_verified_at"),
            price_version=product.get("price_version"),
        )

    def selling_inclusive(self, amount: Decimal) -> Decimal:
        if self.selling_gst_inclusive is None:
            raise ValueError("selling GST basis is unverified")
        return gst_inclusive_inr(
            amount, gst_inclusive=self.selling_gst_inclusive, gst_rate=self.gst_rate
        )

    def invariant_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.sku:
            errors.append("missing_sku")
        if self.approved_floor_price is None:
            errors.append("missing_approved_floor")
        if self.target_selling_price is None:
            errors.append("missing_target_price")
        if self.selling_gst_inclusive is None:
            errors.append("missing_selling_gst_basis")
        if self.cost_price is not None and self.cost_gst_inclusive is None:
            errors.append("missing_cost_gst_basis")
        if not self.price_source_sheet_id:
            errors.append("missing_price_source")
        if not self.price_version:
            errors.append("missing_price_version")
        if self.price_verified_at is None:
            errors.append("missing_price_verification")

        if self.selling_gst_inclusive is not None:
            floor = self.selling_inclusive(self.approved_floor_price) if self.approved_floor_price else None
            target = self.selling_inclusive(self.target_selling_price) if self.target_selling_price else None
            if floor is not None and target is not None and target < floor:
                errors.append("target_below_floor")
            mrp = self.selling_inclusive(self.mrp) if self.mrp is not None else None
            if mrp is not None and floor is not None and mrp < floor:
                errors.append("mrp_below_floor")
            if mrp is not None and target is not None and mrp < target:
                errors.append("mrp_below_target")
            if self.cost_price is not None and self.cost_gst_inclusive is not None and floor is not None:
                cost = gst_inclusive_inr(
                    self.cost_price,
                    gst_inclusive=self.cost_gst_inclusive,
                    gst_rate=self.gst_rate,
                )
                if cost > floor:
                    errors.append("cost_above_floor")
        return tuple(dict.fromkeys(errors))

    def validate_invariants(self) -> tuple[str, ...]:
        """Return stable invariant codes; an empty tuple is fully verified."""
        return self.invariant_errors()

    def assert_valid(self) -> None:
        errors = self.invariant_errors()
        if errors:
            raise ValueError("invalid price snapshot: " + ", ".join(errors))

    def is_fresh(self, *, now: datetime, max_age: timedelta) -> bool:
        if self.price_verified_at is None or max_age <= timedelta(0):
            return False
        current = _as_utc(now)
        assert current is not None
        age = current - self.price_verified_at
        return -timedelta(minutes=5) <= age <= max_age


@dataclass(frozen=True, slots=True)
class QuoteCandidate:
    amount: MoneyInput
    sku: str
    currency: str = "INR"
    gst_inclusive: object = None
    gst_rate: object = None
    quantity: int = 1
    structured: bool = False
    exact_product_match: bool = False
    actor: Actor = "ai"

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", normalize_inr(self.amount))
        object.__setattr__(self, "sku", str(self.sku or "").strip())
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(self, "gst_inclusive", normalize_gst_inclusive(self.gst_inclusive))
        object.__setattr__(
            self, "gst_rate",
            None if self.gst_rate is None or str(self.gst_rate).strip() == "" else normalize_gst_rate(self.gst_rate),
        )
        if self.actor not in {"ai", "human"}:
            raise ValueError("actor must be ai or human")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity < 1:
            raise ValueError("quantity must be a positive whole number")


@dataclass(frozen=True, slots=True)
class PricingDecisionV1:
    action: PricingAction
    reasons: tuple[str, ...]
    quote_inr_gst_inclusive: Decimal | None
    floor_inr_gst_inclusive: Decimal | None
    target_inr_gst_inclusive: Decimal | None
    price_version: str | None
    schema_version: str = "pricing_decision.v1"


def decide_pricing_v1(
    snapshot: PriceSnapshot,
    quote: QuoteCandidate,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(minutes=60),
) -> PricingDecisionV1:
    """Make one auditable pricing decision from immutable inputs."""
    current = _as_utc(now or datetime.now(timezone.utc))
    assert current is not None
    reasons = list(snapshot.invariant_errors())
    if not snapshot.is_fresh(now=current, max_age=max_age):
        reasons.append("stale_or_future_price")
    if quote.sku.casefold() != snapshot.sku.casefold() or not quote.exact_product_match:
        reasons.append("product_not_exact")
    if quote.gst_inclusive is None:
        reasons.append("missing_quote_gst_basis")
    if quote.gst_rate is None:
        reasons.append("missing_quote_gst_rate")
    elif quote.gst_rate != snapshot.gst_rate:
        reasons.append("quote_gst_rate_mismatch")

    quote_inclusive = None
    floor_inclusive = None
    target_inclusive = None
    if quote.gst_inclusive is not None and quote.gst_rate is not None:
        quote_inclusive = gst_inclusive_inr(
            quote.amount, gst_inclusive=quote.gst_inclusive, gst_rate=quote.gst_rate
        )
    if snapshot.selling_gst_inclusive is not None:
        if snapshot.approved_floor_price is not None:
            floor_inclusive = snapshot.selling_inclusive(snapshot.approved_floor_price)
        if snapshot.target_selling_price is not None:
            target_inclusive = snapshot.selling_inclusive(snapshot.target_selling_price)

    if quote_inclusive is not None and floor_inclusive is not None and quote_inclusive < floor_inclusive:
        reasons.append("quote_below_floor")
    if (
        quote_inclusive is not None
        and snapshot.mrp is not None
        and snapshot.selling_gst_inclusive is not None
    ):
        mrp_inclusive = snapshot.selling_inclusive(snapshot.mrp)
        if quote_inclusive > mrp_inclusive:
            reasons.append("quote_above_mrp")

    unverified = bool(reasons)
    if quote.actor == "human":
        action: PricingAction = "warning_required" if unverified else "auto_send"
        if not reasons:
            reasons.append("verified_human_quote")
    else:
        if not quote.structured:
            reasons.append("quote_not_structured")
        if quote.quantity != 1:
            reasons.append("quantity_not_single")
        if snapshot.stock_available is None:
            reasons.append("stock_unverified")
        elif snapshot.stock_available < 1:
            reasons.append("out_of_stock")
        if quote_inclusive is None or target_inclusive is None or quote_inclusive != target_inclusive:
            reasons.append("quote_not_exact_target")
        action = "review_required" if reasons else "auto_send"
        if not reasons:
            reasons.append("verified_exact_target")

    return PricingDecisionV1(
        action=action,
        reasons=tuple(dict.fromkeys(reasons)),
        quote_inr_gst_inclusive=quote_inclusive,
        floor_inr_gst_inclusive=floor_inclusive,
        target_inr_gst_inclusive=target_inclusive,
        price_version=snapshot.price_version,
    )


# Short alias for callers that do not need the schema version in the function name.
decide_pricing = decide_pricing_v1
