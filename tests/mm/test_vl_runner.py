"""Tests for VL grouping, fill settlement, return orders, and placement tracking."""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from mm_builders import intent, market, vl_config

from rotor.mm import vl_runner as vr
from rotor.mm.exchange import errors
from rotor.mm.exchange.client import (
    MarketInfo,
    OrderRecord,
    SignedOrder,
    SignedVLBatch,
    VLBatchResult,
)
from rotor.mm.positions import Position
from rotor.mm.vl_config import PairConfig
from rotor.mm.vl_runner import (
    _place_groups,
    _validate_budget_caps,
    _without_self_cross,
    group_intents,
)
from rotor.price_reference.models import PriceObservation, ReferencePrice
from rotor.price_reference.sources import WiseNoRoute

LOG = logging.getLogger("test")
HOUR = 3600.0


def test_group_intents_uses_vl_for_multi_leg_groups():
    groups = group_intents([
        intent("XSGD/USDC", "bid", "USDC"),
        intent("EURC/USDC", "bid", "USDC"),
        intent("XSGD/USDC", "ask", "XSGD"),
    ], 2)

    assert groups[0][0] == "USDC"
    assert groups[0][2] is True
    assert [i.market for i in groups[0][1]] == ["XSGD/USDC", "EURC/USDC"]
    assert groups[1][0] == "XSGD"
    assert groups[1][2] is False


def test_validate_budget_caps_rejects_oversized_budgeted_intent():
    cfg = vl_config(budget="50")

    with pytest.raises(ValueError, match="above configured budget"):
        _validate_budget_caps([
            ("USDC", [intent("XSGD/USDC", "bid", "USDC", price="1.10", qty="100")], False)
        ], cfg)


def _runtime(info: MarketInfo | None = None) -> vr.PairRuntime:
    return vr.PairRuntime(
        config=PairConfig(
            market="XSGD/USDC",
            price_source="wise",
            qty_base=Decimal("100"),
            fixed_bps=Decimal("50"),
        ),
        info=info or market(),
        oracle=None,
        iso_base="SGD",
        iso_quote="USD",
    )


class _Wise:
    """Wise's after-fee rates around a live mid, recording the sizes priced."""

    default_max_age_s = 600.0

    def __init__(self, *, live="1.0000", sell_fee="0.0024", buy_fee="0.0037", age=0.0,
                 fail=False, no_sell_route=False):
        self.live = Decimal(live)
        self.sell_fee = Decimal(sell_fee)
        self.buy_fee = Decimal(buy_fee)
        self.age = age
        self.fail = fail
        self.no_sell_route = no_sell_route
        self.priced: list[tuple[str, Decimal]] = []

    def sell_rate(self, base, quote, qty_base):
        if self.no_sell_route:
            raise WiseNoRoute("Wise does not convert SGD to USD")
        return self._obs("sell", qty_base, self.live * (1 - self.sell_fee))

    def buy_rate(self, base, quote, qty_base):
        return self._obs("buy", qty_base, self.live * (1 + self.buy_fee))

    def _obs(self, direction, qty, rate):
        self.priced.append((direction, qty))
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return PriceObservation(source="wise", pair="SGD/USD", rate=rate, ts=NOW - self.age)


NOW = 1_800_000_000.0


def _priced(intents, wise, info=None, *, mid="1.0000", stale=False, now=NOW, refs=None):
    runtime = replace(_runtime(info), settlement=wise)
    ref = ReferencePrice(pair="SGD/USD", rate=Decimal(mid), ts=now, source="ecb", stale=stale)
    return vr._at_wise_prices(intents, runtime, ref, now=now, log=LOG, refs=refs)


def _quote(fixed_bps="25", mid="1.0000", qty="100"):
    m, f = Decimal(mid), Decimal(fixed_bps) / 10000
    return [
        intent("XSGD/USDC", "bid", "USDC", price=str(m * (1 - f)), qty=qty),
        intent("XSGD/USDC", "ask", "XSGD", price=str(m * (1 + f)), qty=qty),
    ]


def _with_return(position, intents, *, now, mid="1.0000", info=None, return_bps="2",
                 window="48"):
    return vr._with_return_order(
        intents,
        _runtime(info),
        position,
        vl_config(return_bps=return_bps, window=window),
        mid=Decimal(mid),
        now=now,
        log=LOG,
    )


