"""Run-loop integration tests for the VL market maker."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from types import SimpleNamespace

import httpx

from rotor.mm import vl_runner as vr
from rotor.mm.exchange.client import (
    MarketInfo,
    OrderRecord,
    SignedOrder,
    SignedVLBatch,
    VLBatchResult,
)
from rotor.mm.exchange.errors import SeraError, SeraRateLimit, SeraUnavailable
from rotor.mm.positions import Position
from rotor.mm.vl_config import BudgetConfig, PairConfig, VLConfig
from rotor.price_reference import ReferenceTooStale, ReferenceUnavailable
from rotor.price_reference.models import PriceObservation, ReferencePrice
from rotor.updater import GitUpdateResult


def _pair(market: str, *, qty: str = "10", fixed_bps: str = "50") -> PairConfig:
    return PairConfig(
        market=market,
        price_source="wise",
        qty_base=Decimal(qty),
        fixed_bps=Decimal(fixed_bps),
    )


def _config(*, pairs: list[PairConfig] | None = None) -> VLConfig:
    return VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("100000"))],
        pairs=pairs or [_pair("XSGD/USDC"), _pair("EURC/USDC")],
        fixed_bps=Decimal("50"),
        return_bps=Decimal("1"),
    )


def _market(symbol: str, base: str, quote: str) -> MarketInfo:
    return MarketInfo(
        symbol=symbol,
        base_symbol=base,
        quote_symbol=quote,
        base_address=f"0x{base.lower()}",
        quote_address=f"0x{quote.lower()}",
        base_decimals=6,
        quote_decimals=6,
        tick_precision=4,
        quantity_precision=2,
        price_step=Decimal("0.0001"),
        amount_step=Decimal("0.01"),
        min_bid_quote_amount=Decimal("0"),
        min_ask_amount=Decimal("0"),
    )


class _SequencedSource:
    name = "test-source"

    def __init__(
        self,
        rates: dict[tuple[str, str], list[str]],
        *,
        fail_first_quotes: int = 0,
        always_fail: set[tuple[str, str]] | None = None,
    ) -> None:
        self.rates = {
            pair: [Decimal(value) for value in values]
            for pair, values in rates.items()
        }
        self.calls: dict[tuple[str, str], int] = {}
        self.fail_first_quotes = fail_first_quotes
        self.always_fail = {
            (b.upper(), q.upper()) for b, q in (always_fail or set())
        }

    def quote(self, base: str, quote: str, *, amount=None) -> PriceObservation:
        if (base.upper(), quote.upper()) in self.always_fail:
            raise RuntimeError("source unavailable for pair")
        if self.fail_first_quotes > 0:
            self.fail_first_quotes -= 1
            raise RuntimeError("source unavailable")
        pair = (base.upper(), quote.upper())
        idx = self.calls.get(pair, 0)
        self.calls[pair] = idx + 1
        values = self.rates[pair]
        rate = values[min(idx, len(values) - 1)]
        return PriceObservation(
            source=self.name,
            pair=f"{pair[0]}/{pair[1]}",
            rate=rate,
            ts=time.time(),
        )


    # Also stands in for Wise's after-fee rates, where normal quotes are placed:
    # fresh, at the latest rate quoted, 50 bps each way by default.
    sell_fee = Decimal("0.0050")
    buy_fee = Decimal("0.0050")
    default_max_age_s = 600.0

    def sell_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        return self._settlement(base, quote, 1 - self.sell_fee)

    def buy_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        return self._settlement(base, quote, 1 + self.buy_fee)

    def mid_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        return self._settlement(base, quote, Decimal("1"))

    def _settlement(self, base: str, quote: str, factor: Decimal) -> PriceObservation:
        pair = (base.upper(), quote.upper())
        if pair in self.always_fail:
            # A pair marked as failing has no Wise rate either.
            raise RuntimeError("source unavailable for pair")
        values = self.rates[pair]
        rate = values[min(max(self.calls.get(pair, 1) - 1, 0), len(values) - 1)]
        return PriceObservation(
            source="wise", pair=f"{pair[0]}/{pair[1]}", rate=rate * factor, ts=time.time())


class _StopFlag:
    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        return None

    def request(self) -> None:
        self.requested = True

    def wait(self, _timeout: float) -> bool:
        return self.requested

    forced = False

    def wait_forced(self, _timeout: float) -> bool:
        return self.forced


class _FakeClient:
    vl_batch_min = 2
    vl_batch_max = 50
    owner_address = "0x" + "11" * 20
    eip712_domain = {
        "chainId": 11155111,
        "verifyingContract": "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e",
    }

    def __init__(
        self,
        *,
        open_orders: list[dict] | None = None,
        fill_first_xsgd_bid: bool = False,
        cancel_ok: bool = True,
        fill_status: str = "settled",
        reject_asks: bool = False,
    ) -> None:
        self.markets_by_symbol = {
            "XSGD/USDC": _market("XSGD/USDC", "XSGD", "USDC"),
            "EURC/USDC": _market("EURC/USDC", "EURC", "USDC"),
        }
        self.open_orders = list(open_orders or [])
        self.fill_first_xsgd_bid = fill_first_xsgd_bid
        # When False, cancels report Sera's cooldown (return False, order stays).
        self.cancel_ok = cancel_ok
        self.events: list[tuple] = []
        self.active_orders: set[str] = set()
        # Every order Sera ever booked; fills for any other id are a 404.
        self.booked: set[str] = set()
        self.batch_orders: dict[str, list[str]] = {}
        self.batch_places: list[dict] = []
        self.standalone_places: list[dict] = []
        self.fill_order_ids: set[str] = set()
        self.fill_status = fill_status
        # Asks spend base tokens; a wallet funded only with USDC cannot back them.
        self.reject_asks = reject_asks
        # Raised by send_vl_batch() after Sera books the batch, one per call:
        # the answer is lost on the way back.
        self.send_errors: list[Exception] = []
        # Raised by send_vl_batch() before the batch reaches Sera, one per call.
        self.unsent_errors: list[Exception] = []
        # Markets Sera rejects orders for as not active (still listed).
        self.inactive_markets: set[str] = set()
        self.inactive_rejections: list[str] = []
        # Raised by bootstrap() in order, one per call, until the list is empty.
        self.bootstrap_errors: list[Exception] = []
        # Vault balances by symbol; None means GET /balances fails.
        self.balances: dict[str, Decimal] | None = None
        # Fill reads for this market's orders fail (rate-limited) on these ticks.
        self.unreadable_market: str | None = None
        self.unreadable_ticks: range = range(0)
        self._batch_count = 0
        self._order_count = 0
        self._uuid_int = 100

    def bootstrap(self) -> None:
        self.events.append(("bootstrap",))
        if self.bootstrap_errors:
            raise self.bootstrap_errors.pop(0)

    def get_open_orders(self) -> list[dict]:
        self.events.append(("open_orders",))
        return list(self.open_orders)

    def expiration_from_server_time(self, order_ttl_seconds: int) -> int:
        self.events.append(("expiration", order_ttl_seconds))
        # Sera's clock follows the local one, which tests may fake.
        return int(time.time()) + order_ttl_seconds

    def sign_vl_batch(self, legs) -> SignedVLBatch:
        self._batch_count += 1
        batch_id = f"batch-{self._batch_count}"
        orders = []
        for idx, leg in enumerate(legs):
            self._uuid_int += 1
            orders.append(SignedOrder(f"{batch_id}-leg-{idx}", self._uuid_int, {"leg": leg}))
        return SignedVLBatch(batch_id, orders)

    def send_vl_batch(self, batch: SignedVLBatch) -> VLBatchResult:
        legs = [order.wire["leg"] for order in batch.orders]
        for leg in legs:
            if leg.market in self.inactive_markets:
                self.inactive_rejections.append(leg.market)
                # Sera's VL route relays the matching engine's message, no code.
                raise SeraError(None, f"Pair {leg.market} is not an active trading "
                                "pair. See GET /markets for available pairs.", 400)
        if self.unsent_errors:
            self.events.append(("unsent_vl", batch.vl_batch_id))
            raise self.unsent_errors.pop(0)
        if self.send_errors:
            self.events.append(("lost_vl", batch.vl_batch_id))
            self._book_vl(batch, legs)
            raise self.send_errors.pop(0)
        return self._book_vl(batch, legs)

    def _book_vl(self, batch: SignedVLBatch, legs) -> VLBatchResult:
        batch_id = batch.vl_batch_id
        records = [OrderRecord(order.order_id, order.uuid_int) for order in batch.orders]
        rows = []
        for record, leg in zip(records, legs, strict=True):
            self.active_orders.add(record.order_id)
            self.booked.add(record.order_id)
            rows.append({
                "market": leg.market,
                "side": leg.side,
                "price": leg.price,
                "qty": leg.qty_base,
                "order_id": record.order_id,
            })
            if (
                self.fill_first_xsgd_bid
                and batch_id == "batch-1"
                and leg.market == "XSGD/USDC"
                and leg.side == "bid"
            ):
                self.fill_order_ids.add(record.order_id)
        self.batch_orders[batch_id] = [record.order_id for record in records]
        self.batch_places.append({"batch_id": batch_id, "legs": rows})
        self.events.append(("place_vl", batch_id))
        return VLBatchResult(
            vl_batch_id=batch_id,
            order_ids=[record.order_id for record in records],
            records=records,
            amendments=[],
            cancelled=[],
            fills=[],
            vl_group=None,
        )

    def sign_order(
        self,
        market: str,
        side: str,
        qty_base: Decimal,
        price: Decimal,
        expiration: int,
    ) -> SignedOrder:
        self._order_count += 1
        self._uuid_int += 1
        return SignedOrder(f"solo-{self._order_count}", self._uuid_int, {
            "market": market,
            "side": side,
            "qty": qty_base,
            "price": price,
            "expiration": expiration,
        })

    def send_order(self, order: SignedOrder) -> OrderRecord:
        market, side = order.wire["market"], order.wire["side"]
        if market in self.inactive_markets:
            self.inactive_rejections.append(market)
            raise SeraError("PAIR_INACTIVE", "Invalid request", 400)
        if self.reject_asks and side == "ask":
            self.events.append(("rejected", market, side))
            raise SeraError("INSUFFICIENT_EQUITY", "available equity below the order", 400)
        self.active_orders.add(order.order_id)
        self.booked.add(order.order_id)
        self.standalone_places.append({**order.wire, "order_id": order.order_id})
        self.events.append(("place_order", order.order_id, market, side))
        return OrderRecord(order.order_id, order.uuid_int)

    def get_vault_balances(self) -> dict[str, Decimal]:
        self.events.append(("balances",))
        if self.balances is None:
            raise RuntimeError("balances unavailable")
        return dict(self.balances)

    def market_of(self, order_id: str) -> str | None:
        rows = [leg for batch in self.batch_places for leg in batch["legs"]]
        return next(
            (row["market"] for row in rows + self.standalone_places if row["order_id"] == order_id),
            None,
        )

    def get_order_fills(self, order_id: str) -> list[dict]:
        self.events.append(("fills", order_id))
        tick = len([event for event in self.events if event[0] == "expiration"])
        if tick in self.unreadable_ticks and self.market_of(order_id) == self.unreadable_market:
            raise SeraRateLimit("RATE_LIMITED", "slow down", 429)
        if order_id not in self.booked:
            # Sera's own answer for an order it has no record of.
            raise SeraError(None, "Order not found", 404)
        if order_id in self.fill_order_ids:
            # Like Sera, every call lists all of the order's fills so far.
            return [{"quantity": "5", "price": "1.0", "settlement_status": self.fill_status}]
        return []

    def cancel_vl_batch(self, vl_batch_id: str) -> bool:
        self.events.append(("cancel_vl", vl_batch_id))
        if not self.cancel_ok:
            return False
        if vl_batch_id not in self.batch_orders:
            raise SeraError(None, f"VL batch not found: {vl_batch_id}", 400)
        for order_id in self.batch_orders.get(vl_batch_id, []):
            self.active_orders.discard(order_id)
        return True

    def cancel_order(self, order_id: str, uuid_int: int) -> bool:
        self.events.append(("cancel_order", order_id, uuid_int))
        if not self.cancel_ok:
            return False
        self.active_orders.discard(order_id)
        self.open_orders = [row for row in self.open_orders if row.get("order_id") != order_id]
        return True


def _wire_runner(monkeypatch, *, config: VLConfig, client: _FakeClient, source) -> None:
    monkeypatch.setattr(vr, "load_vl_config", lambda _path: config)
    monkeypatch.setattr(vr, "build_client", lambda **_kwargs: client)
    monkeypatch.setattr(vr, "load_price_reference_source", lambda _name: source)
    monkeypatch.setattr(vr, "load_token_to_iso", lambda: {
        "XSGD": "SGD",
        "EURC": "EUR",
        "USDC": "USD",
    })


def _stop_after_sleeps(monkeypatch, count: int) -> list[float]:
    stop = _StopFlag()
    sleeps: list[float] = []

    def fake_sleep(_started: float, poll_seconds: float, _stop=None) -> None:
        sleeps.append(poll_seconds)
        if len(sleeps) >= count:
            stop.requested = True

    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    monkeypatch.setattr(vr, "sleep_remaining", fake_sleep)
    return sleeps


HOUR = 3600.0
# A fixed start for faked clocks: well past any real order's expiry.
T0 = 1_800_000_000.0


def _fake_clock(monkeypatch, *, steps: list[float]) -> dict[str, float]:
    """Fake the clock and stop the run loop after one sleep per step.

    Each sleep moves time forward by the next step, so tick n runs at
    T0 + sum(steps[:n]).
    """
    clock = {"now": T0}
    stop = _StopFlag()
    pending = list(steps)

    def fake_sleep(_started: float, _poll_seconds: float, _stop=None) -> None:
        clock["now"] += pending.pop(0)
        if not pending:
            stop.requested = True

    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])
    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    monkeypatch.setattr(vr, "sleep_remaining", fake_sleep)
    return clock


def _xsgd_asks(client: _FakeClient) -> list[tuple[Decimal, Decimal]]:
    return [
        (row["qty"], row["price"]) for row in client.standalone_places
        if row["market"] == "XSGD/USDC" and row["side"] == "ask"
    ]


def test_run_returns_a_fill_through_the_auction_then_holds_at_the_fill_price(
    monkeypatch, tmp_path,
):
    # Tick 1's XSGD bid buys 5 XSGD at 0.9950. Ticks run at 0h, 12h, 24h and
    # 60h; SGD falls from 1.0000 to 0.9800 before the third tick.
    client = _FakeClient(fill_first_xsgd_bid=True)
    source = _SequencedSource({
        ("SGD", "USD"): ["1.0000", "1.0000", "0.9800", "0.9800"],
        ("EUR", "USD"): ["2.0000"],
    })
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[12 * HOUR, 12 * HOUR, 36 * HOUR, 1.0])

    result = vr.run(
        config_path="vl.toml",
        poll_seconds=305.0,
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    assert _xsgd_asks(client) == [
        # 0h: the normal ask, 50 bps over mid.
        (Decimal("10"), Decimal("1.0050")),
        # 12h: the settled fill is now a position. The ask sells back only the
        # 5 XSGD bought, 75 bps over the fill price (a quarter of the window gone).
        (Decimal("5"), Decimal("1.0025")),
        # 24h: halfway, 50 bps over the fill price, though the market fell to 0.98.
        (Decimal("5"), Decimal("1.0000")),
        # 60h: past the window, it holds at the fill price above the market.
        (Decimal("5"), Decimal("0.9950")),
    ]
    # The bid side keeps quoting normally around the latest mid throughout.
    xsgd_bids = [
        leg["price"] for batch in client.batch_places for leg in batch["legs"]
        if leg["market"] == "XSGD/USDC" and leg["side"] == "bid"
    ]
    assert xsgd_bids == [
        Decimal("0.9950"), Decimal("0.9950"), Decimal("0.9751"), Decimal("0.9751")]

    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        assert store.load_positions() == {
            "XSGD/USDC": Position(Decimal("5"), Decimal("0.9950"), T0)
        }
    finally:
        store.close()


def test_run_counts_a_fill_on_a_batch_whose_answer_was_lost(monkeypatch, tmp_path):
    # Sera books tick 1's USDC bid batch, but its answer times out on the way
    # back, and a taker fills 5 of its XSGD bid. Rotor recorded the batch before
    # sending it, so tick 2 cancels it and the fill still becomes a position
    # that is returned at its price, as if the answer had arrived.
    client = _FakeClient(fill_first_xsgd_bid=True)
    client.send_errors = [httpx.ReadTimeout("timed out")]
    source = _SequencedSource({
        ("SGD", "USD"): ["1.0000", "1.0000", "0.9800"],
        ("EUR", "USD"): ["2.0000"],
    })
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[12 * HOUR, 12 * HOUR, 1.0])

    result = vr.run(
        config_path="vl.toml",
        poll_seconds=305.0,
        min_requote_seconds=0.0,
        log_level="CRITICAL",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    assert ("lost_vl", "batch-1") in client.events
    assert ("cancel_vl", "batch-1") in client.events
    assert _xsgd_asks(client)[1:] == [
        (Decimal("5"), Decimal("1.0025")),
        # The market fell to 0.98; the ask still does not sell below the fill.
        (Decimal("5"), Decimal("1.0000")),
    ]
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        assert store.load_positions() == {
            "XSGD/USDC": Position(Decimal("5"), Decimal("0.9950"), T0)
        }
    finally:
        store.close()


def test_run_drops_a_batch_that_never_reached_sera_and_keeps_quoting(
    monkeypatch, tmp_path, caplog,
):
    # Tick 1's USDC bid batch fails to connect, so Sera never books it. Tick 2's
    # cancel gets Sera's "not found", drops it, and quotes as normal; its
    # ledger rows settle at zero fills once the orders would have expired.
    client = _FakeClient()
    client.unsent_errors = [httpx.ConnectError("connection refused")]
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[305.0, 600.0, 1.0])

    result = vr.run(
        config_path="vl.toml",
        poll_seconds=305.0,
        min_requote_seconds=0.0,
        log_level="CRITICAL",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    assert ("cancel_vl", "batch-1") in client.events
    # Its two legs settle with no fills, each with a warning saying why.
    assert caplog.text.count("Sera has no record of it; settled with no fills") == 2
    assert _markets_quoted_per_tick(client)[1:] == [
        {"XSGD/USDC", "EURC/USDC"}, {"XSGD/USDC", "EURC/USDC"}]
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        assert not any(row.order_id.startswith("batch-1-") for row in store.load_unsettled())
        assert store.load_positions() == {}
    finally:
        store.close()


def test_a_mainnet_run_refuses_the_state_file_of_a_testnet_run(monkeypatch, tmp_path):
    # A testnet run buys 5 XSGD and stores the position. The operator then
    # points the same checkout at mainnet: that run must not quote the testnet
    # position back on mainnet, so it refuses the file before placing anything.
    state_path = tmp_path / "state.sqlite"
    testnet = _FakeClient(fill_first_xsgd_bid=True)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=testnet, source=source)
    _fake_clock(monkeypatch, steps=[HOUR, 1.0])
    vr.run(config_path="vl.toml", min_requote_seconds=0.0, log_level="CRITICAL",
           state_path=state_path)

    mainnet = _FakeClient()
    mainnet.eip712_domain = {
        "chainId": 1, "verifyingContract": "0xB5C50C5D5f038404F85970b7f5B7259C4AC0E198"}
    notifier = _RecordingNotifier()
    _wire_runner(monkeypatch, config=_config(), client=mainnet, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)

    result = vr.run(config_path="vl.toml", min_requote_seconds=0.0, once=True,
                    telegram=True, log_level="CRITICAL", state_path=state_path)

    assert result == 1
    assert mainnet.batch_places == [] and mainnet.standalone_places == []
    assert len(notifier.containing("on chain 11155111")) == 1
    store = vr.StateStore(state_path)
    try:
        assert store.load_positions() == {
            "XSGD/USDC": Position(Decimal("5"), Decimal("0.9950"), T0)}
    finally:
        store.close()


def test_run_quotes_at_wise_prices_and_returns_through_the_auction(monkeypatch, tmp_path):
    # Wise: selling SGD costs 50 bps, buying it 80 bps. Normal quotes sit at
    # those prices; after tick 1's XSGD bid fills at 0.9950, the return ask
    # follows the auction instead, inside Wise's price, back toward the fill.
    client = _FakeClient(fill_first_xsgd_bid=True)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    source.buy_fee = Decimal("0.0080")
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[12 * HOUR, 1.0])

    vr.run(config_path="vl.toml", min_requote_seconds=0.0, log_level="CRITICAL",
           state_path=tmp_path / "state.sqlite")

    assert _xsgd_asks(client)[:2] == [
        (Decimal("10"), Decimal("1.0080")),
        # A quarter of the window gone: 0.9950 x (1 + 100 bps x 3/4).
        (Decimal("5"), Decimal("1.0025")),
    ]
    xsgd_bids = [
        leg["price"] for batch in client.batch_places for leg in batch["legs"]
        if leg["market"] == "XSGD/USDC" and leg["side"] == "bid"
    ]
    assert set(xsgd_bids) == {Decimal("0.9950")}


def test_fire_once_runs_settle_the_previous_runs_fills(monkeypatch, tmp_path):
    # Each fire-once run is a new process: fills on the last run's orders must
    # still reach a position through the state file.
    client = _FakeClient(fill_first_xsgd_bid=True)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    clock = {"now": T0}
    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)
    state_path = tmp_path / "state.sqlite"

    def fire() -> int:
        return vr.run(
            config_path="vl.toml",
            once=True,
            min_requote_seconds=0.0,
            log_level="ERROR",
            state_path=state_path,
        )

    assert fire() == 0
    clock["now"] += HOUR
    assert fire() == 0

    # One hour into the window: 0.9950 x (1 + 100 bps x 47/48), rounded up.
    assert _xsgd_asks(client) == [
        (Decimal("10"), Decimal("1.0050")),
        (Decimal("5"), Decimal("1.0048")),
    ]


def test_fire_once_runs_share_the_stall_alert_and_report_when_quoting_resumes(
    monkeypatch, tmp_path,
):
    # The vault is empty, so every fire places nothing. Fires run 305 s apart;
    # the one 915 s after the stall began alerts, the next does not repeat it,
    # and the first fire that places again says quoting resumed.
    client = _FakeClient()
    client.balances = {}
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    clock = {"now": T0}
    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    def fire() -> int:
        result = vr.run(config_path="vl.toml", once=True, min_requote_seconds=0.0,
                        telegram=True, log_level="CRITICAL",
                        state_path=tmp_path / "state.sqlite")
        clock["now"] += 305.0
        return result

    stalled = [fire() for _ in range(5)]
    alerts = len(notifier.containing("VL quoting stalled"))
    client.balances = None
    resumed = [fire(), fire()]

    assert stalled == [1, 1, 1, 1, 1]
    assert alerts == 1
    assert resumed == [0, 0]
    assert len(notifier.containing("VL quoting stalled")) == 1
    assert len(notifier.containing("quoting resumed after 25m")) == 1


def test_run_alerts_on_a_stall_and_reports_when_quoting_resumes(monkeypatch):
    # Ticks every 305 s on an empty vault; the vault is funded before tick 6.
    client = _FakeClient()
    client.balances = {}
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    clock = _fake_clock(monkeypatch, steps=[305.0] * 6)
    real_sleep = vr.sleep_remaining

    def fund_before_tick_6(started, poll, stop=None):
        real_sleep(started, poll, stop)
        if clock["now"] == T0 + 5 * 305.0:
            client.balances = None

    monkeypatch.setattr(vr, "sleep_remaining", fund_before_tick_6)

    vr.run(config_path="vl.toml", min_requote_seconds=0.0, telegram=True,
           state_path=":memory:")

    assert len(notifier.containing("VL quoting stalled")) == 1
    assert len(notifier.containing("quoting resumed after 25m")) == 1


def test_fire_once_runs_alert_when_one_pair_stops_quoting_and_when_it_is_back(
    monkeypatch, tmp_path,
):
    # XSGD/USDC's price source fails while EURC/USDC keeps quoting, so nothing
    # stalls overall. Fires run 305 s apart: the one 15 minutes into the outage
    # alerts once, and the first fire after the source recovers says so.
    client = _FakeClient()
    notifier = _RecordingNotifier()
    source = _SequencedSource(
        {("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]},
        always_fail={("SGD", "USD")},
    )
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    clock = {"now": T0}
    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    def fire() -> int:
        result = vr.run(config_path="vl.toml", once=True, min_requote_seconds=0.0,
                        telegram=True, log_level="CRITICAL",
                        state_path=tmp_path / "state.sqlite")
        clock["now"] += 305.0
        return result

    outage = [fire() for _ in range(5)]
    alerts = notifier.containing("Not quoting these pairs")
    source.always_fail = set()
    fire()
    fire()

    assert outage == [0, 0, 0, 0, 0]
    assert [text for text, _ in alerts] == [
        "⚠️ Not quoting these pairs; the other pairs are quoting:\n"
        "XSGD/USDC for 15m: SGD/USD: test-source failed "
        "(RuntimeError: source unavailable for pair); Wise's mid failed too "
        "(RuntimeError: source unavailable for pair)"
    ]
    assert len(notifier.containing("Not quoting these pairs")) == 1
    assert [text for text, _ in notifier.containing("Quoting again")] == [
        "✅ Quoting again:\nXSGD/USDC after 25m"]
    assert not notifier.containing("VL quoting stalled")


def test_run_holds_the_return_side_from_the_tick_after_a_fill(monkeypatch, tmp_path):
    # At the normal cadence the filled order is still resting (and unexpired) on
    # the next tick; its fill already decides that tick's ask.
    client = _FakeClient(fill_first_xsgd_bid=True)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[305.0, 1.0])

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    # 305 s into the window the auction is still at the normal ask's price; only
    # the size shows it is the return order for the 5 XSGD bought.
    assert _xsgd_asks(client) == [
        (Decimal("10"), Decimal("1.0050")),
        (Decimal("5"), Decimal("1.0050")),
    ]


def test_run_holds_a_fill_that_is_still_settling_without_storing_it(monkeypatch, tmp_path):
    client = _FakeClient(fill_first_xsgd_bid=True, fill_status="confirming")
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[12 * HOUR, 1.0])

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    # The fill holds the ask as soon as it is visible...
    assert _xsgd_asks(client) == [
        (Decimal("10"), Decimal("1.0050")),
        (Decimal("5"), Decimal("1.0025")),
    ]
    # ...but only a settled fill is written to the stored position.
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        assert store.load_positions() == {}
        assert "batch-1-leg-0" in {row.order_id for row in store.load_unsettled()}
    finally:
        store.close()


def test_run_places_a_vl_batch_whose_legs_each_use_the_whole_budget(monkeypatch):
    # The Earn setup sizes every pair to the whole stablecoin budget and relies
    # on VL sharing it: the XSGD bid spends 9.95 USDC and the EURC bid 19.90,
    # together more than the 20 USDC budget, but Sera locks only the largest.
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    config = VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("20"))],
        pairs=[_pair("XSGD/USDC"), _pair("EURC/USDC")],
        fixed_bps=Decimal("50"),
        return_bps=Decimal("2"),
    )
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    _stop_after_sleeps(monkeypatch, 1)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    assert [
        (leg["market"], leg["side"]) for leg in client.batch_places[0]["legs"]
    ] == [("XSGD/USDC", "bid"), ("EURC/USDC", "bid")]


def test_run_keeps_quoting_when_a_price_rise_pushes_a_bid_over_budget(monkeypatch):
    # The XSGD bid is sized to the whole 10 USDC budget at SGD 1.0000 (it spends
    # 9.95). SGD then rises 1%, which would make it spend 10.049: instead of the
    # budget check stopping every market, the bid is trimmed to fit.
    client = _FakeClient()
    source = _SequencedSource({
        ("SGD", "USD"): ["1.0000", "1.0100"],
        ("EUR", "USD"): ["1.0000"],
    })
    config = VLConfig(
        budgets=[BudgetConfig("USDC", Decimal("10"))],
        pairs=[_pair("XSGD/USDC"), _pair("EURC/USDC", qty="5")],
        fixed_bps=Decimal("50"),
        return_bps=Decimal("2"),
    )
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    _stop_after_sleeps(monkeypatch, 2)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    xsgd_bids = [
        (leg["qty"], leg["price"]) for batch in client.batch_places for leg in batch["legs"]
        if leg["market"] == "XSGD/USDC"
    ]
    # Tick 2: 10 / 1.0049 = 9.9512..., rounded down to the 0.01 step.
    assert xsgd_bids == [
        (Decimal("10"), Decimal("0.9950")),
        (Decimal("9.95"), Decimal("1.0049")),
    ]


def _markets_quoted_per_tick(client: _FakeClient) -> list[set[str]]:
    ticks: list[set[str]] = []
    batches = {batch["batch_id"]: batch["legs"] for batch in client.batch_places}
    for event in client.events:
        if event[0] == "expiration":
            ticks.append(set())
        elif event[0] == "place_vl":
            ticks[-1].update(leg["market"] for leg in batches[event[1]])
        elif event[0] == "place_order":
            ticks[-1].add(event[2])
    return ticks


def test_run_skips_only_the_market_whose_fills_could_not_be_read(monkeypatch):
    client = _FakeClient()
    client.unreadable_market = "XSGD/USDC"
    client.unreadable_ticks = range(2, 3)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _stop_after_sleeps(monkeypatch, 3)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    # Tick 2 cannot read XSGD's fills, so only XSGD sits that tick out.
    assert _markets_quoted_per_tick(client) == [
        {"XSGD/USDC", "EURC/USDC"},
        {"EURC/USDC"},
        {"XSGD/USDC", "EURC/USDC"},
    ]


def test_run_alerts_once_when_an_orders_fills_stay_unreadable(monkeypatch):
    client = _FakeClient()
    client.unreadable_market = "XSGD/USDC"
    client.unreadable_ticks = range(2, 100)
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _stop_after_sleeps(monkeypatch, vr.READ_FAILURE_ALERT_TICKS + 3)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        telegram=True,
        state_path=":memory:",
    )

    assert result == 0
    assert len(notifier.containing("XSGD/USDC not quoting")) == 1
    # EURC kept quoting on every tick throughout.
    assert all("EURC/USDC" in tick for tick in _markets_quoted_per_tick(client))


def test_run_waits_a_few_seconds_for_the_cancel_guard_instead_of_skipping(monkeypatch, tmp_path):
    # The next tick starts 2 s before the newest order clears the 300 s guard:
    # it waits and requotes. One starting 200 s early still skips.
    for first_step, ticks_placed in ((298.0, 2), (100.0, 1)):
        client = _FakeClient()
        source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
        _wire_runner(monkeypatch, config=_config(), client=client, source=source)
        _fake_clock(monkeypatch, steps=[first_step, 1.0])

        assert vr.run(
            config_path="vl.toml",
            min_requote_seconds=300.0,
            log_level="ERROR",
            state_path=tmp_path / f"state-{ticks_placed}.sqlite",
        ) == 0
        assert len(client.batch_places) == ticks_placed


def test_run_does_not_send_orders_the_wallet_cannot_fund(monkeypatch, caplog):
    # Earn funds only USDC: the asks spend XSGD/EURC the vault does not hold.
    # With balances known they are not sent at all, so Sera has nothing to
    # reject and nothing is logged at WARNING or above.
    client = _FakeClient(reject_asks=True)
    client.balances = {"USDC": Decimal("100000")}
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _stop_after_sleeps(monkeypatch, 2)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        state_path=":memory:",
    )

    assert result == 0
    assert len(client.batch_places) == 2
    assert client.standalone_places == []
    assert not [event for event in client.events if event[0] == "rejected"]
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_run_keeps_quoting_bids_when_every_ask_is_rejected(monkeypatch):
    # Earn funds only USDC, so Sera rejects every ask (they spend XSGD/EURC the
    # wallet does not hold). The USDC bid batch must still go out every tick.
    client = _FakeClient(reject_asks=True)
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _stop_after_sleeps(monkeypatch, 2)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    assert len(client.batch_places) == 2
    assert {leg["side"] for batch in client.batch_places for leg in batch["legs"]} == {"bid"}
    assert len([event for event in client.events if event[0] == "rejected"]) == 4
    # The second tick cancelled and replaced the first tick's bids as usual.
    assert ("cancel_vl", "batch-1") in client.events
    assert len(client.active_orders) == 2


def test_run_skips_a_market_sera_does_not_list_and_alerts(monkeypatch, caplog):
    # JPYC/USDC is not listed (delisted, say) and EURC is configured in the
    # opposite orientation: both are skipped, XSGD/USDC still quotes.
    client = _FakeClient()
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"]})
    config = _config(pairs=[_pair("XSGD/USDC"), _pair("JPYC/USDC"), _pair("USDC/EURC")])
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _stop_after_sleeps(monkeypatch, 1)

    result = vr.run(
        config_path="vl.toml",
        min_requote_seconds=0.0,
        telegram=True,
        state_path=":memory:",
    )

    assert result == 0
    assert {row["market"] for row in client.standalone_places} == {"XSGD/USDC"}
    assert "not quoting JPYC/USDC: Sera does not list this market" in caplog.text
    assert (
        "not quoting USDC/EURC: Sera lists this pair as EURC/USDC; configure it that way"
        in caplog.text
    )
    alerts = notifier.containing("Not quoting JPYC/USDC (Sera does not list this market)")
    assert len(alerts) == 1 and alerts[0][1] is True
    assert "USDC/EURC (Sera lists this pair as EURC/USDC" in alerts[0][0]


def test_run_skips_a_pair_whose_token_has_no_currency_mapping(monkeypatch, caplog):
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    # The operator's mapping leaves EURC out (the built-in map is replaced here).
    monkeypatch.setattr(vr, "market_to_iso_pair", lambda market, mapping: (
        ("SGD", "USD") if market == "XSGD/USDC"
        else (_ for _ in ()).throw(ValueError("no ISO mapping for token 'EURC'"))
    ))
    _stop_after_sleeps(monkeypatch, 1)

    assert vr.run(config_path="vl.toml", min_requote_seconds=0.0, state_path=":memory:") == 0
    assert {row["market"] for row in client.standalone_places} == {"XSGD/USDC"}
    assert "not quoting EURC/USDC: no ISO mapping for token 'EURC'" in caplog.text


def test_run_refuses_to_start_when_sera_lists_none_of_the_markets(monkeypatch, caplog):
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"]})
    _wire_runner(monkeypatch, config=_config(pairs=[_pair("JPYC/USDC")]), client=client, source=source)
    _stop_after_sleeps(monkeypatch, 1)

    assert vr.run(config_path="vl.toml", state_path=":memory:") == 1
    assert "no configured pair can be quoted: JPYC/USDC (Sera does not list" in caplog.text
    assert client.batch_places == [] and client.standalone_places == []


SERA_DOWN = SeraError(None, "Service temporarily unavailable; please retry", 503)


class _RecordingStop(_StopFlag):
    """Stop flag that records waits and stops the run when asked to wait long."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if timeout > 120:
            self.requested = True
        return self.requested


