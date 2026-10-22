"""Tests for price-source parsing and token-to-ISO market mapping."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from rotor.price_reference.sources import (
    BnmRateSource,
    EcbRateSource,
    FedH10RateSource,
    MasRateSource,
    WiseRateSource,
)
from rotor.price_reference.symbols import market_to_iso_pair


def test_market_to_iso_pair_defaults():
    assert market_to_iso_pair("XSGD/USDC") == ("SGD", "USD")
    assert market_to_iso_pair("myrt_usdt") == ("MYR", "USD")


def test_every_earn_majors_token_has_a_built_in_currency():
    # Earn's default basket, in both orientations Sera may list a pair.
    expected = {
        "EURC": "EUR", "JPYC": "JPY", "TGBP": "GBP", "AXCNH": "CNH",
        "AUDD": "AUD", "CADC": "CAD", "VCHF": "CHF", "XSGD": "SGD",
    }
    for token, iso in expected.items():
        assert market_to_iso_pair(f"{token}/USDC") == (iso, "USD")
        assert market_to_iso_pair(f"USDT/{token}") == ("USD", iso)


def _wise_quote_response(options):
    return {
        "rate": 0.78,
        "rateTimestamp": "2026-05-31T00:00:00Z",
        "sourceCurrency": "SGD",
        "targetCurrency": "USD",
        "paymentOptions": options,
    }


def test_wise_source_prices_amount_and_picks_cheapest_enabled(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_wise_quote_response([
            # cheapest enabled (most target) — should be selected
            {"payIn": "BALANCE", "payOut": "BANK_TRANSFER",
             "sourceAmount": 2000, "targetAmount": 1547.60,
             "fee": {"total": 6.07}, "disabled": False},
            {"payIn": "CARD", "payOut": "BANK_TRANSFER",
             "sourceAmount": 2000, "targetAmount": 1476.74,
             "fee": {"total": 97.37}, "disabled": False},
            # better rate but disabled — must be skipped
            {"payIn": "PROMO", "payOut": "X",
             "sourceAmount": 2000, "targetAmount": 1600.0,
             "fee": {"total": 0.0}, "disabled": True},
        ]))

    monkeypatch.setenv("WISE_API_TOKEN", "token-123")
    src = WiseRateSource(
        base_url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    obs = src.quote("SGD", "USD", amount=Decimal("2000"))

    # Effective (fee-inclusive) rate from the cheapest enabled option.
    assert obs.rate == Decimal("1547.60") / Decimal("2000")
    assert obs.pair == "SGD/USD"
    assert obs.raw["pay_in"] == "BALANCE"
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/v3/quotes")
    assert seen["auth"] == "Bearer token-123"
    assert seen["body"] == {
        "sourceCurrency": "SGD",
        "targetCurrency": "USD",
        "sourceAmount": 2000.0,
    }


def test_wise_source_honors_pinned_payment_method(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_wise_quote_response([
            {"payIn": "BALANCE", "payOut": "BANK_TRANSFER",
             "sourceAmount": 1000, "targetAmount": 775.0, "disabled": False},
            {"payIn": "CARD", "payOut": "BANK_TRANSFER",
             "sourceAmount": 1000, "targetAmount": 740.0, "disabled": False},
        ]))

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    src = WiseRateSource(
        pay_in="card",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    obs = src.quote("SGD", "USD", amount=Decimal("1000"))

    # The pinned (more expensive) CARD option is used despite a cheaper one.
    assert obs.rate == Decimal("740.0") / Decimal("1000")


def test_wise_source_uses_default_amount_when_none_given(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_wise_quote_response([
            {"payIn": "BALANCE", "payOut": "BANK_TRANSFER",
             "sourceAmount": 500, "targetAmount": 388.0, "disabled": False},
        ]))

    monkeypatch.setenv("WISE_API_TOKEN", "t")
    src = WiseRateSource(
        default_amount="500",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    src.quote("SGD", "USD")

    assert seen["body"]["sourceAmount"] == 500.0


def test_wise_source_quotes_without_a_token(monkeypatch):
    # Wise prices anonymous quote requests; the token is optional.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_wise_quote_response([
            {"payIn": "BANK_TRANSFER", "payOut": "BANK_TRANSFER",
             "sourceAmount": 1000, "targetAmount": 740.0, "disabled": False},
        ]))

    monkeypatch.delenv("WISE_API_TOKEN", raising=False)
    monkeypatch.setattr("rotor.config._DOTENV_LOADED", True)
    src = WiseRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))

    obs = src.quote("SGD", "USD", amount=Decimal("1000"))

    assert seen["auth"] is None
    assert obs.rate == Decimal("0.74")


def test_ecb_source_crosses_rates_through_eur():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
      xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
      <Cube>
        <Cube time="2026-05-31">
          <Cube currency="USD" rate="1.20"/>
          <Cube currency="SGD" rate="1.50"/>
        </Cube>
      </Cube>
    </gesmes:Envelope>
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    src = EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))
    obs = src.quote("SGD", "USD")

    assert obs.rate == Decimal("0.8")
    assert obs.pair == "SGD/USD"
    assert obs.raw["date"] == "2026-05-31"


def test_fed_h10_source_crosses_rates_through_usd():
    # FRED API observations, newest first; the latest day may be unpublished (".").
    payloads = {
        "DEXSIUS": {"observations": [
            {"date": "2026-06-03", "value": "."},
            {"date": "2026-06-02", "value": "1.50"},
        ]},
        "DEXUSEU": {"observations": [
            {"date": "2026-06-02", "value": "1.20"},
        ]},
    }
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        series = request.url.params["series_id"]
        seen["api_key"] = request.url.params.get("api_key", "")
        seen["file_type"] = request.url.params.get("file_type", "")
        return httpx.Response(200, json=payloads[series])

    src = FedH10RateSource(
        api_key="test-key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    obs = src.quote("EUR", "SGD")

    assert obs.rate == Decimal("1.800")
    assert obs.pair == "EUR/SGD"
    # The FRED API key and JSON format are sent on the request.
    assert seen["api_key"] == "test-key"
    assert seen["file_type"] == "json"


def test_fed_h10_source_reads_each_series_the_way_it_quotes():
    # The H.10 release quotes GBP, AUD, NZD and EUR as U.S. dollars per unit
    # (DEXUS<cc>) and the rest as units per U.S. dollar (DEX<cc>US).
    payloads = {
        "DEXUSUK": "1.2700", "DEXUSAL": "0.6600", "DEXUSNZ": "0.6000",
        "DEXJPUS": "150.00", "DEXSZUS": "0.8000",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        value = payloads[request.url.params["series_id"]]
        return httpx.Response(200, json={"observations": [{"date": "2026-06-02", "value": value}]})

    src = FedH10RateSource(api_key="k", http=httpx.Client(transport=httpx.MockTransport(handler)))

    def rate(base: str, quote: str) -> Decimal:
        # Crossing through USD divides twice; compare to 20 places.
        return round(src.quote(base, quote).rate, 20)

    assert rate("GBP", "USD") == Decimal("1.27")
    assert rate("AUD", "USD") == Decimal("0.66")
    assert rate("NZD", "USD") == Decimal("0.6")
    assert rate("JPY", "USD") == round(Decimal("1") / Decimal("150"), 20)
    assert rate("GBP", "CHF") == Decimal("1.016")


def test_fed_h10_source_rejects_a_series_it_cannot_orient():
    with pytest.raises(ValueError, match="cannot tell which way FRED series 'GBPUSD' quotes"):
        FedH10RateSource(series_by_currency={"GBP": "GBPUSD"})


def test_fed_h10_source_requires_api_key():
    src = FedH10RateSource()  # no api_key configured

    with pytest.raises(ValueError, match="FRED_API_KEY"):
        src.quote("SGD", "USD")


def test_bnm_source_crosses_rates_through_myr_and_averages_when_middle_missing():
    payloads = {
        "USD": {
            "data": {
                "currency_code": "USD",
                "unit": 1,
                "rate": {
                    "date": "2026-06-09",
                    "buying_rate": "4.04",
                    "selling_rate": "4.06",
                    "middle_rate": None,
                },
            }
        },
        "SGD": {
            "data": {
                "currency_code": "SGD",
                "unit": 1,
                "rate": {
                    "date": "2026-06-09",
                    "buying_rate": "3.00",
                    "selling_rate": "3.02",
                    "middle_rate": "3.01",
                },
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        currency = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=payloads[currency])

    src = BnmRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))
    obs = src.quote("SGD", "USD")

    assert obs.rate == Decimal("3.01") / Decimal("4.05")
    assert obs.pair == "SGD/USD"


def test_mas_source_crosses_rates_through_sgd():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["resource_id"] == MasRateSource.DEFAULT_RESOURCE_IDS["USD"]
        return httpx.Response(200, json={
            "success": True,
            "result": {
                "records": [{
                    "date": "2026-06-09",
                    "exchange_rate_usd": "1.35",
                }]
            },
        })

    src = MasRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))
    obs = src.quote("USD", "SGD")

    assert obs.rate == Decimal("1.35")
    assert obs.pair == "USD/SGD"