# ---------------------------------------------------------------------------
# Return orders
# ---------------------------------------------------------------------------

def test_short_position_swaps_the_bid_for_a_return_bid_and_keeps_the_ask():
    # Sold 40 XSGD at 1.0050 twelve hours ago; mid has not moved.
    position = Position(Decimal("-40"), Decimal("1.0050"), 0.0)
    intents = _quote("50")

    out = _with_return(position, intents, now=12 * HOUR)

    bid, ask = out
    # 1.0050 x (1 - 100 bps x 0.75) = 0.9974625, rounded down onto the grid.
    assert (bid.side, bid.qty_base, bid.price) == ("bid", Decimal("40"), Decimal("0.9974"))
    assert ask == intents[1]
    # Everything except size and price is the normal bid's.
    assert (bid.spent_token, bid.expiration) == (intents[0].spent_token, intents[0].expiration)


def test_the_return_window_setting_sets_how_fast_the_auction_walks():
    # 12 h into a 24 h window is halfway: 1.0050 x (1 - 100 bps x 0.5). (Mid
    # 1.0100 keeps the return_bps limit out of the way.)
    position = Position(Decimal("-40"), Decimal("1.0050"), 0.0)

    bid, _ask = _with_return(
        position, _quote("50", "1.0100"), mid="1.0100", now=12 * HOUR, window="24")
    assert bid.price == Decimal("0.9999")

    # The default 48 h window is only a quarter through.
    bid, _ask = _with_return(position, _quote("50", "1.0100"), mid="1.0100", now=12 * HOUR)
    assert bid.price == Decimal("0.9974")


def test_return_order_holds_at_the_fill_price_while_the_market_stays_away():
    # Bought 40 XSGD at 0.9950 three days ago; the market has fallen to 0.9700.
    position = Position(Decimal("40"), Decimal("0.9950"), 0.0)
    intents = _quote("50", "0.9700")

    _bid, ask = _with_return(position, intents, mid="0.9700", now=72 * HOUR)

    assert (ask.qty_base, ask.price) == (Decimal("40"), Decimal("0.9950"))


def test_return_order_sits_return_bps_from_mid_when_the_market_came_back():
    position = Position(Decimal("-40"), Decimal("1.0050"), 0.0)

    bid, _ask = _with_return(position, _quote("50", "1.0000"), mid="1.0000", now=48 * HOUR)

    assert bid.price == Decimal("0.9998")


def test_return_order_never_spends_more_than_the_normal_order():
    # A 300 XSGD short, more than one 100 XSGD order: the bid it replaces spent
    # 99.5 USDC, so the return bid buys at most 99.5 / 0.9998 XSGD of it.
    position = Position(Decimal("-300"), Decimal("1.0050"), 0.0)
    intents = _quote("50")

    bid, _ask = _with_return(position, intents, now=48 * HOUR)

    assert bid.price == Decimal("0.9998")
    assert bid.qty_base == Decimal("99.51")
    assert bid.qty_base * bid.price <= intents[0].qty_base * intents[0].price


def test_a_dust_position_keeps_the_full_normal_order_on_its_side():
    # 9.99 XSGD short against a 100 XSGD bid: under a tenth of it, so the bid
    # stays a full normal quote; 10 XSGD is enough to take the side.
    intents = _quote("50")

    assert _with_return(Position(Decimal("-9.99"), Decimal("1.0050"), 0.0), intents,
                        now=12 * HOUR) == intents

    bid, _ask = _with_return(Position(Decimal("-10"), Decimal("1.0050"), 0.0), intents,
                             now=12 * HOUR)
    assert (bid.qty_base, bid.is_return) == (Decimal("10"), True)


def test_position_below_the_market_minimum_keeps_the_normal_order():
    position = Position(Decimal("-0.5"), Decimal("1.0050"), 0.0)
    intents = _quote("50")

    out = _with_return(position, intents, now=0.0, info=market(min_bid_quote="1"))

    assert out == intents


# ---------------------------------------------------------------------------
# Normal orders priced at Wise's after-fee rates
# ---------------------------------------------------------------------------

