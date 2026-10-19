"""Tests for VL grouping, fill settlement, return orders, and placement tracking."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from rotor.mm import vl_runner as vr
from rotor.mm.exchange import errors
from rotor.mm.exchange.client import MarketInfo, OrderRecord, VLBatchResult
from rotor.mm.positions import Position
from rotor.mm.vl_config import BudgetConfig, PairConfig, VLConfig
from rotor.mm.vl_runner import (
    OrderIntent,
    _place_groups,
    _validate_budget_caps,
    group_intents,
)

LOG = logging.getLogger("test")
HOUR = 3600.0


def _intent(
    market: str,
    side: str,
    spent: str,
    *,
    price: str = "1.00",
    qty: str = "100",
    mid: str = "1.01",
) -> OrderIntent:
    return OrderIntent(
        market=market,
        side=side,
        qty_base=Decimal(qty),
        price=Decimal(price),
        expiration=999,
        spent_token=spent,
        benchmark_rate=Decimal(mid),
    )


def test_group_intents_uses_vl_only_for_budgeted_multi_leg_groups():
    groups = group_intents([
        _intent("XSGD/USDC", "bid", "USDC"),
        _intent("EURC/USDC", "bid", "USDC"),
        _intent("XSGD/USDC", "ask", "XSGD"),
    ], {"USDC"}, 2)

    assert groups[0][0] == "USDC"
    assert groups[0][2] is True
    assert [i.market for i in groups[0][1]] == ["XSGD/USDC", "EURC/USDC"]
    assert groups[1][0] == "XSGD"
    assert groups[1][2] is False


def test_validate_budget_caps_rejects_oversized_budgeted_intent():
    cfg = VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("50"))],
        pairs=[],
        fixed_bps=Decimal("8"),
        return_bps=Decimal("1"),
    )

    with pytest.raises(ValueError, match="above configured budget"):
        _validate_budget_caps([
            _intent("XSGD/USDC", "bid", "USDC", price="1.10", qty="100")
        ], cfg)


def _market(*, min_bid_quote: str = "0", min_ask: str = "0") -> MarketInfo:
    return MarketInfo(
        symbol="XSGD/USDC",
        base_symbol="XSGD",
        quote_symbol="USDC",
        base_address="0xbase",
        quote_address="0xquote",
        base_decimals=6,
        quote_decimals=6,
        tick_precision=4,
        quantity_precision=2,
        price_step=Decimal("0.0001"),
        amount_step=Decimal("0.01"),
        min_bid_quote_amount=Decimal(min_bid_quote),
        min_ask_amount=Decimal(min_ask),
    )


def _runtime(info: MarketInfo | None = None) -> vr.PairRuntime:
    return vr.PairRuntime(
        config=PairConfig(
            market="XSGD/USDC",
            price_source="wise",
            qty_base=Decimal("100"),
            fixed_bps=Decimal("50"),
        ),
        info=info or _market(),
        algo=None,
        oracle=None,
        iso_base="SGD",
        iso_quote="USD",
    )


def _vl_config(*, return_bps: str = "2") -> VLConfig:
    return VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("1000"))],
        pairs=[],
        fixed_bps=Decimal("50"),
        return_bps=Decimal(return_bps),
    )


def _normal_quote(mid: str = "1.0000") -> list[OrderIntent]:
    """The normal 50 bps quote around `mid` for 100 XSGD."""
    m = Decimal(mid)
    return [
        _intent("XSGD/USDC", "bid", "USDC", price=str(m * Decimal("0.995")), qty="100", mid=mid),
        _intent("XSGD/USDC", "ask", "XSGD", price=str(m * Decimal("1.005")), qty="100", mid=mid),
    ]


def _with_return(position, intents, *, now, info=None, return_bps="2"):
    return vr._with_return_order(
        intents,
        _runtime(info),
        position,
        _vl_config(return_bps=return_bps),
        now=now,
        log=LOG,
    )


# ---------------------------------------------------------------------------
# Return orders
# ---------------------------------------------------------------------------

def test_short_position_swaps_the_bid_for_a_return_bid_and_keeps_the_ask():
    # Sold 40 XSGD at 1.0050 twelve hours ago; mid has not moved.
    position = Position(Decimal("-40"), Decimal("1.0050"), 0.0)
    intents = _normal_quote()

    out = _with_return(position, intents, now=12 * HOUR)

    bid, ask = out
    # 1.0050 x (1 - 100 bps x 0.75) = 0.9974625, rounded down onto the grid.
    assert (bid.side, bid.qty_base, bid.price) == ("bid", Decimal("40"), Decimal("0.9974"))
    assert ask == intents[1]
    # Everything except size and price is the normal bid's.
    assert (bid.spent_token, bid.expiration, bid.benchmark_rate) == (
        intents[0].spent_token, intents[0].expiration, intents[0].benchmark_rate)


def test_return_order_holds_at_the_fill_price_while_the_market_stays_away():
    # Bought 40 XSGD at 0.9950 three days ago; the market has fallen to 0.9700.
    position = Position(Decimal("40"), Decimal("0.9950"), 0.0)
    intents = _normal_quote("0.9700")

    _bid, ask = _with_return(position, intents, now=72 * HOUR)

    assert (ask.qty_base, ask.price) == (Decimal("40"), Decimal("0.9950"))


def test_return_order_sits_return_bps_from_mid_when_the_market_came_back():
    position = Position(Decimal("-40"), Decimal("1.0050"), 0.0)

    bid, _ask = _with_return(position, _normal_quote("1.0000"), now=48 * HOUR)

    assert bid.price == Decimal("0.9998")


def test_return_order_never_spends_more_than_the_normal_order():
    # A 300 XSGD short, more than one 100 XSGD order: the bid it replaces spent
    # 99.5 USDC, so the return bid buys at most 99.5 / 0.9998 XSGD of it.
    position = Position(Decimal("-300"), Decimal("1.0050"), 0.0)
    intents = _normal_quote()

    bid, _ask = _with_return(position, intents, now=48 * HOUR)

    assert bid.price == Decimal("0.9998")
    assert bid.qty_base == Decimal("99.51")
    assert bid.qty_base * bid.price <= intents[0].qty_base * intents[0].price


def test_position_below_the_market_minimum_keeps_the_normal_order():
    position = Position(Decimal("-0.5"), Decimal("1.0050"), 0.0)
    intents = _normal_quote()

    out = _with_return(position, intents, now=0.0, info=_market(min_bid_quote="1"))

    assert out == intents


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


def _positions(client, store, server_now):
    """One tick's fill read: settle what is final, return positions for quoting."""
    unsettled = store.load_unsettled()
    fills = vr._read_fills(client, [order.order_id for order in unsettled])
    return vr._positions_for_quoting(
        store, unsettled, fills, server_now=server_now, log=LOG)


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


