"""Tests for VL grouping, return-to-neutral, and placement tracking helpers."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from rotor.mm.exchange.client import OrderRecord
from rotor.mm.quote_engine import LiveOrder
from rotor.mm.vl_config import BudgetConfig, VLConfig
from rotor.mm.vl_runner import (
    LivePlacement,
    OrderIntent,
    _place_groups,
    _return_sides_from_placement_fills,
    _return_sides_from_polled_fills,
    _validate_budget_caps,
    group_intents,
)


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


def test_placement_fills_mark_opposite_return_side():
    source = _intent("XSGD/USDC", "bid", "USDC", price="0.99", mid="1.00")
    placement = LivePlacement(
        kind="vl",
        spent_token="USDC",
        orders=[LiveOrder("bid", "order-1", 1, 0.0, market="XSGD/USDC")],
        placed_at=0.0,
        vl_batch_id="order-1",
        fills=[{"order_id": "order-1", "trades": [{"quantity": "25"}]}],
        intent_by_order_id={"order-1": source},
    )

    sides = _return_sides_from_placement_fills(
        [placement],
        log=logging.getLogger("test"),
    )

    assert sides == {"XSGD/USDC": {"ask"}}


def test_polled_fills_mark_opposite_side_from_live_order_metadata():
    placement = LivePlacement(
        kind="vl",
        spent_token="USDC",
        orders=[LiveOrder("ask", "entry-1", 1, 0.0, market="XSGD/USDC")],
        placed_at=0.0,
        vl_batch_id="entry-1",
    )

    class Client:
        def get_order_fills(self, order_id):
            assert order_id == "entry-1"
            return [{
                "maker_order_id": "maker-1",
                "taker_order_id": "entry-1",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "quantity": "10",
                "price": "0.99",
            }]

    sides = _return_sides_from_polled_fills(
        Client(), [placement], log=logging.getLogger("test")
    )

    assert sides == {"XSGD/USDC": {"bid"}}


def test_validate_budget_caps_rejects_oversized_budgeted_intent():
    cfg = VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("50"))],
        pairs=[],
        fixed_bps=Decimal("8"),
        return_bps=Decimal("1"),
    )

    with pytest.raises(ValueError, match="above configured budget"):
        _validate_budget_caps([
            ("USDC", [_intent("XSGD/USDC", "bid", "USDC", price="1.10", qty="100")], False)
        ], cfg)


def test_standalone_placement_tracks_intent_for_return_side():
    """A singleton (standalone) entry fill must also tighten the return side.

    Regression: standalone placements previously omitted intent_by_order_id,
    so polled fills on them did not produce return-side behavior while VL legs
    did. Both placement paths must support fill-driven return sides.
    """
    source = _intent("XSGD/USDC", "bid", "USDC", price="0.99", mid="1.00")

    class Client:
        vl_batch_min = 2

        def place_order_previewed(self, market, side, qty_base, price, expiration):
            return OrderRecord(order_id="solo-1", uuid_int=7)

    placements = _place_groups(
        Client(),
        [("USDC", [source], False)],
        logging.getLogger("test"),
    )

    assert len(placements) == 1
    placement = placements[0]
    assert placement.kind == "standalone"
    assert placement.intent_by_order_id == {"solo-1": source}

    class FillClient:
        def get_order_fills(self, order_id):
            assert order_id == "solo-1"
            return [{"quantity": "40", "timestamp": "t", "price": "0.99"}]

    sides = _return_sides_from_polled_fills(
        FillClient(), placements, log=logging.getLogger("test"))
    assert sides == {"XSGD/USDC": {"ask"}}
