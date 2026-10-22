"""Inventory positions left by fills, and the price of the order that returns them.

A fill moves inventory away from where the operator funded it. Rotor records
that move as a per-market position and quotes it back on the opposite side:

- The return order starts twice `fixed_bps` from the fill price and walks
  linearly to the fill price over the
  return window (48h by default). This is a reverse Dutch auction: rotor asks
  for less profit the longer the position stays open.
- After the window it holds at the fill price until it fills, however long that
  takes. Rotor never returns a position at a loss against its fill price.
- It never quotes closer to the reference mid than `return_bps`, so a market
  that moved in rotor's favour is not overpaid.

Everything here is pure arithmetic on Decimals so it can be audited and tested
without a client, a clock, or a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rotor.algo import BPS


@dataclass(frozen=True)
class Position:
    """Base tokens one market's fills moved, and what rotor paid or got for them."""

    # Base tokens bought minus base tokens sold. Positive after net buying (sell
    # it back with an ask); negative after net selling (buy it back with a bid).
    net_base: Decimal
    # Size-weighted fill price: the breakeven the return order never crosses.
    price: Decimal
    # Size-weighted fill time in POSIX seconds; the return window starts here.
    opened_at: float

    @property
    def return_side(self) -> str:
        """Side of the order that returns this position."""
        return "ask" if self.net_base > 0 else "bid"


def apply_fill(
    position: Position | None,
    side: str,
    qty_base: Decimal,
    price: Decimal,
    ts: float,
) -> Position | None:
    """Return the position after one fill, or None once it is flat."""
    # A bid buys base and an ask sells it.
    signed = qty_base if side == "bid" else -qty_base
    if position is None:
        return Position(signed, price, ts) if signed else None
    total = position.net_base + signed
    if total == 0:
        return None
    if (signed > 0) == (position.net_base > 0):
        # Same direction: the position grows, and its price and clock become
        # size-weighted averages of every fill that built it.
        weight = abs(signed) / abs(total)
        return Position(
            total,
            position.price + (price - position.price) * weight,
            position.opened_at + (ts - position.opened_at) * float(weight),
        )
    if (total > 0) == (position.net_base > 0):
        # Partly returned: the rest keeps its price and clock.
        return Position(total, position.price, position.opened_at)
    # Returned and then some: the excess is a new position at this fill.
    return Position(total, price, ts)


def return_price(
    position: Position,
    mid: Decimal,
    *,
    fixed_bps: Decimal,
    return_bps: Decimal,
    now: float,
    window_seconds: float,
) -> Decimal:
    """Price of the order that returns `position`, before grid rounding."""
    # Share of the return window still to run: 1 at the fill, 0 once it is over.
    # Timestamps are floats; convert once so the price math stays in Decimal.
    elapsed = (Decimal(str(now)) - Decimal(str(position.opened_at))) / Decimal(
        str(window_seconds))
    left = min(Decimal("1"), max(Decimal("0"), 1 - elapsed))
    # Profit still asked for: a full round-trip spread at the fill, none at the end.
    markup = 2 * fixed_bps * left / BPS
    if position.net_base < 0:
        # Sold base: buy it back no higher than the auction price or mid - return_bps.
        return min(position.price * (1 - markup), mid * (1 - return_bps / BPS))
    # Bought base: sell it back no lower than the auction price or mid + return_bps.
    return max(position.price * (1 + markup), mid * (1 + return_bps / BPS))
