"""Opt-in live Sera testnet scenarios for the VL runner.

These tests intentionally place real testnet orders. They are not part of the
default suite and should be run only with small funded maker/taker wallets.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from rotor.config import get_config
from rotor.mm import vl_runner as vr
from rotor.mm.exchange.client import (
    OrderRecord,
    SeraClient,
    SignedOrder,
    SignedVLBatch,
    VLBatchResult,
)
from rotor.price_reference.models import PriceObservation


def _flag(name: str) -> bool:
    return str(get_config(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        not _flag("ROTOR_RUN_TESTNET"),
        reason="set ROTOR_RUN_TESTNET=1 to run real Sera testnet tests",
    ),
]


@dataclass(frozen=True)
class _PlacedOrder:
    market: str
    side: str
    qty_base: Decimal
    price: Decimal
    order_id: str
    uuid_int: int
    path: str
    batch_id: str | None = None


class _StaticRateSource:
    name = "testnet-static"

    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    def quote(self, base: str, quote: str, *, amount=None) -> PriceObservation:
        base = base.upper()
        quote = quote.upper()
        return PriceObservation(
            source=self.name,
            pair=f"{base}/{quote}",
            rate=self.rate,
            ts=time.time(),
        )

    # Testnet tokens have no real Wise market: settle at the static rate, no fee.
    default_max_age_s = 600.0

    def sell_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        return self.quote(base, quote)

    def buy_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        return self.quote(base, quote)


class _RecordingClient:
    """Thin recorder around a real Sera client used by the runner."""

    def __init__(self, inner: SeraClient) -> None:
        self.inner = inner
        self.placed_orders: list[_PlacedOrder] = []
        self.vl_batches: list[VLBatchResult] = []
        self.cancel_events: list[tuple[str, str, bool]] = []
        # Signed but not yet sent: the legs/terms to record once Sera accepts.
        self._legs: dict[str, list] = {}
        self._terms: dict[str, tuple] = {}

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def sign_vl_batch(self, legs) -> SignedVLBatch:
        batch = self.inner.sign_vl_batch(legs)
        self._legs[batch.vl_batch_id] = list(legs)
        return batch

    def send_vl_batch(self, batch: SignedVLBatch) -> VLBatchResult:
        result = self.inner.send_vl_batch(batch)
        self.vl_batches.append(result)
        legs = self._legs.pop(batch.vl_batch_id)
        for leg, record in zip(legs, result.records, strict=True):
            self.placed_orders.append(
                _PlacedOrder(
                    market=leg.market,
                    side=leg.side,
                    qty_base=leg.qty_base,
                    price=leg.price,
                    order_id=record.order_id,
                    uuid_int=record.uuid_int,
                    path="vl",
                    batch_id=result.vl_batch_id,
                )
            )
        return result

    def sign_order(
        self,
        market: str,
        side: str,
        qty_base: Decimal,
        price: Decimal,
        expiration: int,
    ) -> SignedOrder:
        order = self.inner.sign_order(market, side, qty_base, price, expiration)
        self._terms[order.order_id] = (market, side, qty_base, price)
        return order

    def send_order(self, order: SignedOrder) -> OrderRecord:
        record = self.inner.send_order(order)
        market, side, qty_base, price = self._terms.pop(order.order_id)
        self.placed_orders.append(
            _PlacedOrder(
                market=market,
                side=side,
                qty_base=qty_base,
                price=price,
                order_id=record.order_id,
                uuid_int=record.uuid_int,
                path="standalone",
            )
        )
        return record

    def cancel_vl_batch(self, vl_batch_id: str) -> bool:
        ok = self.inner.cancel_vl_batch(vl_batch_id)
        self.cancel_events.append(("vl", vl_batch_id, ok))
        return ok

    def cancel_order(self, order_id: str, uuid_int: int) -> bool:
        ok = self.inner.cancel_order(order_id, uuid_int)
        self.cancel_events.append(("standalone", order_id, ok))
        return ok


class _StopFlag:
    def __init__(self) -> None:
        self.requested = False

    def install(self) -> None:
        return None


def _value(name: str, default: str = "") -> str:
    return str(get_config(name, default) or "").strip()


def _required_value(name: str, purpose: str) -> str:
    value = _value(name)
    if not value:
        pytest.skip(f"set {name} for {purpose}")
    return value


def _decimal(name: str, default: str | None, purpose: str) -> Decimal:
    raw = _value(name, default or "")
    if not raw:
        pytest.skip(f"set {name} for {purpose}")
    try:
        return Decimal(raw)
    except InvalidOperation:
        pytest.skip(f"{name} must be a valid decimal for {purpose}")


def _int(name: str, default: str, purpose: str) -> int:
    raw = _value(name, default)
    try:
        return int(raw)
    except ValueError:
        pytest.skip(f"{name} must be an integer for {purpose}")


def _float(name: str, default: str, purpose: str) -> float:
    raw = _value(name, default)
    try:
        return float(raw)
    except ValueError:
        pytest.skip(f"{name} must be numeric for {purpose}")


def _require_scenario_ack() -> None:
    if not _flag("ROTOR_RUN_TESTNET_SCENARIOS"):
        pytest.skip("set ROTOR_RUN_TESTNET_SCENARIOS=1 for live runner scenarios")
    if not _flag("ROTOR_TESTNET_ACK_LIVE_ORDER_RISK"):
        pytest.skip(
            "set ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 to acknowledge this test "
            "places real testnet orders"
        )


def _signing_client(
    *,
    private_key_name: str,
    api_key_name: str,
    api_secret_name: str,
) -> SeraClient:
    private_key = _value(private_key_name)
    if not private_key:
        pytest.skip(f"set {private_key_name} for signed testnet scenarios")
    api_key = _value(api_key_name) or _value("SERA_API_KEY")
    api_secret = _value(api_secret_name) or _value("SERA_API_SECRET")
    if not (api_key and api_secret):
        pytest.skip(
            f"set {api_key_name}/{api_secret_name}, or common SERA_API_KEY/"
            "SERA_API_SECRET, for signed testnet scenarios"
        )
    account = Account.from_key(private_key)

    def signer(domain: dict, types: dict, message: dict) -> str:
        signable = encode_typed_data(
            domain_data=domain,
            message_types=types,
            message_data=message,
        )
        signature = account.sign_message(signable).signature.hex()
        return signature if signature.startswith("0x") else "0x" + signature

    return SeraClient(
        owner_address=account.address,
        signer=signer,
        api_key=api_key,
        api_secret=api_secret,
        base_url=_value("SERA_BASE_URL", "https://api.testnet.sera.cx/api/v1"),
        timeout=float(_value("ROTOR_TESTNET_TIMEOUT", "15")),
    )


def _opposite_side(side: str) -> str:
    return "ask" if side == "bid" else "bid"


def _market(value: str) -> str:
    return value.upper().replace("_", "/")


def _write_config(
    path: Path,
    *,
    target_market: str,
    sibling_market: str,
    budget_token: str,
    budget_amount: Decimal,
    qty_base: Decimal,
    fixed_bps: Decimal,
    return_bps: Decimal,
) -> None:
    path.write_text(
        "\n".join([
            f'fixed_bps = "{fixed_bps}"',
            f'return_bps = "{return_bps}"',
            "",
            "[[budget]]",
            f'token = "{budget_token}"',
            f'amount = "{budget_amount}"',
            "",
            "[[pair]]",
            f'market = "{target_market}"',
            'price_source = "wise"',
            f'qty_base = "{qty_base}"',
            "",
            "[[pair]]",
            f'market = "{sibling_market}"',
            'price_source = "wise"',
            f'qty_base = "{qty_base}"',
            "",
        ]),
        encoding="utf-8",
    )


def _first_order(
    maker: _RecordingClient,
    *,
    market: str,
    side: str,
) -> _PlacedOrder:
    for order in maker.placed_orders:
        if order.market == market and order.side == side:
            return order
    raise AssertionError(f"runner did not place {market} {side}")


def _poll_for_fills(
    client: SeraClient,
    order_ids: list[str],
    *,
    wait_seconds: float,
) -> list[dict]:
    deadline = time.time() + wait_seconds
    while True:
        rows: list[dict] = []
        for order_id in order_ids:
            rows.extend(client.get_order_fills(order_id))
        if rows or time.time() >= deadline:
            return rows
        time.sleep(1.0)


def _best_effort_cancel_open_orders(
    client: SeraClient,
    *,
    markets: set[str],
    wait_seconds: float,
) -> None:
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    errors: list[str] = []
    seen_batches: set[str] = set()
    try:
        rows = client.get_open_orders()
    except Exception as exc:  # pragma: no cover - depends on live testnet API.
        warnings.warn(f"testnet cleanup open-order read failed: {exc}", stacklevel=2)
        return
    for row in rows:
        market = str(row.get("symbol") or row.get("market") or "").upper()
        if market not in markets:
            continue
        batch_id = str(row.get("vl_batch_id") or "").strip()
        if batch_id:
            if batch_id in seen_batches:
                continue
            seen_batches.add(batch_id)
            try:
                if not client.cancel_vl_batch(batch_id):
                    errors.append(f"VL batch {batch_id}: cancel returned false")
            except Exception as exc:  # pragma: no cover - live API dependent.
                errors.append(f"VL batch {batch_id}: {type(exc).__name__}: {exc}")
            continue
        order_id = row.get("trade_id") or row.get("order_id")
        uuid_int = row.get("uuid_int")
        if not order_id or not uuid_int:
            continue
        try:
            if not client.cancel_order(str(order_id), int(uuid_int)):
                errors.append(f"order {order_id}: cancel returned false")
        except Exception as exc:  # pragma: no cover - live API dependent.
            errors.append(f"order {order_id}: {type(exc).__name__}: {exc}")
    if errors:
        warnings.warn("testnet cleanup problems: " + "; ".join(errors), stacklevel=2)


def _install_runner_hooks(
    monkeypatch,
    *,
    maker: _RecordingClient,
    mid: Decimal,
    stop: _StopFlag,
    fill_after_first_tick,
    wait_between_ticks: float,
) -> None:
    source = _StaticRateSource(mid)
    calls = 0
    # Tick 1 quotes and is filled; the filled order expires and its fill settles
    # into a position by tick 2 or 3, which then quotes the return order.
    ticks = 3

    def build_client(*, dry_run: bool, log):
        assert not dry_run
        return maker

    def sleep_remaining(_started: float, _poll_seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            fill_after_first_tick()
        if calls < ticks:
            if wait_between_ticks > 0:
                time.sleep(wait_between_ticks)
            return
        stop.requested = True

    monkeypatch.setattr(vr, "build_client", build_client)
    monkeypatch.setattr(vr, "load_price_reference_source", lambda _name: source)
    monkeypatch.setattr(vr, "StopFlag", lambda: stop)
    monkeypatch.setattr(vr, "sleep_remaining", sleep_remaining)


@pytest.mark.parametrize("maker_side", ["bid", "ask"])
def test_testnet_vl_runner_fill_then_return_requote(
    monkeypatch,
    tmp_path,
    maker_side: str,
):
    _require_scenario_ack()
    purpose = "live runner fill/return scenario"
    target_market = _market(_required_value("ROTOR_TESTNET_MARKET", purpose))
    sibling_market = _market(_required_value("ROTOR_TESTNET_SECOND_MARKET", purpose))
    mid = _decimal("ROTOR_TESTNET_MID", _value("ROTOR_TESTNET_PRICE"), purpose)
    qty_base = _decimal("ROTOR_TESTNET_QTY_BASE", None, purpose)
    fixed_bps = _decimal("ROTOR_TESTNET_FIXED_BPS", "50", purpose)
    return_bps = _decimal("ROTOR_TESTNET_RETURN_BPS", "1", purpose)
    budget_amount = _decimal("ROTOR_TESTNET_BUDGET_AMOUNT", "1000000", purpose)
    ttl_seconds = _int("ROTOR_TESTNET_ORDER_TTL", "90", purpose)
    fill_wait = _float("ROTOR_TESTNET_FILL_WAIT_SECONDS", "20", purpose)
    wait_between_ticks = _float(
        "ROTOR_TESTNET_SCENARIO_BETWEEN_TICKS_SECONDS",
        "305",
        purpose,
    )
    cleanup_wait = _float("ROTOR_TESTNET_CLEANUP_WAIT_SECONDS", "0", purpose)

    maker = _RecordingClient(_signing_client(
        private_key_name="ROTOR_TESTNET_MAKER_PRIVATE_KEY",
        api_key_name="ROTOR_TESTNET_MAKER_API_KEY",
        api_secret_name="ROTOR_TESTNET_MAKER_API_SECRET",
    ))
    taker = _signing_client(
        private_key_name="ROTOR_TESTNET_TAKER_PRIVATE_KEY",
        api_key_name="ROTOR_TESTNET_TAKER_API_KEY",
        api_secret_name="ROTOR_TESTNET_TAKER_API_SECRET",
    )
    maker.bootstrap()
    taker.bootstrap()
    if maker.owner_address == taker.owner_address:
        pytest.skip("live runner scenarios require distinct maker and taker wallets")
    if target_market not in maker.markets_by_symbol:
        pytest.skip(f"{target_market} is not available on configured Sera testnet")
    if sibling_market not in maker.markets_by_symbol:
        pytest.skip(f"{sibling_market} is not available on configured Sera testnet")

    target_info = maker.markets_by_symbol[target_market]
    sibling_info = maker.markets_by_symbol[sibling_market]
    if maker_side == "bid":
        if target_info.quote_symbol.upper() != sibling_info.quote_symbol.upper():
            pytest.skip("bid VL scenario needs two markets with the same quote token")
        budget_token = target_info.quote_symbol.upper()
    elif target_info.base_symbol.upper() == sibling_info.base_symbol.upper():
        budget_token = target_info.base_symbol.upper()
    else:
        budget_token = target_info.quote_symbol.upper()

    config_path = tmp_path / "vl-testnet.toml"
    _write_config(
        config_path,
        target_market=target_market,
        sibling_market=sibling_market,
        budget_token=budget_token,
        budget_amount=budget_amount,
        qty_base=qty_base,
        fixed_bps=fixed_bps,
        return_bps=return_bps,
    )

    taker_records: list[OrderRecord] = []
    stop = _StopFlag()

    def fill_after_first_tick() -> None:
        maker_order = _first_order(maker, market=target_market, side=maker_side)
        taker_record = taker.place_order(
            target_market,
            _opposite_side(maker_side),
            qty_base,
            maker_order.price,
            taker.expiration_from_server_time(ttl_seconds),
        )
        taker_records.append(taker_record)
        fills = _poll_for_fills(
            maker.inner,
            [maker_order.order_id],
            wait_seconds=fill_wait,
        )
        if not fills:
            fills = _poll_for_fills(
                taker,
                [taker_record.order_id],
                wait_seconds=fill_wait,
            )
        assert fills, "taker did not fill the maker quote within wait window"

    _install_runner_hooks(
        monkeypatch,
        maker=maker,
        mid=mid,
        stop=stop,
        fill_after_first_tick=fill_after_first_tick,
        wait_between_ticks=wait_between_ticks,
    )

    try:
        result = vr.run(
            config_path=config_path,
            poll_seconds=wait_between_ticks,
            order_ttl_seconds=ttl_seconds,
            min_requote_seconds=0,
            max_reference_age_s=600,
            cancel_on_exit=True,
            log_level="ERROR",
            state_path=tmp_path / "state.sqlite",
        )
    finally:
        _best_effort_cancel_open_orders(
            maker.inner,
            markets={target_market, sibling_market},
            wait_seconds=cleanup_wait,
        )
        for taker_record in taker_records:
            try:
                taker.cancel_order(taker_record.order_id, taker_record.uuid_int)
            except Exception:  # pragma: no cover - filled taker orders may not cancel.
                pass

    # The fill settled into a position at the maker order's price.
    maker_order = _first_order(maker, market=target_market, side=maker_side)
    store = vr.StateStore(tmp_path / "state.sqlite")
    try:
        position = store.load_positions().get(target_market)
    finally:
        store.close()
    assert position is not None, "the maker fill did not settle into a position"
    assert position.net_base == (qty_base if maker_side == "bid" else -qty_base)
    assert position.price == maker_order.price

    # A later tick quoted it back without crossing the fill price.
    return_side = _opposite_side(maker_side)
    return_orders = [
        order for order in maker.placed_orders
        if (
            order.market == target_market
            and order.side == return_side
            and order.order_id != maker_order.order_id
            and (
                order.price <= maker_order.price if return_side == "bid"
                else order.price >= maker_order.price
            )
        )
    ]

    assert result == 0
    assert return_orders
    assert maker.cancel_events
    if maker_side == "bid":
        assert _first_order(maker, market=target_market, side="bid").path == "vl"
