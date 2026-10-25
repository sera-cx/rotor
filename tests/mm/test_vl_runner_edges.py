"""Additional mock-backed coverage for VL runner edge behavior."""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal

import httpx
import pytest
from mm_builders import intent, vl_config

from rotor.mm import vl_runner as vr
from rotor.mm.exchange import errors
from rotor.mm.exchange.client import (
    OrderRecord,
    SignedOrder,
    SignedVLBatch,
    VLBatchAmendment,
    VLBatchResult,
)
from rotor.mm.positions import Position
from rotor.mm.quote_engine import LiveOrder

_LOG = logging.getLogger("test")


def test_fit_budget_caps_keeps_an_order_at_exactly_the_budget():
    groups = [("USDC", [intent(price="1", qty="100")], False)]

    assert vr._fit_budget_caps(groups, vl_config(budget="100"), _LOG) == (groups, {})


def test_fit_budget_caps_leaves_out_an_order_over_the_budget():
    ask = intent(side="ask", spent="USDC", price="1", qty="100.01")

    kept, left_out = vr._fit_budget_caps([("USDC", [ask], False)], vl_config(budget="100"), _LOG)

    assert kept == []
    assert "over its budget of 100" in str(left_out["XSGD/USDC"])


def test_group_intents_batches_an_unbudgeted_multi_leg_group():
    # Two asks spending XSGD (unbudgeted), each sized to the whole XSGD vault
    # balance: standalone, Sera would refuse the second; one batch freezes once.
    groups = vr.group_intents(
        [
            intent(spent="XSGD"),
            intent(market="EURC/USDC", spent="XSGD"),
        ],
        vl_min_size=2,
    )

    assert groups == [
        ("XSGD", groups[0][1], True),
    ]


def test_place_groups_uses_vl_batch_and_tracks_intents():
    class Client:
        vl_batch_min = 2

        def sign_vl_batch(self, legs):
            assert len(legs) == 2
            return SignedVLBatch("batch-1", [
                SignedOrder("leg-1", 101, {}),
                SignedOrder("leg-2", 102, {}),
            ])

        def send_vl_batch(self, batch):
            return VLBatchResult(
                vl_batch_id=batch.vl_batch_id,
                records=[OrderRecord("leg-1", 101), OrderRecord("leg-2", 102)],
                amendments=[],
                cancelled=[],
            )

    intents = [intent(), intent(market="EURC/USDC")]
    placements = vr._place_groups(
        Client(),
        [("USDC", intents, True)],
        logging.getLogger("test"),
    )

    assert len(placements) == 1
    placement = placements[0]
    assert placement.kind == "vl"
    assert placement.vl_batch_id == "batch-1"
    assert [order.order_id for order in placement.orders] == ["leg-1", "leg-2"]
    assert [order.side for order in placement.orders] == ["bid", "bid"]
    assert [order.market for order in placement.orders] == ["XSGD/USDC", "EURC/USDC"]


def test_place_groups_skips_a_rejected_order_and_places_the_rest(caplog):
    calls: list[tuple[str, str]] = []

    class Client:
        vl_batch_min = 2

        def sign_order(self, market, side, qty_base, price, expiration):
            return SignedOrder(f"solo-{market}", 1, {"market": market})

        def send_order(self, order):
            market = order.wire["market"]
            calls.append(("place", market))
            if market == "EURC/USDC":
                raise errors.SeraError("INSUFFICIENT_EQUITY", "not enough EURC", 400)
            return OrderRecord(order.order_id, order.uuid_int)

        def cancel_order(self, order_id, uuid_int):
            calls.append(("cancel", order_id))
            return True

    placements = vr._place_groups(
        Client(),
        [("USDC", [intent(), intent(market="EURC/USDC"), intent(market="JPYC/USDC")], False)],
        logging.getLogger("test"),
    )

    # The rejected order is skipped; the others stay live and tracked, and
    # nothing that did place is cancelled.
    assert [p.orders[0].market for p in placements] == ["XSGD/USDC", "JPYC/USDC"]
    assert calls == [("place", "XSGD/USDC"), ("place", "EURC/USDC"), ("place", "JPYC/USDC")]
    assert "could not place EURC/USDC bid spent=USDC: INSUFFICIENT_EQUITY" in caplog.text


