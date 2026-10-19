"""Shared price-reference data shapes."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class PriceObservation:
    """One rate observation from one external source.

    `rate` is always quote-per-base for an ISO pair such as `SGD/USD`.
    The market-maker maps Sera token markets (`XSGD/USDC`) into those ISO
    pairs before asking the reference oracle for a price.
    """

    # Provider identifier used in logs/errors to attribute the observation.
    source: str
    # ISO pair in BASE/QUOTE form, e.g. SGD/USD.
    pair: str
    # Quote-per-base exchange rate represented as Decimal for exact arithmetic.
    rate: Decimal
    # Observation timestamp as POSIX seconds for freshness checks.
    ts: float
    # Raw provider payload is retained for debugging but hidden from repr noise.
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class ReferencePrice:
    """Quote-safe mid price from the configured source."""

    # ISO pair used by the market maker.
    pair: str
    # Quote-per-base mid rate: fresh, or carried forward when `stale`.
    rate: Decimal
    # POSIX timestamp copied from the accepted observation.
    ts: float
    # Source name copied through for logs/audits.
    source: str
    # Seconds between the source's published timestamp and when it was read.
    age_s: float = 0.0
    # True when the rate is past its source's freshness window but still inside
    # the carry limit, e.g. a daily source that does not publish on weekends.
    stale: bool = False
