"""Tests for Sera's price/size grids and minimum order checks."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rotor.mm.exchange.client import MarketInfo
from rotor.mm.grid import quantize_price, quantize_qty, validate_minimums


def _market(
    *,
    price_step: Decimal | None = Decimal("0.0001"),
    amount_step: Decimal | None = Decimal("0.01"),
    tick_precision: int = 4,
    quantity_precision: int = 2,
    min_bid_quote_amount: Decimal = Decimal("8.80"),
    min_ask_amount: Decimal = Decimal("1"),
) -> MarketInfo:
    return MarketInfo(
        symbol="XSGD/USDC",
        base_symbol="XSGD",
        quote_symbol="USDC",
        base_address="0xbase",
        quote_address="0xquote",
        base_decimals=6,
        quote_decimals=6,
        tick_precision=tick_precision,
        quantity_precision=quantity_precision,
        price_step=price_step,
        amount_step=amount_step,
        min_bid_quote_amount=min_bid_quote_amount,
        min_ask_amount=min_ask_amount,
    )


def test_bids_round_down_asks_round_up_and_sizes_round_down():
    info = _market()

    assert quantize_price(info, Decimal("0.739458"), "bid") == Decimal("0.7394")
    assert quantize_price(info, Decimal("0.740642"), "ask") == Decimal("0.7407")
    assert quantize_qty(info, Decimal("100.009")) == Decimal("100.00")


def test_precision_is_the_fallback_when_steps_are_absent():
    info = _market(price_step=None, amount_step=None, tick_precision=3, quantity_precision=1)

    assert quantize_price(info, Decimal("1.2334"), "bid") == Decimal("1.233")
    assert quantize_price(info, Decimal("1.2351"), "ask") == Decimal("1.236")
    assert quantize_qty(info, Decimal("12.345")) == Decimal("12.3")


@pytest.mark.parametrize(("qty", "bid", "ask", "match"), [
    (Decimal("0"), Decimal("1"), None, "quantity rounds to zero"),
    (Decimal("1"), Decimal("0.74"), None, "bid notional"),
    (Decimal("0.99"), None, Decimal("1"), "ask qty"),
    (Decimal("100"), Decimal("1.01"), Decimal("1.00"), "must be below ask"),
    (Decimal("100"), None, None, "no quoteable side"),
])
def test_orders_below_seras_minimums_or_crossed_are_refused(qty, bid, ask, match):
    with pytest.raises(ValueError, match=match):
        validate_minimums(_market(), qty, bid, ask)


def test_an_order_at_the_minimums_is_accepted():
    validate_minimums(_market(), Decimal("12"), Decimal("0.7334"), Decimal("0.7400"))