def _not_found(message: str) -> errors.SeraError:
    """Sera's reply to cancelling an order or batch it never booked."""
    return errors.from_response(400, {"detail": {"status": "error", "message": message}})


@pytest.mark.parametrize(("kind", "reply", "kept"), [
    pytest.param("vl", False, True, id="cooldown-kept"),
    pytest.param("standalone", errors.SeraError("ORDER_NOT_FOUND", "no such order", 404),
                 False, id="404-dropped"),
    # As for a placement whose answer was lost and that Sera never booked.
    pytest.param("standalone", _not_found("Order not found for cancellation:0xowner,o-1"),
                 False, id="400-order-dropped"),
    pytest.param("vl", _not_found("VL batch not found: batch-1"), False, id="400-batch-dropped"),
    # A 5xx might mean the order is still live, so it stays tracked for retry.
    pytest.param("standalone", errors.SeraError("SERVER_ERROR", "boom", 500),
                 True, id="5xx-kept"),
])
def test_cancel_placements_keeps_only_what_may_still_be_live(kind, reply, kept):
    placement = vr.LivePlacement(
        kind=kind, spent_token="USDC", orders=[LiveOrder("bid", "o-1", 1, 0.0)], placed_at=0.0,
        vl_batch_id="batch-1" if kind == "vl" else None)
    calls = []

    class Client:
        def cancel_order(self, order_id, uuid_int):
            calls.append("cancel_order")
            return self._answer()

        def cancel_vl_batch(self, vl_batch_id):
            calls.append("cancel_vl_batch")
            return self._answer()

        def _answer(self):
            if isinstance(reply, Exception):
                raise reply
            return reply

    remaining = vr._cancel_placements(Client(), [placement], logging.getLogger("test"))

    assert remaining == ([placement] if kept else [])
    assert calls == ["cancel_vl_batch" if kind == "vl" else "cancel_order"]


def test_state_store_dedupes_and_prunes_unresolved_state(tmp_path):
    store = vr.StateStore(tmp_path / "state.sqlite")
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[
            LiveOrder("bid", "order-1", 2**252, 10.0, "XSGD/USDC", Decimal("25"))
        ],
        placed_at=10.0,
    )

    try:
        store.replace_active([placement])
        store.replace_active([placement])
        loaded = store.load_active({"XSGD/USDC"})
        assert len(loaded) == 1
        assert loaded[0].orders[0].order_id == "order-1"
        assert loaded[0].orders[0].uuid_int == 2**252
        assert loaded[0].orders[0].qty_base == Decimal("25")

        store.replace_active([])
        assert store.load_active({"XSGD/USDC"}) == []
    finally:
        store.close()


def test_state_store_keeps_unsettled_orders_and_positions_across_restarts(tmp_path):
    path = tmp_path / "state.sqlite"
    order = vr.UnsettledOrder(
        order_id="order-1",
        market="XSGD/USDC",
        side="ask",
        price=Decimal("1.0050"),
        expiration=1000,
        placed_at=10.0,
    )
    store = vr.StateStore(path)
    try:
        store.add_unsettled([order])
        store.add_unsettled([order])
        assert store.load_unsettled() == [order]
        store.settle_order(order, Decimal("25"))
    finally:
        store.close()

    store = vr.StateStore(path)
    try:
        assert store.load_unsettled() == []
        assert store.load_positions() == {
            "XSGD/USDC": Position(Decimal("-25"), Decimal("1.0050"), 10.0)
        }
    finally:
        store.close()


TESTNET_WALLET = {"owner": "0x" + "11" * 20, "chain_id": "11155111", "sera_contract": "0x8347"}


