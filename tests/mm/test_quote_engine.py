"""Unit tests for shared quote-engine helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rotor.mm import quote_engine as qe
from rotor.mm.exchange.client import MarketInfo
from rotor.security import key as wallet_key


def _market() -> MarketInfo:
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
    )


def test_build_algo_returns_simple_algo():
    algo = qe.build_algo(
        qty_base=Decimal("100"),
        fixed_bps=Decimal("8"),
    )

    quote = qe.quote(algo, _market(), Decimal("1"))
    assert algo.name == "simple_market_making"
    assert quote.bid_price == Decimal("0.9992")
    assert quote.ask_price == Decimal("1.0008")


def test_build_client_dry_run_uses_zero_address_when_wallet_locked(monkeypatch):
    wallet_key.lock()
    monkeypatch.delenv("SERA_PRIVATE_KEY", raising=False)

    client = qe.build_client(dry_run=True, log=__import__("logging").getLogger("test"))

    assert client.owner_address == qe.ZERO_ADDRESS
    with pytest.raises(RuntimeError, match="dry-run signer"):
        client._signer({}, {}, {})


def test_build_client_dry_run_uses_unlocked_wallet_address(monkeypatch):
    wallet_key.lock()
    private_key = "0x" + ("aa" * 32)
    monkeypatch.setenv("SERA_PRIVATE_KEY", private_key)
    wallet_key.unlock()

    try:
        client = qe.build_client(dry_run=True, log=__import__("logging").getLogger("test"))
        assert client.owner_address == wallet_key.wallet_address().lower()
    finally:
        wallet_key.lock()


def test_all_requote_safe_uses_boundary(monkeypatch):
    monkeypatch.setattr(qe.time, "time", lambda: 100.0)
    orders = [qe.LiveOrder("bid", "id", 1, placed_at=50.0)]

    assert qe.all_requote_safe(orders, min_requote_seconds=50.0) is True
    assert qe.all_requote_safe(orders, min_requote_seconds=50.1) is False


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00Z",
        "2026-01-01T08:00:00+08:00",
        1767225600,
    ],
)
def test_parse_ts_accepts_common_api_timestamp_shapes(value):
    assert qe.parse_ts(value) == pytest.approx(1767225600.0)


def test_parse_ts_returns_none_for_bad_values():
    assert qe.parse_ts("") is None
    assert qe.parse_ts("not-a-date") is None


def test_fmt_helpers_are_compact():
    assert qe.fmt(Decimal("100.000")) == "100"
    assert qe.fmt_optional(None) == "-"
    assert qe.fmt_optional(Decimal("1.2300")) == "1.23"


def test_stop_flag_request_and_wait():
    flag = qe.StopFlag()
    assert flag.requested is False
    flag.request()
    assert flag.requested is True
    # Already requested: wait returns True immediately without blocking.
    assert flag.wait(60.0) is True


def test_sleep_remaining_interruptible_does_not_call_time_sleep(monkeypatch):
    def boom(_seconds):
        raise AssertionError("sleep_remaining must not block when a stop is given")

    monkeypatch.setattr(qe.time, "sleep", boom)
    flag = qe.StopFlag()
    flag.request()

    # With a requested stop, sleep_remaining waits on the event (returns at once)
    # instead of calling time.sleep — so the loop wakes promptly on SIGTERM.
    qe.sleep_remaining(qe.time.time(), 100.0, flag)


def test_a_second_stop_signal_forces_the_stop():
    import signal

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    stop = qe.StopFlag()
    try:
        stop.install()
        signal.raise_signal(signal.SIGTERM)
        assert (stop.requested, stop.forced) == (True, False)
        signal.raise_signal(signal.SIGINT)
        assert stop.forced is True
        assert stop.wait_forced(5.0) is True
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
