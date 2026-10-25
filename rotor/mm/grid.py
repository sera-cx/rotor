"""Sera's price and size grids, and its minimum order sizes.

Pure Decimal helpers with no network or wallet access: every order rotor
builds goes through them, so they are kept small and separately tested.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from rotor.mm.exchange.client import MarketInfo

# One basis point is 1/10,000; keeping this as Decimal avoids float drift in
# financial arithmetic.
BPS = Decimal("10000")


def quantize_price(info: MarketInfo, value: Decimal, side: str) -> Decimal:
    """Snap a raw price onto Sera's price grid with side-aware rounding.

    Bids round down and asks round up, so rounding never quotes more
    aggressively than the price asked for.
    """
    rounding = ROUND_DOWN if side == "bid" else ROUND_UP
    return _quantize_step(value, info.price_step, info.tick_precision, rounding)


def quantize_qty(info: MarketInfo, value: Decimal) -> Decimal:
    """Snap a raw base quantity onto Sera's amount grid, rounding down."""
    return _quantize_step(value, info.amount_step, info.quantity_precision, ROUND_DOWN)


def _quantize_step(
    value: Decimal,
    step: Decimal | None,
    precision: int,
    rounding,
) -> Decimal:
    """Snap `value` to an explicit step, or to precision-derived powers of ten."""
    # Newer market metadata can provide an exact step; older metadata may only
    # provide decimal precision, so derive a quantum like 0.01 from precision=2.
    quantum = step or Decimal("1").scaleb(-precision)
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


def validate_minimums(info: MarketInfo, side: str, qty: Decimal, price: Decimal) -> None:
    """Reject an order below Sera's market minimum for its side (or empty)."""
    if qty <= 0:
        raise ValueError("quantity rounds to zero")
    # Bid minimums are quote-token notional minimums: base size times bid price.
    if side == "bid" and qty * price < info.min_bid_quote_amount:
        raise ValueError(f"bid notional {qty * price} below Sera min {info.min_bid_quote_amount}")
    # Ask minimums are base-token amount minimums.
    if side == "ask" and qty < info.min_ask_amount:
        raise ValueError(f"ask qty {qty} below Sera min {info.min_ask_amount}")