def _boots(client: _FakeClient) -> int:
    return len([event for event in client.events if event[0] == "bootstrap"])


def test_run_retries_a_temporary_startup_failure_and_reports_recovery(monkeypatch, tmp_path):
    client = _FakeClient()
    client.bootstrap_errors = [SERA_DOWN, SeraUnavailable("GET /health is not healthy")]
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _stop_after_sleeps(monkeypatch, 1)
    state_path = tmp_path / "state.sqlite"

    result = vr.run(
        config_path="vl.toml", min_requote_seconds=0.0, telegram=True, state_path=state_path)

    assert result == 0
    assert _boots(client) == 3
    # One alert for the hour's first failure (not the second), one on recovery.
    assert len(notifier.containing("rotor could not start (attempt 1 of 3 this hour)")) == 1
    assert notifier.containing("attempt 2 of 3") == []
    assert len(notifier.containing("rotor started after 2 failed attempt(s)")) == 1
    assert len(client.batch_places) == 1
    assert not (tmp_path / "state.sqlite.boot.json").exists()


def test_run_waits_for_the_next_hour_after_three_failed_startups(monkeypatch, tmp_path):
    clock = {"now": T0 + 600.0}
    client = _FakeClient()
    client.bootstrap_errors = [SERA_DOWN] * 10
    notifier = _RecordingNotifier()
    stop = _RecordingStop()
    _wire_runner(monkeypatch, config=_config(), client=client, source=_SequencedSource({}))
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])

    result = vr.run(config_path="vl.toml", telegram=True, state_path=tmp_path / "state.sqlite")

    assert result == 1
    assert _boots(client) == 3
    # 30 s and 60 s between attempts, then until the top of the next hour.
    assert stop.waits == [30.0, 60.0, 3000.0]
    errors = [text for text, _ in notifier.messages if "rotor could not start" in text]
    assert len(errors) == 2
    assert "attempt 3 of 3 this hour; next attempt at" in errors[1]