def test_unknown_orders_have_no_fills_but_other_errors_propagate():
    client = _FillsClient()

    client.error = errors.SeraError("CONFLICT", "try again", 409)
    with pytest.raises(errors.SeraError):
        vr._read_fills(client, ["a"])

    client.error = errors.SeraError("NOT_FOUND", "no such order", 404)
    assert vr._read_fills(client, ["a", "b", "a"]) == {"a": [], "b": []}
    assert client.calls == ["a", "a", "b"]


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

    def __init__(self, *, fail_standalone: bool = False, short_ids: bool = False) -> None:
        self.fail_standalone = fail_standalone
        self.short_ids = short_ids
        self.cancelled: list[str] = []

    def place_vl_batch(self, legs):
        ids = [f"leg-{i}" for i in range(len(legs))]
        if self.short_ids:
            ids = ids[:-1]
        records = [OrderRecord(order_id, 10 + i) for i, order_id in enumerate(ids)]
        return VLBatchResult(
            vl_batch_id="batch-1",
            order_ids=ids,
            records=records,
            amendments=[],
            cancelled=[],
            fills=[],
            vl_group=None,
        )

    def place_order_previewed(self, market, side, qty_base, price, expiration):
        if self.fail_standalone:
            raise RuntimeError("placement rejected")
        return OrderRecord(order_id="solo-1", uuid_int=7)

    def cancel_vl_batch(self, vl_batch_id):
        self.cancelled.append(vl_batch_id)
        return True


def _groups():
    return [
        ("USDC", [
            _intent("XSGD/USDC", "bid", "USDC", price="0.99"),
            _intent("EURC/USDC", "bid", "USDC", price="1.98"),
        ], True),
        ("XSGD", [_intent("XSGD/USDC", "ask", "XSGD", price="1.01")], False),
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


def test_orders_a_failed_tick_cancels_are_still_recorded_for_settlement():
    # The VL batch is placed, then the standalone ask fails and the tick cancels
    # the batch; it may already have filled, so it must still be settled.
    store = vr.StateStore(":memory:")
    client = _PlacingClient(fail_standalone=True)

    placed = _place_groups(client, _groups(), LOG, state=store)

    assert placed == []
    assert client.cancelled == ["batch-1"]
    assert [row.order_id for row in store.load_unsettled()] == ["leg-0", "leg-1"]


def test_vl_batch_with_mismatched_ids_attributes_no_fills():
    store = vr.StateStore(":memory:")

    placed = _place_groups(_PlacingClient(short_ids=True), _groups()[:1], LOG, state=store)

    # Still tracked so it can be cancelled, but no leg is trusted to a side.
    assert [order.order_id for order in placed[0].orders] == ["leg-0"]
    assert store.load_unsettled() == []


def test_standalone_placement_tracks_intent_for_settlement():
    """A singleton (standalone) order must be settled like a VL leg.

    Regression: standalone placements once omitted intent_by_order_id, so their
    fills were invisible while VL legs' were. Both paths must record intents.
    """
    source = _intent("XSGD/USDC", "bid", "USDC", price="0.99", mid="1.00")

    class Client:
        vl_batch_min = 2

        def place_order_previewed(self, market, side, qty_base, price, expiration):
            return OrderRecord(order_id="solo-1", uuid_int=7)

    placements = _place_groups(
        Client(),
        [("USDC", [source], False)],
        LOG,
    )

    assert len(placements) == 1
    placement = placements[0]
    assert placement.kind == "standalone"
    assert placement.intent_by_order_id == {"solo-1": source}
    assert vr._unsettled_orders(placement) == [
        vr.UnsettledOrder(
            order_id="solo-1",
            market="XSGD/USDC",
            side="bid",
            price=Decimal("0.99"),
            expiration=999,
            placed_at=placement.orders[0].placed_at,
        )
    ]
