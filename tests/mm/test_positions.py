"""Tests for fill-driven positions and their return-order price."""

from __future__ import annotations

from decimal import Decimal

from rotor.mm.positions import Position, apply_fill, return_price

HOUR = 3600.0
WINDOW = 48 * HOUR


def _price(position: Position, mid: str, now: float, *, fixed_bps="50", return_bps="2") -> Decimal:
    return return_price(
        position,
        Decimal(mid),
        fixed_bps=Decimal(fixed_bps),
        return_bps=Decimal(return_bps),
        now=now,
        window_seconds=WINDOW,
    )


def test_a_first_fill_opens_a_position_to_return_on_the_other_side():
    bought = apply_fill(None, "bid", Decimal("100"), Decimal("0.99"), 10.0)
    sold = apply_fill(None, "ask", Decimal("100"), Decimal("1.01"), 10.0)

    assert bought == Position(Decimal("100"), Decimal("0.99"), 10.0)
    assert bought.return_side == "ask"
    assert sold == Position(Decimal("-100"), Decimal("1.01"), 10.0)
    assert sold.return_side == "bid"
    assert apply_fill(None, "bid", Decimal("0"), Decimal("1"), 10.0) is None


def test_same_side_fills_size_weight_the_price_and_the_clock():
    position = Position(Decimal("100"), Decimal("0.99"), 0.0)

    grown = apply_fill(position, "bid", Decimal("300"), Decimal("0.95"), 400.0)

    assert grown == Position(Decimal("400"), Decimal("0.96"), 300.0)


def test_opposite_fills_return_the_position_and_any_excess_flips_it():
    position = Position(Decimal("100"), Decimal("0.99"), 0.0)

    # Partly returned: the rest keeps the breakeven and the clock.
    assert apply_fill(position, "ask", Decimal("40"), Decimal("1.00"), 50.0) == Position(
        Decimal("60"), Decimal("0.99"), 0.0
    )
    # Exactly returned: flat.
    assert apply_fill(position, "ask", Decimal("100"), Decimal("1.00"), 50.0) is None
    # Over-returned: the excess is a new short at this fill.
    assert apply_fill(position, "ask", Decimal("150"), Decimal("1.02"), 50.0) == Position(
        Decimal("-50"), Decimal("1.02"), 50.0
    )


def test_return_bid_walks_from_the_normal_quote_to_the_fill_price_then_holds():
    # Sold base at 1.0050: the ask filled 50 bps over a 1.0000 mid.
    short = Position(Decimal("-100"), Decimal("1.0050"), 0.0)

    # At the fill it bids where the normal bid would be, a round trip below.
    assert _price(short, "1.03", 0.0) == Decimal("1.0050") * Decimal("0.99")
    # Halfway through the window it asks for half that profit.
    assert _price(short, "1.03", 24 * HOUR) == Decimal("1.0050") * Decimal("0.995")
    # From the end of the window on it holds at the fill price, here below a
    # market that moved up, for as long as it takes.
    assert _price(short, "1.03", 48 * HOUR) == Decimal("1.0050")
    assert _price(short, "1.03", 1000 * HOUR) == Decimal("1.0050")


def test_return_ask_walks_up_to_the_fill_price_then_holds():
    # Bought base at 0.9950: the bid filled 50 bps under a 1.0000 mid.
    long = Position(Decimal("100"), Decimal("0.9950"), 0.0)

    assert _price(long, "0.97", 0.0) == Decimal("0.9950") * Decimal("1.01")
    assert _price(long, "0.97", 12 * HOUR) == Decimal("0.9950") * Decimal("1.0075")
    assert _price(long, "0.97", 72 * HOUR) == Decimal("0.9950")


def test_return_order_never_quotes_closer_to_mid_than_return_bps():
    short = Position(Decimal("-100"), Decimal("1.0050"), 0.0)
    long = Position(Decimal("100"), Decimal("0.9950"), 0.0)

    # The market moved in rotor's favour, so the auction price would overpay;
    # the order sits return_bps from mid instead.
    assert _price(short, "0.9900", 48 * HOUR) == Decimal("0.9900") * Decimal("0.9998")
    assert _price(long, "1.0100", 48 * HOUR) == Decimal("1.0100") * Decimal("1.0002")


def test_return_price_treats_a_clock_behind_the_fill_as_the_fill_time():
    short = Position(Decimal("-100"), Decimal("1.0050"), 1000.0)

    assert _price(short, "1.03", 0.0) == _price(short, "1.03", 1000.0)