def test_normal_quotes_sit_at_wises_after_fee_prices_whatever_fixed_bps_is():
    # Wise: selling SGD costs 24 bps, buying it 37 bps.
    for fixed_bps in ("25", "50", "100"):
        bid, ask = _priced(_quote(fixed_bps), _Wise())
        assert (bid.price, ask.price) == (Decimal("0.9976"), Decimal("1.0037"))


def test_wise_is_priced_for_each_orders_own_size():
    wise = _Wise()
    bid, ask = _quote()
    _priced([replace(bid, qty_base=Decimal("40")), ask], wise)

    assert wise.priced == [("sell", Decimal("40")), ("buy", Decimal("100"))]


def test_quotes_follow_wises_live_rates_not_a_stale_daily_mid():
    # The central bank published 1.0000; the live market has since risen 1%.
    bid, ask = _priced(_quote("50"), _Wise(live="1.0100"))
    assert (bid.price, ask.price) == (Decimal("1.0075"), Decimal("1.0138"))

    # The market has fallen 1%.
    bid, ask = _priced(_quote("50"), _Wise(live="0.9900"))
    assert (bid.price, ask.price) == (Decimal("0.9876"), Decimal("0.9937"))


def test_a_return_order_keeps_its_auction_price_and_asks_wise_nothing():
    wise = _Wise()
    bid, ask = _quote()
    returning = replace(ask, price=Decimal("1.0001"), is_return=True)

    priced = _priced([bid, returning], wise)

    assert priced[1] is returning
    assert wise.priced == [("sell", Decimal("100"))]


def test_a_side_wise_does_not_offer_sits_at_the_weekday_spread_around_the_mid():
    # Wise sends to some currencies it cannot send from: the ask keeps Wise's
    # price, the bid sits fallback_bps (default 50) under the mid.
    refs = vr.TickReferences()
    bid, ask = _priced(_quote(), _Wise(no_sell_route=True), refs=refs)

    assert bid.price == Decimal("0.9950")
    assert ask.price == Decimal("1.0037")
    # Wise answered (it offers no such route), so this is not an outage.
    assert refs.wise_outage == {}


def test_the_weekend_spread_covers_fx_weekends_carried_mids_and_wise_outages():
    saturday = NOW + 24 * HOUR  # NOW is a Friday morning.
    friday_night = NOW + 13 * HOUR  # 21:00 UTC, when the FX weekend starts.
    for wise, kwargs in (
        (_Wise(no_sell_route=True), {"now": saturday}),
        (_Wise(no_sell_route=True), {"now": friday_night}),
        (_Wise(no_sell_route=True), {"stale": True}),
    ):
        bid, _ask = _priced(_quote(), wise, **kwargs)
        assert bid.price == Decimal("0.9800")  # 200 bps under the mid
    # Sunday 21:00 UTC the market reopens and the weekday spread is back.
    bid, _ask = _priced(_quote(), _Wise(no_sell_route=True), now=NOW + 61 * HOUR)
    assert bid.price == Decimal("0.9950")

    # Wise unreachable, or its rate older than its window: both sides go wide,
    # and the outage is recorded for the alert.
    for wise in (_Wise(fail=True), _Wise(age=601.0)):
        refs = vr.TickReferences()
        bid, ask = _priced(_quote(), wise, refs=refs)
        assert (bid.price, ask.price) == (Decimal("0.9800"), Decimal("1.0200"))
        assert list(refs.wise_outage) == ["XSGD/USDC"]


def test_a_bid_below_the_market_minimum_at_wises_price_is_dropped():
    # 100 XSGD at Wise's 0.9900 is 99.00 USDC, under the 99.50 minimum.
    sides = _priced(_quote("25"), _Wise(sell_fee="0.0100"), info=market(min_bid_quote="99.5"))

    assert [intent.side for intent in sides] == ["ask"]


# ---------------------------------------------------------------------------
# Orders trimmed to their budget
# ---------------------------------------------------------------------------

