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


def _wise_reply(options, *, rate=0.78):
    return {
        "rate": rate,
        "rateTimestamp": "2026-05-31T00:00:00Z",
        "paymentOptions": options,
    }


def _wise(handler) -> WiseRateSource:
    return WiseRateSource(
        base_url="https://api.wise.test",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _option(pay_in, source, target, *, pay_out="BANK_TRANSFER", disabled=False):
    return {"payIn": pay_in, "payOut": pay_out, "sourceAmount": source,
            "targetAmount": target, "disabled": disabled}


def test_wise_sell_rate_is_what_converting_the_amount_pays_by_the_cheapest_transfer(
    monkeypatch,
):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=_wise_reply([
            _option("DEBIT", 2000, 1540.00),
            _option("BANK_TRANSFER", 2000, 1554.00),  # cheapest usable
            # Better, but conversions inside a Wise balance are not settlement,
            # and a disabled method cannot be used.
            _option("BALANCE", 2000, 1558.00),
            _option("SWIFT", 2000, 1559.00, pay_out="BALANCE"),
            _option("PROMO", 2000, 1600.00, disabled=True),
        ]))

    monkeypatch.delenv("WISE_API_TOKEN", raising=False)
    monkeypatch.setattr("rotor.config._DOTENV_LOADED", True)
    obs = _wise(handler).sell_rate("SGD", "USD", Decimal("2000"))

    assert seen == [("/v3/quotes", {
        "sourceCurrency": "SGD", "targetCurrency": "USD", "sourceAmount": 2000.0})]
    assert obs.rate == Decimal("0.777")
    assert obs.raw["pay_in"] == "BANK_TRANSFER"


def test_wise_buy_rate_is_what_buying_the_amount_costs_by_the_cheapest_transfer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_wise_reply([
            _option("BANK_TRANSFER", 1566.00, 2000),  # cheapest usable
            _option("DEBIT", 1580.00, 2000),
            _option("BALANCE", 1562.00, 2000),
        ], rate=1.282))

    obs = _wise(handler).buy_rate("SGD", "USD", Decimal("2000"))

    # Buy 2000 SGD by paying USD: the target side is fixed.
    assert seen == [{"sourceCurrency": "USD", "targetCurrency": "SGD", "targetAmount": 2000.0}]
    assert obs.rate == Decimal("0.783")


def test_wise_mid_is_halfway_between_its_sell_and_buy_rates_for_the_amount():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "sourceAmount" in body:
            return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 783, 1000)]))

    obs = _wise(handler).quote("SGD", "USD", amount=Decimal("1000"))

    assert obs.rate == Decimal("0.780")
    assert obs.source == "wise"


def test_wise_mid_comes_from_the_same_reply_as_the_sell_rate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    wise = _wise(handler)
    wise.sell_rate("SGD", "USD", Decimal("1000"))

    assert wise.mid_rate("SGD", "USD", Decimal("1000")).rate == Decimal("0.78")
    assert len(calls) == 1


def test_wise_sends_a_token_only_when_one_is_set(monkeypatch):
    auth = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    monkeypatch.setattr("rotor.config._DOTENV_LOADED", True)
    monkeypatch.delenv("WISE_API_TOKEN", raising=False)
    _wise(handler).sell_rate("SGD", "USD", Decimal("1000"))
    monkeypatch.setenv("WISE_API_TOKEN", "token-123")
    _wise(handler).sell_rate("SGD", "USD", Decimal("1000"))

    assert auth == [None, "Bearer token-123"]


def test_wise_reuses_a_recent_reply_and_falls_back_to_it_when_a_request_fails(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("rotor.price_reference.sources.time.time", lambda: clock["now"])
    calls = {"n": 0, "fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["fail"]:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    wise = _wise(handler)
    wise.sell_rate("SGD", "USD", Decimal("1000"))
    clock["now"] += 30
    wise.sell_rate("SGD", "USD", Decimal("1000"))
    assert calls["n"] == 1  # one tick asks Wise once per amount

    clock["now"] += 300
    calls["fail"] = True
    # The last reply comes back; the caller checks how old its rate is.
    assert wise.sell_rate("SGD", "USD", Decimal("1000")).rate == Decimal("0.777")
    assert calls["n"] == 2
    with pytest.raises(httpx.HTTPStatusError):
        wise.sell_rate("SGD", "USD", Decimal("2000"))  # nothing to fall back on


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
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={
            "data": [
                {"currency_code": "USD", "unit": 1, "rate": {
                    "date": "2026-06-09", "buying_rate": "4.04", "selling_rate": "4.06",
                    "middle_rate": None}},
                {"currency_code": "SGD", "unit": 1, "rate": {
                    "date": "2026-06-09", "buying_rate": "3.00", "selling_rate": "3.02",
                    "middle_rate": "3.01"}},
                # Quoted per 100 units, as BNM does for the dirham.
                {"currency_code": "AED", "unit": 100, "rate": {
                    "date": "2026-06-09", "middle_rate": "110.25"}},
            ],
            "meta": {"session": "1700"},
        })

    src = BnmRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))

    assert src.quote("SGD", "USD").rate == Decimal("3.01") / Decimal("4.05")
    assert src.quote("AED", "USD").rate == Decimal("1.1025") / Decimal("4.05")
    # The latest session's list, not the per-currency endpoint (11:30 session).
    assert paths == ["/public/exchange-rate", "/public/exchange-rate"]
    with pytest.raises(ValueError, match="BNM publishes no XYZ rate"):
        src.quote("XYZ", "USD")
