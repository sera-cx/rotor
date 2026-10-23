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
    # Divide by the grid, round to an integer grid count, then scale back.
    return (value / quantum).to_integral_value(rounding=rounding) * quantum


def validate_minimums(
    info: MarketInfo, qty: Decimal, bid_px: Decimal | None, ask_px: Decimal | None
) -> None:
    """Reject orders below Sera's market minimums (or empty, or crossed)."""
    # A zero/negative amount after quantization cannot produce a valid order.
    if qty <= 0:
        raise ValueError("quantity rounds to zero")
    if bid_px is None and ask_px is None:
        raise ValueError("no quoteable side")
    # Bid minimums are quote-token notional minimums: base size times bid price.
    if bid_px is not None and info.min_bid_quote_amount and qty * bid_px < info.min_bid_quote_amount:
        raise ValueError(
            f"bid notional {qty * bid_px} below Sera min {info.min_bid_quote_amount}"
        )
    # Ask minimums are base-token amount minimums.
    if ask_px is not None and info.min_ask_amount and qty < info.min_ask_amount:
        raise ValueError(f"ask qty {qty} below Sera min {info.min_ask_amount}")
    # A crossed quote is invalid and would invert maker economics.
    if bid_px is not None and ask_px is not None and bid_px >= ask_px:
        raise ValueError(f"bid {bid_px} must be below ask {ask_px}")