def test_fire_once_runs_share_the_hourly_startup_budget(monkeypatch, tmp_path):
    clock = {"now": T0 + 600.0}
    client = _FakeClient()
    client.bootstrap_errors = [SERA_DOWN] * 10
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)
    monkeypatch.setattr(vr.time, "time", lambda: clock["now"])

    def fire() -> int:
        return vr.run(config_path="vl.toml", once=True, telegram=True,
                      state_path=tmp_path / "state.sqlite")

    assert [fire() for _ in range(4)] == [1, 1, 1, 1]
    # The fourth fire in the hour does not contact Sera; only the first and the
    # third (the hour's last attempt) alert.
    assert _boots(client) == 3
    assert len([t for t, _ in notifier.messages if "rotor could not start" in t]) == 2

    clock["now"] += 3600.0
    client.bootstrap_errors = []
    assert fire() == 0
    assert _boots(client) == 4


def test_run_does_not_retry_a_startup_failure_retrying_cannot_fix(monkeypatch, tmp_path):
    client = _FakeClient()
    notifier = _RecordingNotifier()
    stop = _RecordingStop()
    _wire_runner(monkeypatch, config=_config(), client=client, source=_SequencedSource({}))
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    monkeypatch.setattr(vr, "load_vl_config", lambda _path: (_ for _ in ()).throw(
        ValueError("vl.toml: return_bps must be > 0")))

    assert vr.run(config_path="vl.toml", telegram=True, state_path=tmp_path / "state.sqlite") == 1
    assert stop.waits == []
    assert _boots(client) == 0
    assert len(notifier.containing("return_bps must be > 0")) == 1


