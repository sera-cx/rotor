"""Run-loop integration tests for the VL market maker."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from types import SimpleNamespace

from rotor.mm import vl_runner as vr
from rotor.mm.exchange.client import MarketInfo, OrderRecord, VLBatchResult
from rotor.mm.exchange.errors import SeraError, SeraRateLimit
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


class _StopFlag:
    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        return None

    def request(self) -> None:
        self.requested = True

    def wait(self, _timeout: float) -> bool:
        return self.requested


class _FakeClient:
    vl_batch_min = 2
    vl_batch_max = 50

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
        self.batch_orders: dict[str, list[str]] = {}
        self.batch_places: list[dict] = []
        self.standalone_places: list[dict] = []
        self.fill_order_ids: set[str] = set()
        self.fill_status = fill_status
        # Asks spend base tokens; a wallet funded only with USDC cannot back them.
        self.reject_asks = reject_asks
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

    def get_config_limits(self) -> None:
        self.events.append(("limits",))

    def get_open_orders(self) -> list[dict]:
        self.events.append(("open_orders",))
        return list(self.open_orders)

    def expiration_from_server_time(self, order_ttl_seconds: int) -> int:
        self.events.append(("expiration", order_ttl_seconds))
        # Sera's clock follows the local one, which tests may fake.
        return int(time.time()) + order_ttl_seconds

    def place_vl_batch(self, legs) -> VLBatchResult:
        self._batch_count += 1
        batch_id = f"batch-{self._batch_count}"
        records: list[OrderRecord] = []
        rows = []
        for idx, leg in enumerate(legs):
            order_id = f"{batch_id}-leg-{idx}"
            self._uuid_int += 1
            records.append(OrderRecord(order_id, self._uuid_int))
            self.active_orders.add(order_id)
            rows.append({
                "market": leg.market,
                "side": leg.side,
                "price": leg.price,
                "qty": leg.qty_base,
                "order_id": order_id,
            })
            if (
                self.fill_first_xsgd_bid
                and self._batch_count == 1
                and leg.market == "XSGD/USDC"
                and leg.side == "bid"
            ):
                self.fill_order_ids.add(order_id)
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

    def place_order(
        self,
        market: str,
        side: str,
        qty_base: Decimal,
        price: Decimal,
        expiration: int,
    ) -> OrderRecord:
        if self.reject_asks and side == "ask":
            self.events.append(("rejected", market, side))
            raise SeraError("INSUFFICIENT_EQUITY", "available equity below the order", 400)
        self._order_count += 1
        self._uuid_int += 1
        order_id = f"solo-{self._order_count}"
        self.active_orders.add(order_id)
        self.standalone_places.append({
            "market": market,
            "side": side,
            "qty": qty_base,
            "price": price,
            "expiration": expiration,
            "order_id": order_id,
        })
        self.events.append(("place_order", order_id, market, side))
        return OrderRecord(order_id, self._uuid_int)

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
        if order_id in self.fill_order_ids:
            # Like Sera, every call lists all of the order's fills so far.
            return [{"quantity": "5", "price": "1.0", "settlement_status": self.fill_status}]
        return []

    def cancel_vl_batch(self, vl_batch_id: str) -> bool:
        self.events.append(("cancel_vl", vl_batch_id))
        if not self.cancel_ok:
            return False
        for order_id in self.batch_orders.get(vl_batch_id, []):
            self.active_orders.discard(order_id)
        return True

    def cancel_order(self, order_id: str, uuid_int: int) -> bool:
        self.events.append(("cancel_order", order_id, uuid_int))
        if not self.cancel_ok:
            return False
        self.active_orders.discard(order_id)
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


def test_run_skips_failed_tick_and_places_on_next_tick(monkeypatch):
    client = _FakeClient()
    source = _SequencedSource(
        {
            ("SGD", "USD"): ["1.0000"],
            ("EUR", "USD"): ["2.0000"],
        },
        # Per-pair isolation means a full tick-skip needs both pairs to fail.
        fail_first_quotes=2,
    )
    _wire_runner(monkeypatch, config=_config(), client=client, source=source)
    sleeps = _stop_after_sleeps(monkeypatch, 2)

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


def test_run_auto_update_cleans_up_live_orders_and_restarts(monkeypatch):
    client = _FakeClient(open_orders=[{
        "symbol": "XSGD/USDC",
        "side": "bid",
        "order_id": "old-order",
        "uuid_int": "42",
        "created_at": "2026-01-01T00:00:00Z",
    }])
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
        state_path=":memory:",
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


def test_run_with_halt_on_stale_reference_skips_weekend_rates(monkeypatch, tmp_path):
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

    assert result == 1
    assert client.batch_places == []
    assert client.standalone_places == []
    assert not notifier.containing("not updating")


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


def test_halting_runtimes_skip_a_weekend_rate(monkeypatch):
    client = _FakeClient()
    source = _AgedSource(WEEKEND_RATES, age_s=WEEKEND_AGE)
    config = _config()
    _wire_runner(monkeypatch, config=config, client=client, source=source)
    runtimes = vr._build_runtimes(config, client, max_reference_age_s=None, carry_stale=False)
    refs = vr.TickReferences()

    intents = vr._build_tick_intents(config, runtimes, {}, expiration=1, log=LOG, refs=refs)

    assert intents == []
    assert isinstance(refs.skipped["XSGD/USDC"], ReferenceTooStale)


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