def test_an_order_the_price_pushed_over_budget_is_trimmed_to_fit():
    # Sized to the whole 1,000 USDC budget at setup; the price has since risen,
    # so 1,100 XSGD at 0.995 would spend 1,094.50 USDC.
    over = intent("XSGD/USDC", "bid", "USDC", price="0.995", qty="1100")
    under = intent("EURC/USDC", "bid", "USDC", price="0.995", qty="1000")
    unbudgeted = intent("XSGD/USDC", "ask", "XSGD", price="1.005", qty="5000")

    out = vr._within_limits(
        [over, under, unbudgeted], market(), BUDGETS, None, LOG)

    # 1,000 / 0.995 = 1,005.0251..., rounded down to the 0.01 size step.
    assert out[0].qty_base == Decimal("1005.02")
    assert out[0].qty_base * out[0].price <= Decimal("1000")
    assert out[1:] == [under, unbudgeted]


def test_an_ask_spending_a_budgeted_token_is_trimmed_to_the_budget():
    # On a USDC-base market the ask is what spends USDC.
    ask = intent("USDC/AUDD", "ask", "USDC", price="1.52", qty="1200")

    out = vr._within_limits([ask], market(), BUDGETS, None, LOG)

    assert out[0].qty_base == Decimal("1000")


def test_an_order_too_small_after_trimming_is_dropped(caplog):
    bid = intent("XSGD/USDC", "bid", "USDC", price="0.995", qty="100")

    out = vr._within_limits(
        [bid], market(min_bid_quote="1"), {"USDC": Decimal("0.5")}, None, LOG)

    assert out == []
    assert "XSGD/USDC bid dropped: trimmed to its USDC budget it is too small" in caplog.text


BUDGETS = {"USDC": Decimal("1000")}


def test_spend_limit_is_the_lower_of_budget_and_vault_balance():
    funds = {"USDC": Decimal("600"), "EURC": Decimal("50")}

    assert vr._spend_limit("USDC", BUDGETS, funds) == (Decimal("600"), "balance")
    assert vr._spend_limit("USDC", BUDGETS, {"USDC": Decimal("5000")}) == (Decimal("1000"), "budget")
    assert vr._spend_limit("EURC", BUDGETS, funds) == (Decimal("50"), "balance")
    # A token the wallet does not hold cannot be spent at all.
    assert vr._spend_limit("XSGD", BUDGETS, funds) == (Decimal("0"), "balance")
    # Without balances, budgets alone apply and unbudgeted tokens are unlimited.
    assert vr._spend_limit("USDC", BUDGETS, None) == (Decimal("1000"), "budget")
    assert vr._spend_limit("XSGD", BUDGETS, None) is None


def test_orders_are_fitted_to_what_the_wallet_holds(caplog):
    caplog.set_level(logging.DEBUG)
    bid = intent("XSGD/USDC", "bid", "USDC", price="0.995", qty="1000")
    unheld_ask = intent("XSGD/USDC", "ask", "XSGD", price="1.005", qty="1000")
    held_ask = intent("EURC/USDC", "ask", "EURC", price="1.005", qty="1000")
    funds = {"USDC": Decimal("600"), "EURC": Decimal("50")}

    out = vr._within_limits([bid, unheld_ask, held_ask], market(), BUDGETS, funds, LOG)

    # 600 / 0.995 = 603.01..., rounded down; the unheld XSGD ask is left out
    # quietly; the EURC ask shrinks to the 50 EURC held.
    assert [(i.market, i.side, i.qty_base) for i in out] == [
        ("XSGD/USDC", "bid", Decimal("603.01")),
        ("EURC/USDC", "ask", Decimal("50")),
    ]
    assert "XSGD/USDC ask left out: the wallet holds no XSGD" in caplog.text
    assert "WARNING" not in caplog.text


# ---------------------------------------------------------------------------
# Fill settlement
# ---------------------------------------------------------------------------

def _unsettled(order_id="o-1", side="ask", price="1.0050", expiration=1000):
    return vr.UnsettledOrder(
        order_id=order_id,
        market="XSGD/USDC",
        side=side,
        price=Decimal(price),
        expiration=expiration,
        placed_at=500.0,
    )


class _FillsClient:
    """Fake client whose fills per order can be changed between calls."""

    def __init__(self, fills: dict[str, list[dict]] | None = None) -> None:
        self.fills = fills or {}
        self.calls: list[str] = []
        self.error: Exception | None = None

    def get_order_fills(self, order_id):
        self.calls.append(order_id)
        if self.error is not None:
            raise self.error
        return list(self.fills.get(order_id, []))


