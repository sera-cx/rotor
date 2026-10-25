"""Tests for Sera's price/size grids and minimum order checks."""

from __future__ import annotations

from decimal import Decimal

import pytest
from mm_builders import market

from rotor.mm.grid import quantize_price, quantize_qty, validate_minimums


def test_bids_round_down_asks_round_up_and_sizes_round_down():
    info = market()

    assert quantize_price(info, Decimal("0.739458"), "bid") == Decimal("0.7394")
    assert quantize_price(info, Decimal("0.740642"), "ask") == Decimal("0.7407")
    assert quantize_qty(info, Decimal("100.009")) == Decimal("100.00")


def test_precision_is_the_fallback_when_steps_are_absent():
    info = market(price_step=None, amount_step=None, tick_precision=3, quantity_precision=1)

    assert quantize_price(info, Decimal("1.2334"), "bid") == Decimal("1.233")
    assert quantize_price(info, Decimal("1.2351"), "ask") == Decimal("1.236")
    assert quantize_qty(info, Decimal("12.345")) == Decimal("12.3")


@pytest.mark.parametrize(("side", "qty", "price", "match"), [
    ("bid", Decimal("0"), Decimal("1"), "quantity rounds to zero"),
    ("bid", Decimal("1"), Decimal("0.74"), "bid notional"),
    ("ask", Decimal("0.99"), Decimal("1"), "ask qty"),
])
def test_orders_below_seras_minimums_are_refused(side, qty, price, match):
    with pytest.raises(ValueError, match=match):
        validate_minimums(market(min_bid_quote="8.80", min_ask="1"), side, qty, price)


def test_an_order_at_the_minimums_is_accepted():
    validate_minimums(market(min_bid_quote="8.80", min_ask="1"), "bid", Decimal("12"), Decimal("0.7334"))
    validate_minimums(market(min_bid_quote="8.80", min_ask="1"), "ask", Decimal("12"), Decimal("0.7400"))
