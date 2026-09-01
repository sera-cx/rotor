"""Tests for price-source parsing and token-to-ISO market mapping."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from rotor.price_reference.sources import (
    BnmRateSource,
    CoinGeckoRateSource,
    EcbRateSource,
    FedH10RateSource,
    MasRateSource,
    WiseRateSource,
)
from rotor.price_reference.symbols import market_to_iso_pair


def test_market_to_iso_pair_defaults():
    assert market_to_iso_pair("XSGD/USDC") == ("SGD", "USD")
    assert market_to_iso_pair("myrt_usdt") == ("MYR", "USD")


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


def test_wise_source_requires_token(monkeypatch):
    monkeypatch.delenv("WISE_API_TOKEN", raising=False)
    monkeypatch.setattr("rotor.config._DOTENV_LOADED", True)
    src = WiseRateSource()

    with pytest.raises(ValueError, match="WISE_API_TOKEN"):
        src.quote("SGD", "USD")


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


def _coingecko_response(**legs):
    # simple/price nests each vs_currency under the coin id, lower-cased.
    return {"tether": {**legs}}


def test_coingecko_source_crosses_through_bridge_and_stamps_update_time():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["has_key_header"] = "x-cg-demo-api-key" in request.headers
        return httpx.Response(200, json=_coingecko_response(
            sgd=1.29, usd=0.999, last_updated_at=1756000000
        ))

    src = CoinGeckoRateSource(
        base_url="https://api.coingecko.test/api/v3",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    obs = src.quote("SGD", "USD")

    # Bridge units cancel: (USD per bridge) / (SGD per bridge) = USD per SGD.
    assert obs.rate == Decimal("0.999") / Decimal("1.29")
    assert obs.pair == "SGD/USD"
    assert obs.source == "coingecko"
    # Freshness must follow CoinGecko's own timestamp, not fetch time.
    assert obs.ts == 1756000000.0
    assert obs.raw["bridge_id"] == "tether"
    assert seen["path"].endswith("/simple/price")
    # Both legs are fetched in one request.
    assert seen["params"]["vs_currencies"] == "sgd,usd"
    assert seen["params"]["ids"] == "tether"
    # No key configured means no key header is sent.
    assert seen["has_key_header"] is False


def test_coingecko_source_sends_api_key_header_when_configured():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-cg-pro-api-key")
        return httpx.Response(200, json=_coingecko_response(sgd=1.29, usd=0.999))

    src = CoinGeckoRateSource(
        base_url="https://pro-api.coingecko.test/api/v3",
        api_key="cg-key-123",
        api_key_header="x-cg-pro-api-key",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    src.quote("SGD", "USD")

    assert seen["key"] == "cg-key-123"


def test_coingecko_source_same_currency_pair_is_unity():
    def handler(request: httpx.Request) -> httpx.Response:
        # A same-currency pair must not ask for a duplicated vs_currency.
        assert dict(request.url.params)["vs_currencies"] == "usd"
        return httpx.Response(200, json=_coingecko_response(usd=0.999))

    src = CoinGeckoRateSource(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert src.quote("USD", "USD").rate == Decimal(1)


def test_coingecko_source_rejects_unsupported_vs_currency():
    def handler(_request: httpx.Request) -> httpx.Response:
        # CoinGecko silently omits fiat currencies it does not support.
        return httpx.Response(200, json=_coingecko_response(usd=0.999))

    src = CoinGeckoRateSource(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="unsupported vs_currency"):
        src.quote("MYR", "USD")


def test_coingecko_source_rejects_unknown_bridge_id():
    def handler(_request: httpx.Request) -> httpx.Response:
        # An unknown coin id returns an empty object, not an error status.
        return httpx.Response(200, json={})

    src = CoinGeckoRateSource(
        bridge_id="not-a-coin",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="no prices for bridge id"):
        src.quote("SGD", "USD")


def test_coingecko_source_rejects_non_positive_rate():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_coingecko_response(sgd=0, usd=0.999))

    src = CoinGeckoRateSource(
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="rate must be positive"):
        src.quote("SGD", "USD")
