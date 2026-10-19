"""Tests for freshness and stale carry-forward in the reference-price oracle."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import pytest

from rotor.price_reference.aggregator import (
    ReferencePriceOracle,
    ReferenceTooStale,
    ReferenceUnavailable,
)
from rotor.price_reference.models import PriceObservation

DAY = 24 * 3600.0


@dataclass
class StaticSource:
    name: str = "wise"
    rate: Decimal = Decimal("0.7400")
    age_s: float = 0.0

    def quote(self, base: str, quote: str, *, amount=None) -> PriceObservation:
        return PriceObservation(
            source=self.name,
            pair=f"{base}/{quote}",
            rate=self.rate,
            ts=time.time() - self.age_s,
        )


@dataclass
class DailySource(StaticSource):
    """A daily official reference: fresh for 48h, carried forward for a week."""

    name: str = "bnm"
    default_max_age_s: float = 2 * DAY
    default_max_carry_age_s: float | None = 7 * DAY


def test_reference_oracle_returns_fresh_wise_quote():
    ref = ReferencePriceOracle(StaticSource(rate=Decimal("0.7415"))).get("SGD", "USD")

    assert ref.pair == "SGD/USD"
    assert ref.rate == Decimal("0.7415")
    assert ref.source == "wise"
    assert ref.stale is False


def test_reference_oracle_rejects_stale_quote():
    oracle = ReferencePriceOracle(StaticSource(age_s=999), max_age_s=10)
    with pytest.raises(ReferenceUnavailable, match="stale"):
        oracle.get("SGD", "USD")


def test_stale_intraday_quote_is_never_carried():
    oracle = ReferencePriceOracle(StaticSource(age_s=999), max_age_s=10)
    with pytest.raises(ReferenceTooStale) as ei:
        oracle.get("SGD", "USD")

    assert "carry limit" not in str(ei.value)


def test_weekend_daily_rate_is_carried_forward():
    # Friday's BNM rate read on Monday morning, before BNM publishes again.
    ref = ReferencePriceOracle(DailySource(rate=Decimal("0.2120"), age_s=3 * DAY)).get(
        "MYR", "USD"
    )

    assert ref.stale is True
    assert ref.rate == Decimal("0.2120")
    assert ref.age_s == pytest.approx(3 * DAY, abs=5)


def test_daily_rate_inside_its_window_is_not_stale():
    ref = ReferencePriceOracle(DailySource(age_s=DAY)).get("MYR", "USD")

    assert ref.stale is False


def test_carried_rate_past_its_carry_limit_is_rejected():
    oracle = ReferencePriceOracle(DailySource(age_s=8 * DAY))
    with pytest.raises(ReferenceTooStale, match="carry limit"):
        oracle.get("MYR", "USD")


def test_carrying_can_be_switched_off():
    oracle = ReferencePriceOracle(DailySource(age_s=3 * DAY), carry_stale=False)
    with pytest.raises(ReferenceTooStale, match="stale"):
        oracle.get("MYR", "USD")


def test_explicit_carry_limit_overrides_the_source():
    oracle = ReferencePriceOracle(DailySource(age_s=3 * DAY), max_carry_age_s=2.5 * DAY)
    with pytest.raises(ReferenceTooStale):
        oracle.get("MYR", "USD")


def test_too_stale_is_a_kind_of_reference_unavailable():
    assert issubclass(ReferenceTooStale, ReferenceUnavailable)