def _fill(quantity: str, status: str | None = "settled") -> dict:
    row = {"quantity": quantity, "price": "1.0050"}
    if status is not None:
        row["settlement_status"] = status
    return row


def _positions(client, store, server_now, stuck=None):
    """One tick's fill read: settle what is final, return positions for quoting."""
    unsettled = store.load_unsettled()
    fills, _failed, _unknown = vr._read_fills(client, [order.order_id for order in unsettled])
    return vr._positions_for_quoting(
        store, unsettled, fills, server_now=server_now, log=LOG, stuck=stuck)


SOLD_40 = {"XSGD/USDC": Position(Decimal("-40"), Decimal("1.0050"), 500.0)}


def test_fills_hold_their_return_side_at_once_and_are_stored_after_expiry():
    store = vr.StateStore(":memory:")
    store.add_unsettled([_unsettled()])
    client = _FillsClient({"o-1": [_fill("30"), _fill("10")]})

    # Before expiry plus grace the order could still fill: its fills count for
    # quoting but are not stored, and it stays in the ledger.
    assert _positions(client, store, 1000 + vr.SETTLE_GRACE_SECONDS - 1) == SOLD_40
    assert store.load_positions() == {}
    assert len(store.load_unsettled()) == 1

    # Once it is final it is stored and leaves the ledger, so it is never
    # polled or counted again.
    assert _positions(client, store, 1000 + vr.SETTLE_GRACE_SECONDS) == SOLD_40
    assert store.load_positions() == SOLD_40
    assert store.load_unsettled() == []
    assert _positions(client, store, 5000) == SOLD_40
    assert client.calls == ["o-1", "o-1"]


def test_a_fill_still_settling_holds_but_is_not_stored():
    store = vr.StateStore(":memory:")
    store.add_unsettled([_unsettled()])
    client = _FillsClient({"o-1": [_fill("30"), _fill("10", "confirming")]})

    assert _positions(client, store, 5000) == SOLD_40
    assert store.load_positions() == {}
    assert len(store.load_unsettled()) == 1

    client.fills["o-1"][1]["settlement_status"] = "settled"
    assert _positions(client, store, 5300) == SOLD_40
    assert store.load_positions() == SOLD_40


def test_failed_and_reverted_fills_count_as_nothing():
    rows = [_fill("30", "failed"), _fill("10", "reverted")]
    assert vr._settled_fill_qty(rows) == 0
    assert vr._visible_fill_qty(rows) == 0
    assert vr._settled_fill_qty([_fill("30"), _fill("10", "failed")]) == Decimal("30")
    # A fill without a status (older responses) counts as settled.
    assert vr._settled_fill_qty([_fill("7", None)]) == Decimal("7")
    # Anything unrecognised means wait rather than guess, but it still holds.
    assert vr._settled_fill_qty([_fill("7", "pending")]) is None
    assert vr._settled_fill_qty([_fill("7", "something-new")]) is None
    assert vr._visible_fill_qty([_fill("7", "something-new")]) == Decimal("7")

    store = vr.StateStore(":memory:")
    store.add_unsettled([_unsettled()])
    client = _FillsClient({"o-1": [_fill("30", "failed")]})
    assert _positions(client, store, 900) == {}
    assert _positions(client, store, 5000) == {}
    assert store.load_unsettled() == []


def test_unknown_orders_have_no_fills_and_unreadable_ones_are_reported():
    client = _FillsClient()

    # Sera's own reply for an order it has no record of.
    client.error = errors.from_response(404, {"detail": "Order not found"})
    assert vr._read_fills(client, ["a", "b", "a"]) == ({"a": [], "b": []}, {}, {"a", "b"})
    assert client.calls == ["a", "b"]

    client.error = errors.SeraRateLimit("RATE_LIMITED", "slow down", 429)
    fills, failed, unknown = vr._read_fills(client, ["a"])
    assert (fills, unknown) == ({}, set())
    assert list(failed) == ["a"] and isinstance(failed["a"], errors.SeraRateLimit)


