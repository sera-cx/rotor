"""Additional mock-backed coverage for VL runner edge behavior."""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal

import pytest

from rotor.mm import vl_runner as vr
from rotor.mm.exchange import errors
from rotor.mm.exchange.client import (
    MarketInfo,
    OrderRecord,
    VLBatchAmendment,
    VLBatchResult,
    VLCancelledLeg,
)
from rotor.mm.positions import Position
from rotor.mm.quote_engine import LiveOrder
from rotor.mm.vl_config import BudgetConfig, VLConfig


def _intent(
    market: str = "XSGD/USDC",
    side: str = "bid",
    spent: str = "USDC",
    *,
    price: str = "1",
    qty: str = "100",
) -> vr.OrderIntent:
    return vr.OrderIntent(
        market=market,
        side=side,
        qty_base=Decimal(qty),
        price=Decimal(price),
        expiration=999,
        spent_token=spent,
        benchmark_rate=Decimal("1"),
    )


def _config(*, budget: str = "100") -> VLConfig:
    return VLConfig(
        budgets=[BudgetConfig("USDC", Decimal(budget))],
        pairs=[],
        fixed_bps=Decimal("8"),
        return_bps=Decimal("1"),
    )


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
        min_bid_quote_amount=Decimal("1"),
        min_ask_amount=Decimal("1"),
    )


def test_validate_budget_caps_allows_exact_bid_cap():
    vr._validate_budget_caps([_intent(price="1", qty="100")], _config(budget="100"))


def test_validate_budget_caps_rejects_ask_quantity_above_cap():
    intent = _intent(side="ask", spent="USDC", price="1", qty="100.01")

    try:
        vr._validate_budget_caps([intent], _config(budget="100"))
    except ValueError as exc:
        assert "above configured budget" in str(exc)
    else:  # pragma: no cover - defensive assertion branch.
        raise AssertionError("expected budget cap rejection")


def test_group_intents_uses_standalone_for_unbudgeted_multi_leg_group():
    groups = vr.group_intents(
        [
            _intent(spent="XSGD"),
            _intent(market="EURC/USDC", spent="XSGD"),
        ],
        budget_tokens={"USDC"},
        vl_min_size=2,
    )

    assert groups == [
        ("XSGD", groups[0][1], False),
    ]


def test_place_groups_uses_vl_batch_and_tracks_intents():
    class Client:
        vl_batch_min = 2

        def place_vl_batch(self, legs):
            assert len(legs) == 2
            return VLBatchResult(
                vl_batch_id="batch-1",
                order_ids=["leg-1", "leg-2"],
                records=[
                    OrderRecord("leg-1", 101),
                    OrderRecord("leg-2", 102),
                ],
                amendments=[],
                cancelled=[],
                fills=[],
                vl_group=None,
            )

    intents = [_intent(), _intent(market="EURC/USDC")]
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
    assert placement.intent_by_order_id == {"leg-1": intents[0], "leg-2": intents[1]}


def test_place_groups_cancels_partial_standalone_placements_on_failure():
    calls: list[tuple[str, str]] = []

    class Client:
        vl_batch_min = 2

        def place_order_previewed(self, market, side, qty_base, price, expiration):
            calls.append(("place", market))
            if market == "EURC/USDC":
                raise RuntimeError("second failed")
            return OrderRecord("solo-1", 1)

        def cancel_order(self, order_id, uuid_int):
            calls.append(("cancel", order_id))
            return True

    placements = vr._place_groups(
        Client(),
        [("USDC", [_intent(), _intent(market="EURC/USDC")], False)],
        logging.getLogger("test"),
    )

    assert placements == []
    assert calls == [
        ("place", "XSGD/USDC"),
        ("place", "EURC/USDC"),
        ("cancel", "solo-1"),
    ]


def test_cancel_placements_keeps_cooldown_vl_batch():
    placement = vr.LivePlacement(
        kind="vl",
        spent_token="USDC",
        orders=[LiveOrder("vl", "leg-1", 1, 0.0)],
        placed_at=0.0,
        vl_batch_id="batch-1",
    )

    class Client:
        def cancel_vl_batch(self, vl_batch_id):
            return False

    remaining = vr._cancel_placements(Client(), [placement], logging.getLogger("test"))

    assert remaining == [placement]


