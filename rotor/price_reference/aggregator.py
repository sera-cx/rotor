"""Freshness-checking wrapper around one configured FX rate source."""

from __future__ import annotations

import time
from decimal import Decimal

from rotor.price_reference.models import ReferencePrice
from rotor.price_reference.sources import RateSource


class ReferenceUnavailable(RuntimeError):
    """Raised when the configured reference source cannot be used."""


class ReferenceTooStale(ReferenceUnavailable):
    """Raised when an observation is too old to quote on, even carried forward."""


class ReferencePriceOracle:
    """Fetch one quote-per-base reference and enforce its freshness policy."""

    def __init__(
        self,
        source: RateSource,
        *,
        max_age_s: float | None = None,
        carry_stale: bool = True,
        max_carry_age_s: float | None = None,
    ) -> None:
        """Store the source adapter and its freshness and carry-forward limits."""
        # Source adapters are injected to keep HTTP/provider behavior testable.
        self.source = source
        # Freshness is enforced centrally so runners do not quote stale mids.
        # When no explicit override is given, each source carries its own window
        # (intraday providers reject after minutes; daily references tolerate
        # longer), falling back to 10 minutes for sources that declare none.
        if max_age_s is not None:
            self.max_age_s = float(max_age_s)
        else:
            self.max_age_s = float(getattr(source, "default_max_age_s", 600.0))
        # Past that window a daily reference may still be quoted on, carried
        # forward across a weekend or holiday when its source publishes nothing,
        # until the source's carry limit. A source that declares no limit
        # (intraday Wise) never carries, and `carry_stale=False` restores strict
        # rejection for every source.
        self.carry_stale = carry_stale
        if max_carry_age_s is not None:
            self.max_carry_age_s: float | None = float(max_carry_age_s)
        else:
            declared = getattr(source, "default_max_carry_age_s", None)
            self.max_carry_age_s = None if declared is None else float(declared)

    def get(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> ReferencePrice:
        """Return the quote-per-base rate for an ISO pair.

        `amount` is the base-currency size being worked; it is forwarded to the
        source so amount-aware providers (Wise) can price the exact size.
        """
        # Normalize the pair label for consistent operator-facing errors.
        pair = f"{base.upper()}/{quote.upper()}"
        try:
            # Delegate provider-specific HTTP/parsing to the configured source.
            obs = self.source.quote(base, quote, amount=amount)
        except Exception as exc:
            # Wrap provider failures in one domain-level exception type.
            raise ReferenceUnavailable(
                f"{pair}: {self.source.name} failed ({type(exc).__name__}: {exc})"
            ) from exc

        # Compare wall-clock time to provider observation time.
        age_s = time.time() - obs.ts
        stale = age_s > self.max_age_s
        if stale and not self._can_carry(age_s):
            limit = (
                f" (past the {self.max_carry_age_s:.0f}s carry limit)"
                if self.carry_stale and self.max_carry_age_s is not None
                else ""
            )
            raise ReferenceTooStale(
                f"{pair}: {self.source.name} quote is stale age={age_s:.1f}s{limit}"
            )
        # Return a smaller quote-safe shape that drops provider-specific raw data,
        # marking a carried-forward rate so the runner can warn about it.
        return ReferencePrice(
            pair=pair,
            rate=obs.rate,
            ts=obs.ts,
            source=obs.source,
            age_s=age_s,
            stale=stale,
        )

    def _can_carry(self, age_s: float) -> bool:
        """Return whether a stale observation of this age may still be quoted on."""
        return (
            self.carry_stale
            and self.max_carry_age_s is not None
            and age_s <= self.max_carry_age_s
        )