@pytest.mark.parametrize("error", [
    # An edge or proxy error page is not Sera saying the order does not exist.
    errors.from_response(404, {"detail": "<html><title>404 Not Found</title></html>"}),
    errors.from_response(404, {"detail": "Not Found"}),
    # A reply with no items list (an HTML 200, an empty body, a renamed key).
    RuntimeError("GET /fills/a returned no items list"),
])
def test_a_reply_that_is_not_seras_answer_is_unreadable_not_unfilled(error):
    client = _FillsClient()
    client.error = error

    fills, failed, unknown = vr._read_fills(client, ["a"])

    assert (fills, unknown, list(failed)) == ({}, set(), ["a"])


def test_a_row_rotor_cannot_parse_makes_its_order_unreadable():
    client = _FillsClient({"good": [_fill("5")], "bad": [{"quantity": "five"}]})

    fills, failed, _unknown = vr._read_fills(client, ["good", "bad"])

    assert fills == {"good": [_fill("5")]}
    assert list(failed) == ["bad"]


def test_an_unreadable_order_is_never_settled_as_unfilled():
    # Expired and past grace, but its fills could not be read this tick: it
    # stays in the ledger rather than settle as zero fills.
    store = vr.StateStore(":memory:")
    store.add_unsettled([_unsettled()])
    client = _FillsClient()
    client.error = errors.SeraError(None, "Service temporarily unavailable", 503)

    assert _positions(client, store, 5000) == {}
    assert [row.order_id for row in store.load_unsettled()] == ["o-1"]

    client.error = None
    client.fills = {"o-1": [_fill("40")]}
    assert _positions(client, store, 5300) == SOLD_40
    assert store.load_unsettled() == []


def test_opposite_fills_net_to_flat_for_quoting_before_they_are_stored():
    store = vr.StateStore(":memory:")
    store.add_unsettled([
        _unsettled("sold", side="ask", price="1.0050", expiration=1000),
        _unsettled("bought", side="bid", price="1.0040", expiration=2000),
    ])
    client = _FillsClient({"sold": [_fill("40")], "bought": [_fill("40")]})

    # The sale is stored; the buy-back is still in flight, so quoting sees flat.
    assert _positions(client, store, 1500) == {}
    assert store.load_positions()["XSGD/USDC"].net_base == Decimal("-40")

    assert _positions(client, store, 2500) == {}
    assert store.load_positions() == {}


# ---------------------------------------------------------------------------
# Placement records every order for settlement
# ---------------------------------------------------------------------------

class _PlacingClient:
    vl_batch_min = 2

    def __init__(self, *, fail_standalone: bool = False) -> None:
        self.fail_standalone = fail_standalone
        self.cancelled: list[str] = []

    def sign_vl_batch(self, legs):
        return SignedVLBatch("batch-1", [
            SignedOrder(f"leg-{i}", 10 + i, {"leg": leg}) for i, leg in enumerate(legs)
        ])

    def send_vl_batch(self, batch):
        records = [OrderRecord(order.order_id, order.uuid_int) for order in batch.orders]
        return VLBatchResult(
            vl_batch_id=batch.vl_batch_id, records=records, amendments=[], cancelled=[])

    def sign_order(self, market, side, qty_base, price, expiration):
        return SignedOrder("solo-1", 7, {"market": market, "side": side})

    def send_order(self, order):
        if self.fail_standalone:
            raise errors.SeraError("INSUFFICIENT_EQUITY", "not enough XSGD", 400)
        return OrderRecord(order.order_id, order.uuid_int)

    def cancel_vl_batch(self, vl_batch_id):
        self.cancelled.append(vl_batch_id)
        return True


def _groups():
    return [
        ("USDC", [
            intent("XSGD/USDC", "bid", "USDC", price="0.99"),
            intent("EURC/USDC", "bid", "USDC", price="1.98"),
        ], True),
        ("XSGD", [intent("XSGD/USDC", "ask", "XSGD", price="1.01")], False),
    ]


def test_placement_records_every_order_with_its_side_price_and_expiry():
    store = vr.StateStore(":memory:")

    placed = _place_groups(_PlacingClient(), _groups(), LOG, state=store)

    assert len(placed) == 2
    assert [
        (row.order_id, row.market, row.side, row.price, row.expiration)
        for row in store.load_unsettled()
    ] == [
        ("leg-0", "XSGD/USDC", "bid", Decimal("0.99"), 999),
        ("leg-1", "EURC/USDC", "bid", Decimal("1.98"), 999),
        ("solo-1", "XSGD/USDC", "ask", Decimal("1.01"), 999),
    ]