def test_cancel_boot_placements_waits_once_then_retries(monkeypatch):
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[LiveOrder("bid", "order-1", 1, 0.0)],
        placed_at=0.0,
    )
    sleeps: list[float] = []

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def cancel_order(self, order_id, uuid_int):
            self.calls += 1
            return self.calls > 1

    client = Client()
    monkeypatch.setattr(vr.time, "sleep", lambda seconds: sleeps.append(seconds))

    remaining = vr._cancel_boot_placements(client, [placement], logging.getLogger("test"))

    assert remaining == []
    assert sleeps == [vr.BOOT_CANCEL_RETRY_SECONDS]
    assert client.calls == 2


def test_state_store_dedupes_and_prunes_unresolved_state(tmp_path):
    store = vr.StateStore(tmp_path / "state.sqlite")
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[
            LiveOrder(
                "bid",
                "order-1",
                2**252,
                10.0,
                "XSGD/USDC",
                Decimal("25"),
            )
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
            LiveOrder(
                "bid",
                "full",
                1,
                10.0,
                "XSGD/USDC",
                Decimal("10"),
            ),
            LiveOrder(
                "ask",
                "partial",
                2,
                10.0,
                "XSGD/USDC",
                Decimal("10"),
            ),
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
    )

    assert [placement.kind for placement in placements] == ["standalone", "vl"]
    assert placements[0].orders[0].order_id == "standalone-1"
    assert placements[0].orders[0].market == "XSGD/USDC"
    assert placements[1].vl_batch_id == "batch-1"
    assert [order.order_id for order in placements[1].orders] == ["vl-leg-1", "vl-leg-2"]
    assert [order.market for order in placements[1].orders] == ["XSGD/USDC", "XSGD/USDC"]
    assert placements[1].intent_by_order_id is None


def test_placement_from_vl_result_applies_amendments_and_drops_cancelled():
    intents = [
        _intent(market="XSGD/USDC", qty="100"),
        _intent(market="EURC/USDC", qty="100"),
        _intent(market="MYRC/USDC", qty="100"),
    ]
    result = VLBatchResult(
        vl_batch_id="b1",
        order_ids=["o0", "o1", "o2"],
        records=[OrderRecord("o0", 1), OrderRecord("o1", 2), OrderRecord("o2", 3)],
        amendments=[VLBatchAmendment(
            order_id="o1", original_amount="100", actual_amount="60", reason="budget_clip")],
        cancelled=[VLCancelledLeg(order_id="o2", reason="stp")],
        fills=[],
        vl_group=None,
    )

    placement = vr._placement_from_vl_result("USDC", result, intents)

    # The server-cancelled leg is not tracked as live.
    assert [o.order_id for o in placement.orders] == ["o0", "o1"]
    assert "o2" not in placement.intent_by_order_id
    sizes = {o.order_id: o.qty_base for o in placement.orders}
    # Clipped leg tracks the accepted (amended) size so a full fill prunes it.
    assert sizes["o0"] == Decimal("100")
    assert sizes["o1"] == Decimal("60")


def test_cancel_placements_drops_orders_sera_reports_gone():
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[LiveOrder("bid", "gone-1", 1, 0.0)],
        placed_at=0.0,
    )

    class Client:
        def cancel_order(self, order_id, uuid_int):
            raise errors.SeraError("ORDER_NOT_FOUND", "no such order", 404)

    remaining = vr._cancel_placements(Client(), [placement], logging.getLogger("test"))

    assert remaining == []


def test_cancel_placements_keeps_retryable_failures():
    placement = vr.LivePlacement(
        kind="standalone",
        spent_token="USDC",
        orders=[LiveOrder("bid", "maybe-live", 1, 0.0)],
        placed_at=0.0,
    )

    class Client:
        def cancel_order(self, order_id, uuid_int):
            raise errors.SeraError("SERVER_ERROR", "boom", 500)

    remaining = vr._cancel_placements(Client(), [placement], logging.getLogger("test"))

    # A 5xx might mean the order is still live, so it stays tracked for retry.
    assert remaining == [placement]


def test_group_intents_splits_oversized_same_token_group():
    intents = [_intent(market=f"M{i}/USDC", spent="USDC") for i in range(5)]

    groups = vr.group_intents(intents, {"USDC"}, 2, 2)

    # 5 legs, batch max 2 -> [2, 2, 1]; the trailing singleton places standalone.
    assert [(len(rows), use_vl) for _, rows, use_vl in groups] == [
        (2, True),
        (2, True),
        (1, False),
    ]


def test_validate_budget_caps_rejects_aggregate_over_budget():
    intents = [
        _intent(price="1", qty="60"),
        _intent(market="EURC/USDC", price="1", qty="60"),
    ]

    with pytest.raises(ValueError, match="aggregate"):
        vr._validate_budget_caps(intents, _config(budget="100"))


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
