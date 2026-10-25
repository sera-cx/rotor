"""Tests for price-source parsing and token-to-ISO market mapping."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from price_helpers import ecb_source, wise_source

from rotor.price_reference.sources import (
    BnmRateSource,
    EcbRateSource,
    WiseNoRoute,
    parse_ts,
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
    obs = wise_source(handler).sell_rate("SGD", "USD", Decimal("2000"))

    assert seen == [("/v3/quotes", {
        "sourceCurrency": "SGD", "targetCurrency": "USD", "sourceAmount": 2000.0})]
    assert obs.rate == Decimal("0.777")


def test_wise_buy_rate_is_what_buying_the_amount_costs_by_the_cheapest_transfer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_wise_reply([
            _option("BANK_TRANSFER", 1566.00, 2000),  # cheapest usable
            _option("DEBIT", 1580.00, 2000),
            _option("BALANCE", 1562.00, 2000),
        ], rate=1.282))

    obs = wise_source(handler).buy_rate("SGD", "USD", Decimal("2000"))

    # Buy 2000 SGD by paying USD: the target side is fixed.
    assert seen == [{"sourceCurrency": "USD", "targetCurrency": "SGD", "targetAmount": 2000.0}]
    assert obs.rate == Decimal("0.783")


def test_wise_as_a_mid_source_quotes_its_mid_market_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    obs = wise_source(handler).quote("SGD", "USD", amount=Decimal("1000"))

    assert obs.rate == Decimal("0.78")
    assert obs.source == "wise"


def test_wise_mid_comes_from_the_buy_reply_when_wise_cannot_send_from_base():
    # Wise sends to CNH but not from it: selling CNH is a 422, buying it works,
    # and that reply's rate is USD->CNH, so the CNH/USD mid is its inverse.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["sourceCurrency"] == "CNH":
            return httpx.Response(422, json={"errors": [{"code": "error.route.not.supported"}]})
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 141, 1000)], rate=7.1))

    wise = wise_source(handler)

    assert wise.mid_rate("CNH", "USD", Decimal("1000")).rate == 1 / Decimal("7.1")
    with pytest.raises(WiseNoRoute):
        wise.sell_rate("CNH", "USD", Decimal("1000"))
    assert wise.buy_rate("CNH", "USD", Decimal("1000")).rate == Decimal("0.141")


def test_wise_reports_a_route_it_does_not_offer_apart_from_an_outage():
    def no_route(_request):
        return httpx.Response(422, json={"errors": [{"code": "error.route.not.supported"}]})

    def down(_request):
        return httpx.Response(503, json={})

    with pytest.raises(WiseNoRoute):
        wise_source(no_route).sell_rate("PEN", "USD", Decimal("1000"))
    with pytest.raises(httpx.HTTPStatusError):
        wise_source(down).sell_rate("SGD", "USD", Decimal("1000"))


def test_wise_mid_comes_from_the_same_reply_as_the_sell_rate():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    wise = wise_source(handler)
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
    wise_source(handler).sell_rate("SGD", "USD", Decimal("1000"))
    monkeypatch.setenv("WISE_API_TOKEN", "token-123")
    wise_source(handler).sell_rate("SGD", "USD", Decimal("1000"))

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

    wise = wise_source(handler)
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


def test_wise_not_answering_costs_one_timeout_a_minute_not_one_per_request(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("rotor.price_reference.sources.time.time", lambda: clock["now"])
    calls = {"n": 0, "hang": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["hang"]:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_wise_reply([_option("BANK_TRANSFER", 1000, 777)]))

    wise = wise_source(handler)
    for amount in ("1000", "2000", "3000"):
        with pytest.raises(httpx.ReadTimeout):
            wise.sell_rate("SGD", "USD", Decimal(amount))
    assert calls["n"] == 1

    clock["now"] += 61
    calls["hang"] = False
    assert wise.sell_rate("SGD", "USD", Decimal("1000")).rate == Decimal("0.777")
    assert calls["n"] == 2


def test_a_wise_token_with_a_line_break_is_refused_by_name_never_echoed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be sent")

    monkeypatch.setattr("rotor.config._DOTENV_LOADED", True)
    monkeypatch.setenv("WISE_API_TOKEN", "secret-part\nmore-secret")

    with pytest.raises(ValueError, match="WISE_API_TOKEN contains whitespace") as raised:
        wise_source(handler).sell_rate("SGD", "USD", Decimal("1000"))
    assert "secret" not in str(raised.value)


def test_ecb_source_crosses_rates_through_eur():
    src = ecb_source("2026-05-31")

    obs = src.quote("SGD", "USD")
    assert (obs.pair, obs.rate) == ("SGD/USD", Decimal("0.8"))
    # EUR itself is seeded at 1, so it works on either side of a pair.
    assert src.quote("EUR", "USD").rate == Decimal("1.20")
    assert src.quote("SGD", "EUR").rate == Decimal("0.6666666666666666666666666667")


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
    # The latest session's list, not the per-currency endpoint (11:30 session),
    # read once for both pairs.
    assert paths == ["/public/exchange-rate"]
    with pytest.raises(ValueError, match="BNM publishes no XYZ rate"):
        src.quote("XYZ", "USD")


def test_a_daily_feed_is_read_once_per_minute_for_all_pairs_and_so_is_a_failure(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("rotor.price_reference.sources.time.time", lambda: clock["now"])
    calls = {"n": 0, "fail": True}
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Envelope><Cube><Cube time="2026-06-09">
      <Cube currency="USD" rate="1.10"/><Cube currency="SGD" rate="1.45"/>
      <Cube currency="JPY" rate="170"/>
    </Cube></Cube></Envelope>"""

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["fail"]:
            raise httpx.ReadTimeout("ECB hung")
        return httpx.Response(200, text=xml)

    src = EcbRateSource(http=httpx.Client(transport=httpx.MockTransport(handler)))

    # A hung feed costs one timeout for every pair in the tick, not one each.
    for pair in (("SGD", "USD"), ("JPY", "USD")):
        with pytest.raises(httpx.ReadTimeout):
            src.quote(*pair)
    assert calls["n"] == 1

    clock["now"] += 61
    calls["fail"] = False
    src.quote("SGD", "USD")
    src.quote("JPY", "USD")
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T08:00:00+08:00",
        1767225600,
    ],
)
def test_parse_ts_accepts_common_api_timestamp_shapes(value):
    assert parse_ts(value) == pytest.approx(1767225600.0)


def test_parse_ts_returns_none_for_bad_values():
    assert parse_ts("") is None
    assert parse_ts("not-a-date") is None