def test_a_rejected_order_does_not_take_down_the_rest_of_the_tick():
    # The VL batch is placed, then Sera rejects the standalone ask. The batch
    # stays live, tracked and recorded for settlement; nothing is cancelled.
    store = vr.StateStore(":memory:")
    client = _PlacingClient(fail_standalone=True)

    placed = _place_groups(client, _groups(), LOG, state=store)

    assert [placement.vl_batch_id for placement in placed] == ["batch-1"]
    assert client.cancelled == []
    assert [row.order_id for row in store.load_unsettled()] == ["leg-0", "leg-1"]


def test_a_rejected_vl_batch_does_not_stop_the_standalone_orders():
    class Client(_PlacingClient):
        def send_vl_batch(self, batch):
            raise vr.errors.SeraError("INSUFFICIENT_EQUITY", "not enough USDC", 400)

    store = vr.StateStore(":memory:")
    placed = _place_groups(Client(), _groups(), LOG, state=store)

    assert [placement.kind for placement in placed] == ["standalone"]
    assert [row.order_id for row in store.load_unsettled()] == ["solo-1"]


@pytest.mark.parametrize("lost", [
    httpx.ReadTimeout("timed out"),
    errors.SeraError(None, "Service temporarily unavailable; please retry", 503),
])
def test_an_order_without_an_answer_stays_recorded_and_is_tracked_for_cancel(lost):
    # Sera may have booked these: their rows stay so a fill is counted, and
    # they are tracked so the next cycle cancels them.
    class Client(_PlacingClient):
        def send_vl_batch(self, batch):
            raise lost

        def send_order(self, order):
            raise lost

    store = vr.StateStore(":memory:")
    issues = vr.PlacementIssues()

    placed = _place_groups(Client(), _groups(), LOG, state=store, issues=issues)

    assert placed == []
    assert [row.order_id for row in store.load_unsettled()] == ["leg-0", "leg-1", "solo-1"]
    batch, order = issues.unanswered
    assert (batch.kind, batch.vl_batch_id) == ("vl", "batch-1")
    assert [(o.order_id, o.uuid_int) for o in batch.orders] == [("leg-0", 10), ("leg-1", 11)]
    assert order.kind == "standalone"
    assert [(o.order_id, o.uuid_int, o.market, o.side) for o in order.orders] == [
        ("solo-1", 7, "XSGD/USDC", "ask")]


def test_standalone_placement_is_recorded_for_settlement():
    """A singleton (standalone) order is recorded in the ledger like a VL leg."""
    source = intent("XSGD/USDC", "bid", "USDC", price="0.99")

    store = vr.StateStore(":memory:")
    placements = _place_groups(
        _PlacingClient(),
        [("USDC", [source], False)],
        LOG,
        state=store,
    )

    assert len(placements) == 1
    placement = placements[0]
    assert placement.kind == "standalone"
    assert [
        (row.order_id, row.market, row.side, row.price, row.expiration)
        for row in store.load_unsettled()
    ] == [("solo-1", "XSGD/USDC", "bid", Decimal("0.99"), 999)]


# ---------------------------------------------------------------------------
# Inactive markets
# ---------------------------------------------------------------------------

def test_inactive_market_is_read_from_either_of_seras_reply_shapes():
    bid = intent("XSGD/USDC", "bid", "USDC")
    other = intent("EURC/USDC", "bid", "USDC")
    batch_reply = errors.SeraError(
        None, "Pair XSGD/USDC is not an active trading pair. See GET /markets.", 400)
    order_reply = errors.SeraError("PAIR_INACTIVE", "Invalid request", 400)

    assert vr._inactive_market(batch_reply, [bid, other]) == "XSGD/USDC"
    assert vr._inactive_market(order_reply, [bid]) == "XSGD/USDC"
    # A pair the batch did not contain, or another error, names nothing.
    assert vr._inactive_market(batch_reply, [other]) is None
    assert vr._inactive_market(errors.SeraError("STP_BLOCKED", "x", 400), [bid]) is None