def test_run_drops_a_market_sera_deactivates_and_retries_it_after_refresh(monkeypatch):
    # XSGD/USDC is deactivated after tick 1 but stays listed. Tick 2's USDC bid
    # batch is re-sent without it, ticks 2-3 leave it out with one alert, and
    # the hourly metadata refresh (tick 4) tries it again once Sera reactivates it.
    client = _FakeClient()
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    clock = _fake_clock(monkeypatch, steps=[305.0, 305.0, 3600.0, 1.0])
    real_sleep = vr.sleep_remaining

    def deactivate_after_first_tick(started, poll, stop=None):
        if clock["now"] == T0:
            client.inactive_markets = {"XSGD/USDC"}
        if clock["now"] == T0 + 610.0:
            client.inactive_markets = set()
        real_sleep(started, poll, stop)

    monkeypatch.setattr(vr, "sleep_remaining", deactivate_after_first_tick)

    result = vr.run(config_path="vl.toml", min_requote_seconds=0.0, telegram=True,
                    state_path=":memory:")

    assert result == 0
    assert _markets_quoted_per_tick(client) == [
        {"XSGD/USDC", "EURC/USDC"},
        {"EURC/USDC"},
        {"EURC/USDC"},
        {"XSGD/USDC", "EURC/USDC"},
    ]
    # Sera rejected XSGD/USDC only on tick 2 (the bid batch, then the ask);
    # tick 3 did not send it.
    assert client.inactive_rejections == ["XSGD/USDC", "XSGD/USDC"]
    assert len(notifier.containing("Sera says XSGD/USDC is not active")) == 1
    # Boot plus the hourly refresh.
    assert _boots(client) == 2