def test_state_store_belongs_to_the_first_wallet_and_network_that_uses_it(tmp_path):
    path = tmp_path / "state.sqlite"
    store = vr.StateStore(path)
    try:
        store.claim(TESTNET_WALLET)
    finally:
        store.close()

    store = vr.StateStore(path)
    try:
        # The same wallet on the same network keeps using it.
        store.claim(dict(TESTNET_WALLET))
        # Mainnet, or another wallet, is refused.
        for other in (
            {**TESTNET_WALLET, "chain_id": "1", "sera_contract": "0xb5c5"},
            {**TESTNET_WALLET, "owner": "0x" + "22" * 20},
        ):
            with pytest.raises(RuntimeError, match=(
                f"belongs to wallet 0x{'11' * 20} on chain 11155111 .*"
                "Set ROTOR_STATE_PATH to a separate state file")):
                store.claim(other)
    finally:
        store.close()


def test_a_state_file_from_before_identities_is_tied_to_its_next_run(tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (market TEXT PRIMARY KEY, net_base TEXT NOT NULL, "
               "price TEXT NOT NULL, opened_at REAL NOT NULL)")
    db.execute("INSERT INTO positions VALUES ('XSGD/USDC', '5', '0.9950', 10.0)")
    db.commit()
    db.close()

    store = vr.StateStore(path)
    try:
        store.claim(TESTNET_WALLET)
        assert set(store.load_positions()) == {"XSGD/USDC"}
        with pytest.raises(RuntimeError, match="belongs to"):
            store.claim({**TESTNET_WALLET, "chain_id": "1"})
    finally:
        store.close()


def _refs(*, quoted=(), skipped=()) -> vr.TickReferences:
    refs = vr.TickReferences()
    for market in quoted:
        refs.quoted[market] = object()
    for market in skipped:
        refs.skipped[market] = RuntimeError(f"{market} source down\nmore detail")
    return refs


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_raw(self, text, *, important=False):
        self.sent.append(text)


def test_a_brief_skip_is_not_alerted_and_a_long_one_is_alerted_once():
    notifier = _Notifier()
    alerts = vr.QuotingAlerts(
        {"XSGD/USDC", "EURC/USDC"}, notifier=notifier, log=logging.getLogger("test"), now=0.0)
    timeline = [
        (0, _refs(skipped=["EURC/USDC"], quoted=["XSGD/USDC"])),
        (300, _refs(quoted=["EURC/USDC", "XSGD/USDC"])),  # a blip: no message
        (600, _refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"])),
        (1200, _refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"])),
        (1500, _refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"])),
        (1800, _refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"])),
        (2100, _refs(quoted=["EURC/USDC", "XSGD/USDC"])),
    ]
    for now, refs in timeline:
        alerts.update(refs, placed=True, now=float(now))
        alerts.check(now=float(now))

    assert notifier.sent == [
        "⚠️ Not quoting these pairs; the other pairs are quoting:\n"
        "XSGD/USDC for 15m: XSGD/USDC source down",
        "✅ Quoting again:\nXSGD/USDC after 25m",
    ]


def test_not_quoting_episodes_survive_restarts(tmp_path):
    notifier = _Notifier()
    path = tmp_path / "state.sqlite"
    log = logging.getLogger("test")
    markets = {"XSGD/USDC", "EURC/USDC"}
    store = vr.StateStore(path)
    try:
        vr.QuotingAlerts(markets, state=store, notifier=notifier, log=log, now=0.0).update(
            _refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"]), placed=True, now=0.0)
    finally:
        store.close()
    assert notifier.sent == []
    # A --once fire 15 minutes later is a new process; the episode carried over.
    store = vr.StateStore(path)
    try:
        alerts = vr.QuotingAlerts(
            markets, state=store, notifier=notifier, log=log, carry_over=True, now=900.0)
        alerts.update(_refs(skipped=["XSGD/USDC"], quoted=["EURC/USDC"]), placed=True, now=900.0)
        alerts.update(_refs(quoted=["XSGD/USDC", "EURC/USDC"]), placed=True, now=1200.0)
        assert set(store.load_quoting()) == {vr.ANY_PAIR}
    finally:
        store.close()

    assert [text.splitlines()[0] for text in notifier.sent] == [
        "⚠️ Not quoting these pairs; the other pairs are quoting:", "✅ Quoting again:"]


def test_no_order_going_out_is_one_alert_with_each_pairs_reason_not_one_per_pair():
    # Both pairs go dark at 300 s. The no-order alert lists why and repeats
    # every window. XSGD's return is the "resumed" message; EURC, still dark,
    # was covered by that alert, so only its own return is reported.
    notifier = _Notifier()
    alerts = vr.QuotingAlerts(
        {"XSGD/USDC", "EURC/USDC"}, notifier=notifier, log=logging.getLogger("test"), now=0.0)
    alerts.update(_refs(quoted=["XSGD/USDC", "EURC/USDC"]), placed=True, now=0.0)
    for now in range(300, 2100, 300):
        alerts.check(now=float(now))
        alerts.update(_refs(skipped=["XSGD/USDC", "EURC/USDC"]), placed=False, now=float(now))
    for now in range(2100, 3000, 300):
        alerts.update(_refs(quoted=["XSGD/USDC"], skipped=["EURC/USDC"]), placed=True,
                      now=float(now))
    alerts.update(_refs(quoted=["XSGD/USDC", "EURC/USDC"]), placed=True, now=3000.0)

    assert notifier.sent == [
        "⚠️ VL quoting stalled: no order has gone out for 15m.\n"
        "EURC/USDC: EURC/USDC source down\n"
        "XSGD/USDC: XSGD/USDC source down",
        "⚠️ VL quoting stalled: no order has gone out for 30m.\n"
        "EURC/USDC: EURC/USDC source down\n"
        "XSGD/USDC: XSGD/USDC source down",
        "✅ VL quoting resumed after 35m without a placement.",
        "✅ Quoting again:\nEURC/USDC after 45m",
    ]


def test_a_continuous_restart_restarts_the_no_order_clock_but_keeps_pair_records(tmp_path):
    notifier = _Notifier()
    log = logging.getLogger("test")
    markets = {"XSGD/USDC", "EURC/USDC"}
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        # The last run placed at 0, then both pairs went dark until it stopped.
        alerts = vr.QuotingAlerts(markets, state=store, notifier=notifier, log=log, now=0.0)
        alerts.update(_refs(skipped=["XSGD/USDC", "EURC/USDC"]), placed=False, now=300.0)
        # Restarted hours later with both pairs still dark: their records are
        # old enough to alert, but with no pair quoting that is the no-order
        # alert's job, and its clock starts at boot.
        alerts = vr.QuotingAlerts(markets, state=store, notifier=notifier, log=log, now=9000.0)
        alerts.check(now=9000.0)
        alerts.update(_refs(skipped=["XSGD/USDC", "EURC/USDC"]), placed=False, now=9000.0)
        assert notifier.sent == []
        alerts.check(now=9900.0)
    finally:
        store.close()

    assert [text.splitlines()[0] for text in notifier.sent] == [
        "⚠️ VL quoting stalled: no order has gone out for 15m."]


def test_wise_being_unreachable_for_the_window_is_alerted_once(tmp_path):
    notifier = _Notifier()

    def outage():
        refs = _refs(quoted=["XSGD/USDC", "EURC/USDC"])
        refs.wise_outage["XSGD/USDC"] = RuntimeError("ReadTimeout")
        refs.wise_outage["EURC/USDC"] = RuntimeError("ReadTimeout")
        return refs

    log = logging.getLogger("test")
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        alerts = vr.QuotingAlerts(
            {"XSGD/USDC", "EURC/USDC"}, state=store, notifier=notifier, log=log, now=0.0)
        alerts.update(outage(), placed=True, now=0.0)
        alerts.update(outage(), placed=True, now=600.0)
        assert notifier.sent == []
        alerts.update(outage(), placed=True, now=900.0)
        alerts.update(outage(), placed=True, now=1200.0)
        # The episode is persisted, so --once fires share it.
        assert vr.WISE_OUTAGE in store.load_quoting()
        alerts.update(_refs(quoted=["XSGD/USDC", "EURC/USDC"]), placed=True, now=1500.0)
        assert vr.WISE_OUTAGE not in store.load_quoting()
    finally:
        store.close()

    assert len(notifier.sent) == 2
    assert notifier.sent[0].startswith("⚠️ Wise is not answering")
    assert notifier.sent[1].startswith("✅ Wise is answering again")



def test_state_store_drops_the_superseded_pending_returns_table(tmp_path):
    path = tmp_path / "state.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE pending_returns (market TEXT, side TEXT)")
    db.commit()
    db.close()

    vr.StateStore(path).close()

    tables = {
        row[0]
        for row in sqlite3.connect(path).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "pending_returns" not in tables
    assert {"unsettled_orders", "positions"} <= tables


def test_prune_fully_filled_placements_drops_only_known_full_fills():
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[
            LiveOrder("bid", "full", 1, 10.0, "XSGD/USDC", Decimal("10")),
            LiveOrder("ask", "partial", 2, 10.0, "XSGD/USDC", Decimal("10")),
            LiveOrder("ask", "unknown-size", 3, 10.0, "XSGD/USDC"),
        ],
        placed_at=10.0,
    )

    remaining = vr._prune_fully_filled_placements(
        [placement],
        {
            "full": Decimal("10"),
            "partial": Decimal("4"),
            "unknown-size": Decimal("99"),
        },
        logging.getLogger("test"),
    )

    assert len(remaining) == 1
    assert [order.order_id for order in remaining[0].orders] == [
        "partial",
        "unknown-size",
    ]


def test_load_live_placements_groups_vl_batches_and_filters_markets():
    class Client:
        def get_open_orders(self):
            return [
                {
                    "symbol": "XSGD/USDC",
                    "side": "bid",
                    "order_id": "standalone-1",
                    "uuid_int": "1",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "symbol": "XSGD/USDC",
                    "side": "ask",
                    "order_id": "vl-leg-1",
                    "uuid_int": "2",
                    "vl_batch_id": "batch-1",
                    "created_at": "2026-01-01T00:00:01Z",
                },
                {
                    "symbol": "XSGD/USDC",
                    "side": "bid",
                    "order_id": "vl-leg-2",
                    "uuid_int": "3",
                    "vl_batch_id": "batch-1",
                    "created_at": "2026-01-01T00:00:02Z",
                },
                {
                    "symbol": "EURC/USDC",
                    "side": "bid",
                    "order_id": "ignored",
                    "uuid_int": "4",
                },
            ]

    placements = vr._load_live_placements(
        Client(),
        {"XSGD/USDC"},
        logging.getLogger("test"),
        stop=vr.StopFlag(),
    )

    assert [placement.kind for placement in placements] == ["standalone", "vl"]
    assert placements[0].orders[0].order_id == "standalone-1"
    assert placements[0].orders[0].market == "XSGD/USDC"
    assert placements[1].vl_batch_id == "batch-1"
    assert [order.order_id for order in placements[1].orders] == ["vl-leg-1", "vl-leg-2"]
    assert [order.market for order in placements[1].orders] == ["XSGD/USDC", "XSGD/USDC"]


def test_placement_from_vl_result_applies_amendments_and_drops_cancelled():
    intents = [
        intent(market="XSGD/USDC", qty="100"),
        intent(market="EURC/USDC", qty="100"),
        intent(market="MYRC/USDC", qty="100"),
    ]
    result = VLBatchResult(
        vl_batch_id="b1",
        records=[OrderRecord("o0", 1), OrderRecord("o1", 2), OrderRecord("o2", 3)],
        amendments=[VLBatchAmendment(order_id="o1", actual_amount="60")],
        cancelled=["o2"],
    )

    placement = vr._placement_from_vl_result("USDC", result, intents)

    # The server-cancelled leg is not tracked as live.
    assert [o.order_id for o in placement.orders] == ["o0", "o1"]
    sizes = {o.order_id: o.qty_base for o in placement.orders}
    # Clipped leg tracks the accepted (amended) size so a full fill prunes it.
    assert sizes["o0"] == Decimal("100")
    assert sizes["o1"] == Decimal("60")


def test_group_intents_splits_oversized_same_token_group():
    intents = [intent(market=f"M{i}/USDC", spent="USDC") for i in range(5)]

    groups = vr.group_intents(intents, 2, 2)

    # 5 legs, batch max 2 -> [2, 2, 1]; the trailing singleton places standalone.
    assert [(len(rows), use_vl) for _, rows, use_vl in groups] == [
        (2, True),
        (2, True),
        (1, False),
    ]


def test_fit_budget_caps_adds_up_standalone_orders_on_one_token():
    first, second = intent(price="1", qty="60"), intent(market="EURC/USDC", price="1", qty="60")

    kept, left_out = vr._fit_budget_caps(
        [("USDC", [first, second], False)], vl_config(budget="100"), _LOG)

    assert kept == [("USDC", [first], False)]
    assert set(left_out) == {"EURC/USDC"}


def test_fit_budget_caps_counts_a_vl_batch_as_its_largest_leg():
    # Sera freezes only the largest leg of a VL batch: its legs share one budget
    # and only one can fill at a time. Eight legs that each use the whole budget
    # fit, as the Earn setup sizes them; a second batch on the same token does
    # not, and is left out rather than costing the tick (Sera's batch maximum
    # splits more legs than it holds into several batches).
    markets = ["XSGD", "EURC", "JPYC", "TGBP", "AXCNH", "AUDD", "CADC", "VCHF"]
    whole_budget = [intent(market=f"{base}/USDC", price="1", qty="100") for base in markets]
    overflow = [intent(market="MYRC/USDC", price="1", qty="100"),
                intent(market="IDRX/USDC", price="1", qty="100")]

    kept, left_out = vr._fit_budget_caps(
        [("USDC", whole_budget, True), ("USDC", overflow, True)], vl_config(budget="100"), _LOG)

    assert kept == [("USDC", whole_budget, True)]
    assert set(left_out) == {"MYRC/USDC", "IDRX/USDC"}


def test_fit_budget_caps_adds_up_separate_batches_and_standalone_orders():
    # Each batch locks its own largest leg, and a standalone order locks in full.
    batch_a = ("USDC", [intent(price="1", qty="40"), intent(market="EURC/USDC", price="1", qty="30")], True)
    batch_b = ("USDC", [intent(market="JPYC/USDC", price="1", qty="40"), intent(market="AUDD/USDC", price="1", qty="20")], True)
    solo = ("USDC", [intent(market="CADC/USDC", price="1", qty="21")], False)
    budget = vl_config(budget="100")

    assert vr._fit_budget_caps([batch_a, batch_b], budget, _LOG) == ([batch_a, batch_b], {})
    kept, left_out = vr._fit_budget_caps([batch_a, batch_b, solo], budget, _LOG)
    assert kept == [batch_a, batch_b]
    assert set(left_out) == {"CADC/USDC"}


def test_state_store_single_instance_lock(tmp_path):
    path = tmp_path / "state.sqlite"
    first = vr.StateStore(path)
    try:
        with pytest.raises(RuntimeError, match="another rotor instance"):
            vr.StateStore(path)
    finally:
        first.close()
    # Once the first instance releases the lock, a fresh start can re-acquire it.
    second = vr.StateStore(path)
    second.close()


def test_filled_base_qty_sums_nested_trades_and_accepts_flat_shapes():
    assert vr._filled_base_qty({"trades": [{"quantity": "1.5"}, {"quantity": "2.5"}]}) == Decimal("4.0")
    assert vr._filled_base_qty({"filled_base_amount": "3"}) == Decimal("3")
    assert vr._filled_base_qty({"quantity": "4"}) == Decimal("4")
    assert vr._filled_base_qty({"amount": "5"}) == Decimal("5")
    assert vr._filled_base_qty({}) == Decimal("0")


class _ExitStop:
    def __init__(self, *, forced: bool = False) -> None:
        self.forced = forced
        self.waits: list[float] = []

    def wait_forced(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.forced


class _CooldownClient:
    """Refuses cancels (Sera's cooldown) until `refusals` have been used up."""

    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.cancels = 0

    def cancel_order(self, order_id, uuid_int):
        self.cancels += 1
        if self.refusals:
            self.refusals -= 1
            return False
        return True


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    def send_raw(self, text, *, important=False):
        self.sent.append((text, important))


def _resting(placed_at: float) -> list[vr.LivePlacement]:
    return [vr.LivePlacement(
        kind="standalone", spent_token="USDC",
        orders=[LiveOrder("bid", "o-1", 1, placed_at)], placed_at=placed_at,
    )]


def test_exit_waits_out_seras_cancel_cooldown_then_cancels(monkeypatch):
    monkeypatch.setattr(vr.time, "time", lambda: 1000.0)
    client, stop, alerts = _CooldownClient(refusals=1), _ExitStop(), _Alerts()

    left = vr._cancel_before_exit(
        client, _resting(placed_at=900.0), logging.getLogger("test"),
        stop=stop, notifier=alerts, wait=True, order_ttl_seconds=600)

    # Placed 100 s ago: wait the rest of the 300 s cooldown (plus 5 s), then cancel.
    assert stop.waits == [205.0]
    assert (left, client.cancels, alerts.sent) == ([], 2, [])


def test_a_second_stop_skips_the_wait_and_reports_what_is_left(monkeypatch):
    monkeypatch.setattr(vr.time, "time", lambda: 1000.0)
    client, stop, alerts = _CooldownClient(refusals=1), _ExitStop(forced=True), _Alerts()

    left = vr._cancel_before_exit(
        client, _resting(placed_at=900.0), logging.getLogger("test"),
        stop=stop, notifier=alerts, wait=True, order_ttl_seconds=600)

    assert (len(left), client.cancels, stop.waits) == (1, 1, [])
    [(text, important)] = alerts.sent
    assert important and "1 order(s) could not be cancelled" in text
    assert "expire by about 1970-01-01 00:25 UTC" in text


class _SlowCancels:
    """Lists cancelled orders as open for the first `reads` reads."""

    def __init__(self, reads: int) -> None:
        self.reads = reads
        self.calls = 0

    def get_open_orders(self):
        self.calls += 1
        return [{"trade_id": "o-1"}] if self.calls <= self.reads else [{"trade_id": "other"}]


class _CountingStop:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self, _timeout: float) -> bool:
        self.waits += 1
        return False


def test_new_quotes_wait_until_seras_cancels_have_taken_effect():
    client, stop = _SlowCancels(reads=2), _CountingStop()

    vr._wait_until_gone(client, {"o-1"}, logging.getLogger("test"), stop=stop)

    # Open on two reads, gone on the third: two short waits in between.
    assert (client.calls, stop.waits) == (3, 2)


def test_placing_goes_ahead_after_a_few_seconds_if_a_cancel_lingers():
    client, stop = _SlowCancels(reads=99), _CountingStop()

    vr._wait_until_gone(client, {"o-1"}, logging.getLogger("test"), stop=stop, polls=4)

    assert (client.calls, stop.waits) == (4, 4)


class _FlakyOpenOrders:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def get_open_orders(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("blip")
        return [{"symbol": "XSGD/USDC", "side": "bid", "trade_id": "mine", "uuid_int": "7"}]


def test_reattach_retries_a_blip_then_reads_sera():
    client, stop = _FlakyOpenOrders(failures=2), _CountingStop()

    placements = vr._load_live_placements(
        client, {"XSGD/USDC"}, logging.getLogger("test"), stop=stop, known={"mine"})

    assert (client.calls, stop.waits) == (3, 2)
    assert [order.order_id for placement in placements for order in placement.orders] == ["mine"]


def test_reattach_falls_back_to_local_state_when_sera_stays_unreachable():
    stored = [vr.LivePlacement(kind="standalone", spent_token="USDC",
                               orders=[LiveOrder("bid", "stored", 1, 0.0)], placed_at=0.0)]

    placements = vr._load_live_placements(
        _FlakyOpenOrders(failures=99), {"XSGD/USDC"}, logging.getLogger("test"),
        stop=_CountingStop(), fallback=stored)

    assert placements == stored