class _InactivePairClient(_PlacingClient):
    """Rejects any VL batch containing an inactive market, as Sera does."""

    def __init__(self, inactive: set[str]) -> None:
        super().__init__()
        self.inactive = inactive
        self.batches: list[list[str]] = []
        self.standalone: list[str] = []

    def send_vl_batch(self, batch):
        legs = [order.wire["leg"] for order in batch.orders]
        self.batches.append([leg.market for leg in legs])
        for leg in legs:
            if leg.market in self.inactive:
                raise errors.SeraError(
                    None, f"Pair {leg.market} is not an active trading pair.", 400)
        return super().send_vl_batch(batch)

    def send_order(self, order):
        self.standalone.append(order.wire["market"])
        return super().send_order(order)


def _bids(*markets: str):
    return ("USDC", [intent(m, "bid", "USDC", price="0.99") for m in markets], True)


def test_a_batch_is_re_sent_without_its_inactive_market():
    client = _InactivePairClient({"JPYC/USDC"})
    issues = vr.PlacementIssues()

    placed = _place_groups(client, [_bids("XSGD/USDC", "JPYC/USDC", "EURC/USDC")], LOG, issues=issues)

    assert client.batches == [
        ["XSGD/USDC", "JPYC/USDC", "EURC/USDC"],
        ["XSGD/USDC", "EURC/USDC"],
    ]
    assert [placement.kind for placement in placed] == ["vl"]
    assert issues.inactive == {"JPYC/USDC"}


def test_legs_left_below_the_batch_minimum_go_out_standalone():
    client = _InactivePairClient({"JPYC/USDC"})

    placed = _place_groups(client, [_bids("XSGD/USDC", "JPYC/USDC")], LOG)

    assert client.batches == [["XSGD/USDC", "JPYC/USDC"]]
    assert client.standalone == ["XSGD/USDC"]
    assert [placement.kind for placement in placed] == ["standalone"]


def test_fills_stuck_settling_an_hour_after_expiry_are_reported_once(caplog):
    store = vr.StateStore(":memory:")
    store.add_unsettled([_unsettled()])
    client = _FillsClient({"o-1": [_fill("40", status="confirming")]})
    stuck: set[str] = set()

    with caplog.at_level(logging.WARNING):
        _positions(client, store, 1000 + 3599, stuck)  # under an hour past expiry: quiet
        positions = _positions(client, store, 1000 + 3600, stuck)
        _positions(client, store, 1000 + 7200, stuck)  # still stuck: not repeated

    assert caplog.text.count("fills still settling 1h 0m after expiry") == 1
    # The fill still holds the return side while it settles.
    assert positions["XSGD/USDC"].net_base == Decimal("-40")
    assert store.load_unsettled() != []


def test_a_normal_order_crossing_the_pairs_return_order_is_left_out():
    # After a bid fill the return ask is priced from a daily mid, the normal bid
    # from Wise's live rate; a move away from the mid can put the bid above it.
    returning = replace(intent("EURC/USDC", "ask", "EURC", price="1.08022"), is_return=True)
    crossing_bid = intent("EURC/USDC", "bid", "USDC", price="1.08070")
    touching_bid = intent("EURC/USDC", "bid", "USDC", price="1.08022")
    lower_bid = intent("EURC/USDC", "bid", "USDC", price="1.08000")

    assert _without_self_cross([crossing_bid, returning], LOG) == [returning]
    assert _without_self_cross([touching_bid, returning], LOG) == [returning]
    assert _without_self_cross([lower_bid, returning], LOG) == [lower_bid, returning]

    # The mirror case: a return bid after an ask fill, and the normal ask.
    return_bid = replace(intent("EURC/USDC", "bid", "USDC", price="1.07900"), is_return=True)
    crossing_ask = intent("EURC/USDC", "ask", "EURC", price="1.07850")
    assert _without_self_cross([return_bid, crossing_ask], LOG) == [return_bid]

    # Without a return order nothing is dropped, even a crossed normal pair.
    normal = [intent("EURC/USDC", "bid", "USDC", price="1.1"), intent("EURC/USDC", "ask", "EURC", price="1.0")]
    assert _without_self_cross(normal, LOG) == normal