def test_run_refreshes_metadata_early_after_a_rejection_that_suggests_it_is_stale(monkeypatch):
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[305.0, 305.0, 1.0])

    def batch_limit_changed(order):
        raise SeraError(None, "quantity is not on the market lot grid", 422)

    monkeypatch.setattr(client, "send_order", batch_limit_changed)

    vr.run(config_path="vl.toml", min_requote_seconds=0.0, state_path=":memory:")

    # Rejected on tick 1, so the refresh comes 600 s after boot (tick 3), not hourly.
    assert _boots(client) == 2


def test_run_keeps_quoting_through_failed_metadata_refreshes_and_alerts_once(monkeypatch):
    # Boot succeeds; the hourly refresh (tick 2) and the retry 10 minutes later
    # (tick 3) fail; the next retry (tick 4) succeeds.
    client = _FakeClient()
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    clock = _fake_clock(monkeypatch, steps=[3600.0, 600.0, 600.0, 1.0])
    real_sleep = vr.sleep_remaining

    def refresh_fails_twice(started, poll, stop=None):
        if clock["now"] == T0:
            client.bootstrap_errors = [SERA_DOWN, SERA_DOWN]
        real_sleep(started, poll, stop)

    monkeypatch.setattr(vr, "sleep_remaining", refresh_fails_twice)

    vr.run(config_path="vl.toml", min_requote_seconds=0.0, telegram=True,
           state_path=":memory:")

    assert _boots(client) == 4
    assert len(_markets_quoted_per_tick(client)) == 4
    assert all(markets == {"XSGD/USDC", "EURC/USDC"} for markets in _markets_quoted_per_tick(client))
    assert len(notifier.containing("could not refresh Sera market metadata")) == 1
    assert len(notifier.containing("metadata refreshed again")) == 1


