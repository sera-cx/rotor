"""Opt-in live tests for the FX reference sources.

These hit real external endpoints, so they are skipped unless
``ROTOR_RUN_LIVE_PRICES=1``. Two groups:

  * Public (keyless): ECB (XML), Bank Negara Malaysia, data.gov.sg (MAS).
  * Credentialed: ``fed`` (FRED API — needs ``FRED_API_KEY``) and ``wise``
    (needs ``WISE_API_TOKEN``); each is skipped unless its key is configured.

Run them with::

    ROTOR_RUN_LIVE_PRICES=1 poetry run python -m pytest -q tests/live
    # or: make live-prices

The sanity bands below are wide enough to survive years of normal FX drift but
narrow enough that a flipped quote orientation (e.g. returning USD/SGD where
SGD/USD is expected) lands outside the band and fails — a cheap guard for the
cross-rate math in each adapter.
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx
import pytest

from rotor.config import get_config
from rotor.price_reference import ReferencePriceOracle, ReferenceUnavailable
from rotor.price_reference.config import load_price_reference_source
from rotor.price_reference.models import PriceObservation


def _flag(name: str) -> bool:
    return str(get_config(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _quote_or_skip(name: str, base: str, quote: str) -> PriceObservation:
    """Fetch a live quote, skipping (not failing) on transient network errors.

    A timeout/connection error is environmental (e.g. a slow FRED endpoint), not
    a code bug — these tests exist to validate parsing and cross-rate
    orientation, so only a reachable-but-wrong response should fail.
    """
    try:
        return load_price_reference_source(name).quote(base, quote)
    except httpx.TransportError as exc:
        pytest.skip(f"{name} {base}/{quote} unreachable: {type(exc).__name__}: {exc}")


pytestmark = [
    pytest.mark.live_prices,
    pytest.mark.skipif(
        not _flag("ROTOR_RUN_LIVE_PRICES"),
        reason="set ROTOR_RUN_LIVE_PRICES=1 to run live public FX source tests",
    ),
]


def _assert_sane(name, base, quote, low, high, obs):
    assert isinstance(obs, PriceObservation)
    assert obs.source == name
    assert obs.pair == f"{base}/{quote}"
    assert isinstance(obs.rate, Decimal)
    assert low < obs.rate < high, (
        f"{name} {base}/{quote} rate {obs.rate} outside sane band "
        f"[{low}, {high}] — possible orientation/cross-rate bug"
    )
    # ts must be a real (published) timestamp, never in the future.
    assert isinstance(obs.ts, float)
    assert 0 < obs.ts <= time.time() + 86400


# (source, base, quote, low, high). Bands are quote-per-base.
#   SGD/USD ~0.74 (USD always stronger; a flip to ~1.35 exceeds 1.1)
#   USD/SGD ~1.35 (a flip to ~0.74 falls below 0.9)
#   EUR/USD ~1.07 (near-parity; band only checks plausibility, not orientation)
PUBLIC_CASES = [
    ("ecb", "SGD", "USD", Decimal("0.5"), Decimal("1.1")),
    ("ecb", "EUR", "USD", Decimal("0.7"), Decimal("1.7")),
    ("bnm", "USD", "SGD", Decimal("0.9"), Decimal("2.0")),
    ("mas", "USD", "SGD", Decimal("0.9"), Decimal("2.0")),
    # Both orientations, because a crossed source is where a flipped rate hides.
    ("coingecko", "SGD", "USD", Decimal("0.5"), Decimal("1.1")),
    ("coingecko", "USD", "SGD", Decimal("0.9"), Decimal("2.0")),
]

# Sources that need a credential. fed uses the FRED API (the legacy fredgraph.csv
# scrape endpoint is bot-walled); wise needs its API token. Skipped unless the
# corresponding key is configured.
CREDENTIALED_CASES = [
    ("fed", "FRED_API_KEY", "SGD", "USD", Decimal("0.5"), Decimal("1.1")),
    ("fed", "FRED_API_KEY", "EUR", "USD", Decimal("0.7"), Decimal("1.7")),
    ("wise", "WISE_API_TOKEN", "SGD", "USD", Decimal("0.5"), Decimal("1.1")),
]


@pytest.mark.parametrize(
    ("name", "base", "quote", "low", "high"),
    PUBLIC_CASES,
    ids=[f"{name}-{base}{quote}" for name, base, quote, _lo, _hi in PUBLIC_CASES],
)
def test_public_source_returns_sane_live_quote(name, base, quote, low, high):
    _assert_sane(name, base, quote, low, high, _quote_or_skip(name, base, quote))


@pytest.mark.parametrize(
    ("name", "env_var", "base", "quote", "low", "high"),
    CREDENTIALED_CASES,
    ids=[f"{name}-{base}{quote}" for name, _e, base, quote, _lo, _hi in CREDENTIALED_CASES],
)
def test_credentialed_source_returns_sane_live_quote(name, env_var, base, quote, low, high):
    if not str(get_config(env_var, "") or "").strip():
        pytest.skip(f"set {env_var} to live-test the {name} source")
    _assert_sane(name, base, quote, low, high, _quote_or_skip(name, base, quote))


def test_ecb_forward_and_inverse_are_consistent():
    # quote-per-base and base-per-quote from the same feed must multiply to ~1.
    forward = _quote_or_skip("ecb", "SGD", "USD").rate
    inverse = _quote_or_skip("ecb", "USD", "SGD").rate

    assert abs(forward * inverse - Decimal("1")) < Decimal("0.001")


def test_oracle_accepts_live_daily_quote_with_generous_window():
    # End-to-end fetch -> parse -> freshness path. A wide window keeps this
    # deterministic across weekends/holidays (when daily feeds are legitimately
    # 1-3 days old and the default 48h window may reject them by design).
    oracle = ReferencePriceOracle(
        load_price_reference_source("ecb"), max_age_s=30 * 86400
    )
    try:
        ref = oracle.get("SGD", "USD")
    except ReferenceUnavailable as exc:
        pytest.skip(f"ecb oracle unreachable: {exc}")

    assert ref.pair == "SGD/USD"
    assert ref.source == "ecb"
    assert ref.rate > 0
