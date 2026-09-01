"""Additional edge coverage for price-reference sources and config."""

from __future__ import annotations

import time
from decimal import Decimal

import httpx
import pytest

from rotor.price_reference.aggregator import ReferencePriceOracle, ReferenceUnavailable
from rotor.price_reference.config import load_price_reference_source, load_token_to_iso
from rotor.price_reference.models import PriceObservation
from rotor.price_reference.sources import EcbRateSource, MasRateSource, WiseRateSource
from rotor.price_reference.symbols import market_to_iso_pair, parse_market_symbol


def test_parse_market_symbol_rejects_bad_shapes():
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        parse_market_symbol("XSGD")
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        parse_market_symbol("XSGD/USDC/EURC")


def test_market_to_iso_pair_uses_configured_overrides():
    assert market_to_iso_pair("xphp/usdc", {"XPHP": "PHP"}) == ("PHP", "USD")


def test_market_to_iso_pair_rejects_unmapped_token():
    with pytest.raises(ValueError, match="no ISO mapping"):
        market_to_iso_pair("NOPE/USDC")


def test_load_token_to_iso_uppercases_json_overrides(monkeypatch):
    monkeypatch.setenv("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", '{"xphp": "php"}')

    assert load_token_to_iso() == {"XPHP": "PHP"}


def test_load_price_reference_source_rejects_unknown_source():
    with pytest.raises(ValueError, match="wise, ecb, fed, bnm, mas"):
        load_price_reference_source("manual")


@pytest.mark.parametrize("name", ["wise", "ecb", "fed", "bnm", "mas", "coingecko"])
def test_load_price_reference_source_accepts_supported_sources(name):
    assert load_price_reference_source(name).name in {name, "fed"}


def test_wise_source_rejects_response_without_enabled_options(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        # All options disabled (no funding method available to the account).
        return httpx.Response(200, json={"rate": 0.78, "paymentOptions": [
            {"payIn": "BALANCE", "targetAmount": 770.0, "disabled": True},
        ]})

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    src = WiseRateSource(
        base_url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="no enabled payment options"):
        src.quote("SGD", "USD")


def test_wise_source_rejects_non_positive_target(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rate": 0.78, "paymentOptions": [
            {"payIn": "BALANCE", "sourceAmount": 1000, "targetAmount": "0",
             "disabled": False},
        ]})

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    src = WiseRateSource(
        base_url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="rate must be positive"):
        src.quote("SGD", "USD")


def test_wise_source_propagates_http_errors(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    src = WiseRateSource(
        base_url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        src.quote("SGD", "USD")


def test_ecb_source_handles_eur_base_and_quote_pairs():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Envelope>
      <Cube>
        <Cube time="2026-05-31">
          <Cube currency="USD" rate="1.20"/>
          <Cube currency="SGD" rate="1.50"/>
        </Cube>
      </Cube>
    </Envelope>
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    src = EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))

    assert src.quote("EUR", "USD").rate == Decimal("1.20")
    assert src.quote("SGD", "EUR").rate == Decimal("0.6666666666666666666666666667")


def test_ecb_source_rejects_xml_without_daily_rates():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<Envelope />")

    src = EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(ValueError, match="daily rates"):
        src.quote("SGD", "USD")


def test_mas_source_rejects_unconfigured_currency():
    src = MasRateSource(resource_ids={})

    with pytest.raises(KeyError, match="MAS resource id"):
        src.quote("EUR", "SGD")


def test_reference_oracle_wraps_source_failures_with_pair_and_type():
    class BrokenSource:
        name = "broken"

        def quote(self, base, quote, *, amount=None):
            raise RuntimeError("boom")

    with pytest.raises(ReferenceUnavailable, match="SGD/USD: broken failed"):
        ReferencePriceOracle(BrokenSource()).get("SGD", "USD")


def test_sources_carry_distinct_default_staleness_windows():
    # Intraday (Wise) rejects after minutes; daily official references tolerate
    # much longer because they legitimately publish once per business day.
    assert WiseRateSource().default_max_age_s == 600.0
    assert EcbRateSource().default_max_age_s == 48 * 3600


def test_ecb_observation_timestamp_reflects_published_date():
    # An old ECB feed must be flagged stale by the freshness guard, which only
    # works if `ts` is the published date rather than fetch time.
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Envelope>
      <Cube>
        <Cube time="2024-01-02">
          <Cube currency="USD" rate="1.20"/>
          <Cube currency="SGD" rate="1.50"/>
        </Cube>
      </Cube>
    </Envelope>
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    src = EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))
    obs = src.quote("SGD", "USD")

    # ts is the 2024 publication date, far older than any sane freshness window.
    assert obs.ts < time.time() - 86400
    with pytest.raises(ReferenceUnavailable, match="stale"):
        ReferencePriceOracle(src).get("SGD", "USD")


def test_per_source_max_age_override_from_config(monkeypatch):
    monkeypatch.setenv("ECB_MAX_AGE_S", "3600")

    assert load_price_reference_source("ecb").default_max_age_s == 3600.0


def test_reference_oracle_allows_quote_at_exact_max_age(monkeypatch):
    class StaticSource:
        name = "static"

        def quote(self, base, quote, *, amount=None):
            return PriceObservation(
                source="static",
                pair="SGD/USD",
                rate=Decimal("0.75"),
                ts=100.0,
            )

    monkeypatch.setattr(time, "time", lambda: 110.0)

    ref = ReferencePriceOracle(StaticSource(), max_age_s=10).get("SGD", "USD")

    assert ref.rate == Decimal("0.75")