def test_debug_logging_never_logs_telegram_request_urls(caplog, tmp_path):
    # httpx logs each request URL at INFO, and a Telegram URL carries the bot
    # token. Rotor's own DEBUG lines stay on; the libraries' are capped.
    names = ("httpx", "httpcore", "telegram")
    levels = {name: logging.getLogger(name).level for name in names}
    try:
        vr.run(config_path=tmp_path / "missing.toml", dry_run=True, once=True,
               log_level="DEBUG")
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with caplog.at_level(logging.DEBUG), httpx.Client(transport=transport) as http:
            http.post("https://api.telegram.org/bot123:SECRET/sendMessage")
            logging.getLogger("rotor.mm.vl").debug("rotor debug line")
        assert "123:SECRET" not in caplog.text
        assert "rotor debug line" in caplog.text
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


def test_run_places_the_new_set_only_after_the_cancelled_one_is_gone(monkeypatch):
    # Sera keeps listing tick 1's batch as open for two reads after rotor
    # cancels it; tick 2 places only once it is gone.
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _fake_clock(monkeypatch, steps=[305.0, 1.0])
    lingering = {"reads": 0}
    real_cancel = client.cancel_vl_batch

    def cancel_but_linger(vl_batch_id):
        ok = real_cancel(vl_batch_id)
        lingering["rows"] = [{"trade_id": oid} for oid in client.batch_orders[vl_batch_id]]
        lingering["reads"] = 2
        return ok

    def open_orders():
        client.events.append(("open_orders",))
        if lingering["reads"]:
            lingering["reads"] -= 1
            return list(lingering["rows"])
        return []

    monkeypatch.setattr(client, "cancel_vl_batch", cancel_but_linger)
    monkeypatch.setattr(client, "get_open_orders", open_orders)

    vr.run(config_path="vl.toml", min_requote_seconds=0.0, state_path=":memory:")

    events = [event[0] for event in client.events]
    cancel = events.index("cancel_vl")
    place = events.index("place_vl", cancel)
    assert events[cancel:place].count("open_orders") == 3


def test_run_skips_failed_tick_and_places_on_next_tick(monkeypatch):
    client = _FakeClient()
    # Per-pair isolation, and Wise standing in for a failed source, mean a full
    # tick-skip needs both pairs' sources and Wise to fail; tick 2 recovers.
    source = _SequencedSource(
        {
            ("SGD", "USD"): ["1.0000"],
            ("EUR", "USD"): ["2.0000"],
        },
        always_fail={("SGD", "USD"), ("EUR", "USD")},
    )
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    sleeps = _stop_after_sleeps(monkeypatch, 2)
    count_sleep = vr.sleep_remaining

    def recover(started, poll, stop=None):
        source.always_fail = set()
        count_sleep(started, poll, stop)

    monkeypatch.setattr(vr, "sleep_remaining", recover)

    result = vr.run(
        config_path="vl.toml",
        poll_seconds=305.0,
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    assert sleeps == [305.0, 305.0]
    assert len(client.batch_places) == 1
    assert len(client.standalone_places) == 2
    assert len(client.active_orders) == 4


def test_run_once_places_quote_set_and_returns_zero(monkeypatch):
    # The fire-once scheduler path: one full reattach-cancel-place cycle, exit 0.
    client = _FakeClient()
    source = _SequencedSource({
        ("SGD", "USD"): ["1.0000"],
        ("EUR", "USD"): ["2.0000"],
    })
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    result = vr.run(
        config_path="vl.toml",
        once=True,
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    assert len(client.active_orders) == 4


def test_run_once_does_not_block_on_boot_cancel_cooldown(monkeypatch):
    # A scheduled fire-once run must never hang on Sera's cancel cooldown: the
    # boot cancel does a single pass (no multi-minute wait) in --once mode.
    client = _FakeClient(
        open_orders=[{
            "symbol": "XSGD/USDC",
            "side": "bid",
            "order_id": "old-order",
            "uuid_int": "42",
            "created_at": "2026-01-01T00:00:00Z",
        }],
        cancel_ok=False,
    )
    source = _SequencedSource({("SGD", "USD"): ["1.0000"]})
    _wire_runner(
        monkeypatch,
        config=_config(pairs=[_pair("XSGD/USDC")]),
        client=client,
        source=source,
    )
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)
    sleeps: list[float] = []
    monkeypatch.setattr(vr.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = vr.run(
        config_path="vl.toml",
        once=True,
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    # Boot cancel hit cooldown, so this fire could not requote and reports 1 —
    # but it returned promptly without the 300s boot-cooldown wait.
    assert result == 1
    assert all(seconds < 100 for seconds in sleeps)


def test_run_isolates_failing_pair_and_quotes_healthy_pair(monkeypatch):
    # XSGD's reference source always fails; EURC's works. The failing pair must
    # not sideline quoting for the healthy pair.
    client = _FakeClient()
    source = _SequencedSource(
        {("EUR", "USD"): ["2.0000"]},
        always_fail={("SGD", "USD")},
    )
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    _stop_after_sleeps(monkeypatch, 1)

    result = vr.run(
        config_path="vl.toml",
        poll_seconds=305.0,
        min_requote_seconds=0.0,
        log_level="ERROR",
        state_path=":memory:",
    )

    assert result == 0
    # Only EURC produced intents (bid spends USDC, ask spends EURC) — both
    # singletons, so two standalone placements and no XSGD orders at all.
    assert len(client.standalone_places) == 2
    assert client.batch_places == []
    assert {row["market"] for row in client.standalone_places} == {"EURC/USDC"}


def test_run_keeps_quoting_and_alerts_when_an_update_cannot_be_used(monkeypatch):
    # The pulled version fails its start check: no restart, the old version
    # keeps quoting, and the operator gets an important alert saying why.
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    notifier = _RecordingNotifier()
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _fake_clock(monkeypatch, steps=[305.0, 1.0])

    class CannotStart:
        def __init__(self, *, interval_seconds: float) -> None:
            pass

        def run_if_due(self, _log):
            return GitUpdateResult(
                status="failed",
                message="pulled aaaa..bbbb, but the new version fails its start check")

    restarts: list[bool] = []
    monkeypatch.setattr(vr, "DailyGitUpdater", CannotStart)
    monkeypatch.setattr(vr, "restart_current_process", lambda: restarts.append(True))

    vr.run(config_path="vl.toml", auto_update=True, telegram=True,
           min_requote_seconds=0.0, state_path=":memory:")

    assert restarts == []
    assert len(_markets_quoted_per_tick(client)) == 2
    alerts = notifier.containing("auto-update failed: pulled aaaa..bbbb")
    assert alerts and all(important for _text, important in alerts)


def test_a_stop_mid_tick_with_cancel_on_exit_places_nothing_new(monkeypatch):
    # Tick 1 places a set. A stop arrives during tick 2: its cancel-first clears
    # tick 1's set and rotor exits without placing another.
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    clock = _fake_clock(monkeypatch, steps=[400.0, 400.0])
    stop = vr.StopFlag()
    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    real_balances = client.get_vault_balances

    def stop_during_tick_2():
        if clock["now"] > T0:
            stop.request()
        return real_balances()

    client.balances = {"USDC": Decimal("100000"), "XSGD": Decimal("100000"),
                       "EURC": Decimal("100000")}
    monkeypatch.setattr(client, "get_vault_balances", stop_during_tick_2)

    result = vr.run(config_path="vl.toml", cancel_on_exit=True, min_requote_seconds=0.0,
                    state_path=":memory:")

    assert result == 0
    assert len(_markets_quoted_per_tick(client)) == 2
    assert _markets_quoted_per_tick(client)[1] == set()
    assert client.active_orders == set()


def test_run_exits_1_when_cancel_on_exit_leaves_orders_live(monkeypatch):
    client = _FakeClient(cancel_ok=False)
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _fake_clock(monkeypatch, steps=[1.0])

    result = vr.run(config_path="vl.toml", cancel_on_exit=True, telegram=True,
                    min_requote_seconds=0.0, state_path=":memory:")

    assert result == 1
    assert notifier.containing("could not be cancelled before exit")


def _remember(state_path, order_id: str, market: str = "XSGD/USDC") -> None:
    """Record `order_id` as one this rotor placed, as a previous run would have."""
    store = vr.StateStore(state_path)
    try:
        store.replace_active([vr.LivePlacement(
            kind="standalone", spent_token="USDC",
            orders=[vr.LiveOrder("bid", order_id, 42, 0.0, market)], placed_at=0.0,
        )])
    finally:
        store.close()


_OLD_ORDER = {
    "symbol": "XSGD/USDC",
    "side": "bid",
    "order_id": "old-order",
    "uuid_int": "42",
    "created_at": "2026-01-01T00:00:00Z",
}


def test_run_refuses_to_start_next_to_orders_it_did_not_place(monkeypatch, tmp_path):
    # Another rotor (or a person) is trading this wallet: rotor neither starts
    # nor touches that trader's order, and says why.
    client = _FakeClient(open_orders=[_OLD_ORDER])
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    # Should rotor start anyway, stop after one tick rather than run on.
    _fake_clock(monkeypatch, steps=[1.0])

    result = vr.run(config_path="vl.toml", telegram=True, state_path=tmp_path / "state.sqlite")

    assert result == 1
    assert not any(event[0].startswith("cancel") for event in client.events)
    assert client.batch_places == [] and client.standalone_places == []
    assert notifier.containing("1 open order(s) on this rotor's markets that it did not place")


def test_run_leaves_a_foreign_order_alone_mid_run_and_alerts_once(monkeypatch):
    # Rotor tracks nothing after a tick whose placements all fail; meanwhile
    # someone else's order appears. Rotor does not adopt (and so cancel) it.
    client = _FakeClient(reject_asks=True)
    notifier = _RecordingNotifier()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    client.unsent_errors = [SeraError("INSUFFICIENT_EQUITY", "no USDC", 400)] * 3
    clock = _fake_clock(monkeypatch, steps=[305.0, 305.0, 1.0])
    real_sleep = vr.sleep_remaining

    def someone_else_places(started, poll, stop=None):
        client.open_orders = [_OLD_ORDER]
        real_sleep(started, poll, stop)

    monkeypatch.setattr(vr, "sleep_remaining", someone_else_places)

    vr.run(config_path="vl.toml", telegram=True, min_requote_seconds=0.0,
           state_path=":memory:")

    assert ("cancel_order", "old-order", 42) not in client.events
    assert len(notifier.containing("that it did not place")) == 1
    assert clock["now"] > T0


def test_run_auto_update_cleans_up_live_orders_and_restarts(monkeypatch, tmp_path):
    client = _FakeClient(open_orders=[_OLD_ORDER])
    _remember(tmp_path / "state.sqlite", "old-order")
    source = _SequencedSource({("SGD", "USD"): ["1.0000"]})
    _wire_runner(
        monkeypatch,
        config=_config(pairs=[_pair("XSGD/USDC")]),
        client=client,
        source=source,
    )
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    class UpdatedOnce:
        def __init__(self, *, interval_seconds: float) -> None:
            self.interval_seconds = interval_seconds

        def run_if_due(self, _log):
            return GitUpdateResult(status="updated", message="pulled")

    restarts: list[bool] = []
    monkeypatch.setattr(vr, "DailyGitUpdater", UpdatedOnce)
    monkeypatch.setattr(vr, "restart_current_process", lambda: restarts.append(True))

    result = vr.run(
        config_path="vl.toml",
        auto_update=True,
        cancel_on_exit=True,
        log_level="ERROR",
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    assert restarts == [True]
    assert ("cancel_order", "old-order", 42) in client.events
    assert client.batch_places == []
    assert client.standalone_places == []


# ---------------------------------------------------------------------------
# Daily reference rates that stop updating over a weekend
# ---------------------------------------------------------------------------

DAY = 24 * 3600.0
# Friday's rate read on Monday morning, before the source publishes again.
WEEKEND_AGE = 3 * DAY
# A date-only publication (1970-01-21), stamped at UTC midnight like BNM's.
FRIDAY = 20 * DAY
LOG = logging.getLogger("rotor.test.stale")
WEEKEND_RATES = {("SGD", "USD"): ["0.7400"], ("EUR", "USD"): ["1.1000"]}


class _AgedSource(_SequencedSource):
    """Daily-style source whose observations are `age_s` seconds old."""

    name = "bnm"
    default_max_age_s = 2 * DAY
    default_max_carry_age_s = 7 * DAY

    def __init__(self, rates, *, age_s: float, always_fail=None) -> None:
        super().__init__(rates, always_fail=always_fail)
        self.age_s = age_s

    def quote(self, base: str, quote: str, *, amount=None) -> PriceObservation:
        obs = super().quote(base, quote, amount=amount)
        return PriceObservation(
            source=self.name, pair=obs.pair, rate=obs.rate, ts=time.time() - self.age_s,
        )


class _RecordingNotifier:
    """Stand-in for TelegramNotifier that records every message it is asked to send."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    def send_raw(self, text: str, *, important: bool = False, debug: bool = False) -> None:
        self.messages.append((text, important))

    def notify_startup(self, text: str) -> None:
        self.messages.append((text, False))

    def notify_shutdown(self, text: str) -> None:
        self.messages.append((text, False))

    def notify_quote(self, market: str, **_kwargs) -> None:
        return None

    def notify_error(self, context: str, error: object) -> None:
        self.messages.append((f"error: {context}: {error}", True))

    def heartbeat(self) -> bool:
        return False

    def containing(self, needle: str) -> list[tuple[str, bool]]:
        return [message for message in self.messages if needle in message[0]]


def _headings(notifier: _RecordingNotifier) -> list[tuple[str, bool]]:
    return [(text.split("\n")[0], important) for text, important in notifier.messages]


def test_run_keeps_quoting_on_a_weekend_rate_and_warns_once(monkeypatch, tmp_path):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE)
    notifier = _RecordingNotifier()
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    _stop_after_sleeps(monkeypatch, 3)

    result = vr.run(
        config_path="unused.toml",
        min_requote_seconds=0.0,
        telegram=True,
        state_path=tmp_path / "state.sqlite",
    )

    assert result == 0
    # Quotes keep going out on the carried Friday rate, tick after tick.
    assert len(client.batch_places) == 3
    warnings = notifier.containing("not updating")
    assert len(warnings) == 1
    text, important = warnings[0]
    assert important is True
    assert "EURC/USDC: bnm rate" in text and "XSGD/USDC: bnm rate" in text
    assert "3d 0h old" in text
    assert not notifier.containing("stalled")


def test_run_with_halt_on_stale_reference_quotes_on_wises_mid_not_weekend_rates(
    monkeypatch, tmp_path,
):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE)
    notifier = _RecordingNotifier()
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    result = vr.run(
        config_path="unused.toml",
        once=True,
        telegram=True,
        halt_on_stale_reference=True,
        state_path=tmp_path / "state.sqlite",
    )

    # No quote rests on a weekend rate: the stale mids are replaced by Wise's
    # live mid (quotes are at Wise's prices either way), with one alert.
    assert result == 0
    assert client.batch_places != []
    assert not notifier.containing("not updating")
    assert len(notifier.containing("use Wise's mid-market rate instead")) == 1


def test_fire_once_runs_announce_a_weekend_only_once(monkeypatch, tmp_path):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE)
    notifier = _RecordingNotifier()
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    for _ in range(3):
        vr.run(
            config_path="unused.toml",
            once=True,
            min_requote_seconds=0.0,
            telegram=True,
            state_path=tmp_path / "state.sqlite",
        )

    assert len(notifier.containing("not updating")) == 1


def test_tick_references_record_carried_and_skipped_pairs(monkeypatch):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE, always_fail={("EUR", "USD")})
    config = _config()
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    runtimes = vr._build_runtimes(config, client, max_reference_age_s=None)
    refs = vr.TickReferences()

    intents = vr._build_tick_intents(config, runtimes, {}, expiration=1, log=LOG, refs=refs)

    assert {intent.market for intent in intents} == {"XSGD/USDC"}
    assert refs.quoted["XSGD/USDC"].stale is True
    assert isinstance(refs.skipped["EURC/USDC"], ReferenceUnavailable)
    assert not isinstance(refs.skipped["EURC/USDC"], ReferenceTooStale)


def test_a_rate_past_its_carry_limit_is_replaced_by_wises_mid_with_one_alert(
    monkeypatch, tmp_path,
):
    # Nine days without a new daily rate: past the 7-day carry limit. The pairs
    # keep quoting on Wise's mid, alerted once across fires, and a fresh rate
    # is reported when it comes back.
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=9 * DAY)
    notifier = _RecordingNotifier()
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    monkeypatch.setattr(vr, "TelegramNotifier", lambda **_kwargs: notifier)
    monkeypatch.setattr(vr, "StopFlag", _StopFlag)

    def fire() -> int:
        return vr.run(config_path="unused.toml", once=True, telegram=True,
                      min_requote_seconds=0.0, state_path=tmp_path / "state.sqlite")

    assert [fire(), fire()] == [0, 0]
    assert len(notifier.containing("use Wise's mid-market rate instead")) == 1
    assert not notifier.containing("Stopped quoting")
    assert not notifier.containing("Not quoting these pairs")

    source.age_s = 0.0
    assert fire() == 0
    assert len(notifier.containing("Reference rates updated again")) == 1


def test_halting_runtimes_quote_on_wises_mid_instead_of_a_weekend_rate(monkeypatch):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE)
    config = _config()
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    runtimes = vr._build_runtimes(config, client, max_reference_age_s=None, carry_stale=False)
    refs = vr.TickReferences()

    intents = vr._build_tick_intents(config, runtimes, {}, expiration=1, log=LOG, refs=refs)

    # The weekend rate is refused, and Wise's live mid stands in for it.
    assert isinstance(refs.fallback["XSGD/USDC"], ReferenceTooStale)
    assert refs.skipped == {}
    assert refs.quoted["XSGD/USDC"].source == "wise"
    assert {intent.market for intent in intents} == {"XSGD/USDC", "EURC/USDC"}


def _stale_alerts(*, state=None, markets=("XSGD/USDC",)) -> vr.StaleReferenceAlerts:
    runtimes = [
        SimpleNamespace(
            config=SimpleNamespace(market=market),
            oracle=SimpleNamespace(max_carry_age_s=7 * DAY),
        )
        for market in markets
    ]
    return vr.StaleReferenceAlerts(runtimes, state=state)


def _refs(*, stale=True, age_s=WEEKEND_AGE, ts=FRIDAY) -> vr.TickReferences:
    return vr.TickReferences(quoted={
        "XSGD/USDC": ReferencePrice(
            pair="SGD/USD", rate=Decimal("0.74"), ts=ts, source="bnm",
            age_s=age_s, stale=stale,
        ),
    })


STARTED = "🟡 Reference rates are not updating. These pairs keep quoting on the last published rate:"
REMINDER = "🟡 Still quoting on reference rates that have not updated:"
RECOVERED = "✅ Reference rates updated again:"
STOPPED = "⛔ Stopped quoting on reference rates past their carry limit:"


def test_stale_alerts_announce_remind_and_recover():
    alerts = _stale_alerts()
    notifier = _RecordingNotifier()
    hour = 3600.0
    monday = _refs(stale=False, age_s=60.0, ts=FRIDAY + 3 * DAY)

    alerts.update(_refs(), notifier=notifier, log=LOG, now=0.0)
    alerts.update(_refs(), notifier=notifier, log=LOG, now=1 * hour)
    alerts.update(_refs(), notifier=notifier, log=LOG, now=6 * hour)
    alerts.update(monday, notifier=notifier, log=LOG, now=7 * hour)
    alerts.update(monday, notifier=notifier, log=LOG, now=8 * hour)

    assert _headings(notifier) == [(STARTED, True), (REMINDER, False), (RECOVERED, False)]
    started = notifier.messages[0][0]
    assert "XSGD/USDC: bnm rate dated 1970-01-21, 3d 0h old" in started
    assert "no new rate by 1970-01-28 00:00 UTC" in started
    assert "weekends and public holidays" in started
    assert "XSGD/USDC: bnm rate dated 1970-01-24" in notifier.messages[2][0]


def test_stale_alerts_report_a_rate_past_its_carry_limit():
    alerts = _stale_alerts()
    notifier = _RecordingNotifier()
    too_old = vr.TickReferences(skipped={
        "XSGD/USDC": ReferenceTooStale("XSGD/USDC: bnm quote is stale age=700000.0s"),
    })

    alerts.update(_refs(), notifier=notifier, log=LOG, now=0.0)
    alerts.update(too_old, notifier=notifier, log=LOG, now=305.0)
    alerts.update(too_old, notifier=notifier, log=LOG, now=610.0)
    alerts.update(_refs(stale=False, age_s=60.0), notifier=notifier, log=LOG, now=915.0)

    assert _headings(notifier) == [(STARTED, True), (STOPPED, True), (RECOVERED, False)]
    assert "age=700000.0s" in notifier.messages[1][0]


def test_stale_alerts_ignore_a_one_off_fetch_failure():
    alerts = _stale_alerts()
    notifier = _RecordingNotifier()
    blip = vr.TickReferences(skipped={
        "XSGD/USDC": ReferenceUnavailable("XSGD/USDC: bnm failed (ReadTimeout: timed out)"),
    })

    alerts.update(_refs(), notifier=notifier, log=LOG, now=0.0)
    alerts.update(blip, notifier=notifier, log=LOG, now=305.0)
    alerts.update(_refs(), notifier=notifier, log=LOG, now=610.0)

    assert _headings(notifier) == [(STARTED, True)]


def test_stale_alert_episodes_survive_a_restart(tmp_path):
    store = vr.StateStore(tmp_path / "state.sqlite")
    notifier = _RecordingNotifier()
    try:
        _stale_alerts(state=store).update(_refs(), notifier=notifier, log=LOG, now=0.0)
        _stale_alerts(state=store).update(_refs(), notifier=notifier, log=LOG, now=305.0)
    finally:
        store.close()

    assert _headings(notifier) == [(STARTED, True)]


def test_stale_alerts_still_log_without_telegram(caplog):
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        _stale_alerts().update(_refs(), notifier=None, log=LOG, now=0.0)

    assert "Reference rates are not updating" in caplog.text


def test_run_refuses_an_order_ttl_sera_would_sweep_at_once(monkeypatch, caplog):
    client = _FakeClient()
    source = _SequencedSource({("SGD", "USD"): ["1.0000"], ("EUR", "USD"): ["2.0000"]})
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)

    assert vr.run(config_path="vl.toml", order_ttl_seconds=60, state_path=":memory:") == 1
    assert "--order-ttl 60s is too short" in caplog.text
    assert client.events == []
