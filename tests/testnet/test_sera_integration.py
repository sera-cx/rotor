"""Opt-in tests against the real Sera testnet API."""

from __future__ import annotations

import time
import warnings
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from rotor.config import get_config
from rotor.mm.exchange import eip712
from rotor.mm.exchange.client import OrderRecord, SeraClient, VLBatchResult, VLLeg

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _flag(name: str) -> bool:
    return str(get_config(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


pytestmark = [
    pytest.mark.testnet,
    pytest.mark.skipif(
        not _flag("ROTOR_RUN_TESTNET"),
        reason="set ROTOR_RUN_TESTNET=1 to run real Sera testnet tests",
    ),
]


def _value(name: str, default: str = "") -> str:
    return str(get_config(name, default) or "").strip()


def _required_value(name: str, purpose: str) -> str:
    value = _value(name)
    if not value:
        pytest.skip(f"set {name} for {purpose}")
    return value


def _required_decimal(name: str, purpose: str) -> Decimal:
    raw = _required_value(name, purpose)
    try:
        return Decimal(raw)
    except InvalidOperation:
        pytest.skip(f"{name} must be a valid decimal for {purpose}")


def _optional_decimal(name: str, default: Decimal, purpose: str) -> Decimal:
    raw = _value(name)
    if not raw:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation:
        pytest.skip(f"{name} must be a valid decimal for {purpose}")


def _owner_address(*, required: bool = False) -> str:
    for name in ("ROTOR_TESTNET_OWNER_ADDRESS", "ROTOR_OWNER_ADDRESS", "SERA_OWNER_ADDRESS"):
        value = _value(name)
        if value:
            return value
    if required:
        pytest.skip("set ROTOR_TESTNET_OWNER_ADDRESS for this testnet check")
    return ZERO_ADDRESS


def _client(*, owner_address: str | None = None) -> SeraClient:
    def no_signer(_domain, _types, _message):
        raise AssertionError("testnet read-only checks must not sign")

    return SeraClient(
        owner_address=owner_address or _owner_address(),
        signer=no_signer,
        api_key=_value("SERA_API_KEY"),
        api_secret=_value("SERA_API_SECRET"),
        base_url=_value("SERA_BASE_URL", "https://api.testnet.sera.cx/api/v1"),
        timeout=float(_value("ROTOR_TESTNET_TIMEOUT", "15")),
    )


def _signing_client(
    *,
    private_key_name: str,
    api_key_name: str,
    api_secret_name: str,
) -> SeraClient:
    private_key = _value(private_key_name)
    if not private_key:
        pytest.skip(f"set {private_key_name} for signed testnet checks")
    api_key = _value(api_key_name) or _value("SERA_API_KEY")
    api_secret = _value(api_secret_name) or _value("SERA_API_SECRET")
    if not (api_key and api_secret):
        pytest.skip(
            f"set {api_key_name}/{api_secret_name}, or common SERA_API_KEY/"
            "SERA_API_SECRET, for signed testnet checks"
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


def _order_inputs() -> tuple[str, str, Decimal, Decimal]:
    market = _value("ROTOR_TESTNET_MARKET")
    qty = _value("ROTOR_TESTNET_QTY_BASE")
    price = _value("ROTOR_TESTNET_PRICE")
    if not (market and qty and price):
        pytest.skip(
            "set ROTOR_TESTNET_MARKET, ROTOR_TESTNET_QTY_BASE, and "
            "ROTOR_TESTNET_PRICE for the signing-parity check"
        )
    return (
        market.upper().replace("_", "/"),
        _value("ROTOR_TESTNET_SIDE", "bid").lower(),
        Decimal(qty),
        Decimal(price),
    )


def _require_api_credentials() -> None:
    if not (_value("SERA_API_KEY") and _value("SERA_API_SECRET")):
        pytest.skip("set SERA_API_KEY and SERA_API_SECRET for authenticated testnet checks")


def _opposite_side(side: str) -> str:
    return "ask" if side == "bid" else "bid"


def _best_effort_cleanup(
    action: Callable[[], bool],
    errors: list[str],
    label: str,
) -> None:
    try:
        ok = action()
    except Exception as exc:  # pragma: no cover - depends on live testnet API.
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return
    if not ok:
        errors.append(f"{label}: cancel returned false, likely cooldown")


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


def test_testnet_bootstrap_loads_public_config_health_and_markets():
    client = _client()

    client.bootstrap()

    assert client.eip712_domain
    assert client.eip712_domain.get("chainId")
    assert client.eip712_domain.get("verifyingContract")
    assert 0 <= client.executor_id <= eip712.EXECUTOR_MAX
    assert client.markets_by_symbol
    assert client.vl_batch_min >= 1
    assert client.vl_batch_max >= client.vl_batch_min


def test_testnet_server_time_returns_recent_unix_timestamp():
    server_time = _client().get_server_time()

    assert server_time > 1_700_000_000
    assert server_time < int(time.time()) + 24 * 60 * 60


def test_testnet_preview_returns_the_struct_rotor_builds_locally():
    # Rotor signs only orders it builds itself. Testnet still exposes
    # POST /orders/preview, so ask it for the same order and require the
    # server's canonical struct to equal rotor's, field for field. Non-placing.
    owner = _owner_address(required=True)
    market, side, qty_base, price = _order_inputs()
    client = _client(owner_address=owner)
    client.bootstrap()
    if market not in client.markets_by_symbol:
        pytest.skip(f"{market} is not available on configured Sera testnet")

    expiration = client.expiration_from_server_time(600)
    order = client.build_order(market, side, qty_base, price, expiration)
    body = client._request(
        "POST", "/orders/preview", json_body=order.wire, authed=False, expect_status=(200, 201))

    assert "signature" not in order.wire
    assert {key: str(value).lower() for key, value in body["eip712_order"].items()} == {
        key: str(value).lower() for key, value in order.struct.items()
    }


def test_testnet_authenticated_open_orders_read():
    _require_api_credentials()
    client = _client(owner_address=_owner_address(required=True))

    rows = client.get_open_orders()

    assert isinstance(rows, list)


def test_testnet_authenticated_order_fills_read_when_order_id_is_provided():
    _require_api_credentials()
    order_id = _value("ROTOR_TESTNET_FILL_ORDER_ID")
    if not order_id:
        pytest.skip("set ROTOR_TESTNET_FILL_ORDER_ID to read real fills")

    rows = _client(owner_address=_owner_address(required=True)).get_order_fills(order_id)

    assert isinstance(rows, list)


def test_testnet_two_wallet_vl_maker_taker_fill_flow():
    if not _flag("ROTOR_RUN_TESTNET_E2E"):
        pytest.skip("set ROTOR_RUN_TESTNET_E2E=1 for signed maker/taker testnet flow")
    if not _flag("ROTOR_TESTNET_ACK_LIVE_ORDER_RISK"):
        pytest.skip(
            "set ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 to acknowledge this test "
            "places real testnet orders"
        )

    maker = _signing_client(
        private_key_name="ROTOR_TESTNET_MAKER_PRIVATE_KEY",
        api_key_name="ROTOR_TESTNET_MAKER_API_KEY",
        api_secret_name="ROTOR_TESTNET_MAKER_API_SECRET",
    )
    taker = _signing_client(
        private_key_name="ROTOR_TESTNET_TAKER_PRIVATE_KEY",
        api_key_name="ROTOR_TESTNET_TAKER_API_KEY",
        api_secret_name="ROTOR_TESTNET_TAKER_API_SECRET",
    )
    maker.bootstrap()
    taker.bootstrap()
    if maker.vl_batch_min > 2 or maker.vl_batch_max < 2:
        pytest.skip(
            "test places two maker VL legs; Sera reports "
            f"min={maker.vl_batch_min} max={maker.vl_batch_max}"
        )

    target_market = _required_value(
        "ROTOR_TESTNET_MARKET",
        "two-wallet maker/taker E2E",
    ).upper().replace("_", "/")
    sibling_market = _required_value(
        "ROTOR_TESTNET_SECOND_MARKET",
        "two-wallet maker/taker E2E",
    ).upper().replace("_", "/")
    maker_side = _value("ROTOR_TESTNET_MAKER_SIDE", "bid").lower()
    maker_qty = _required_decimal("ROTOR_TESTNET_QTY_BASE", "two-wallet maker/taker E2E")
    maker_price = _required_decimal("ROTOR_TESTNET_PRICE", "two-wallet maker/taker E2E")
    sibling_price = _optional_decimal(
        "ROTOR_TESTNET_SECOND_PRICE",
        maker_price,
        "two-wallet maker/taker E2E",
    )
    taker_side = (_value("ROTOR_TESTNET_TAKER_SIDE") or _opposite_side(maker_side)).lower()
    taker_price = _optional_decimal(
        "ROTOR_TESTNET_TAKER_PRICE",
        maker_price,
        "two-wallet maker/taker E2E",
    )
    taker_qty = _optional_decimal(
        "ROTOR_TESTNET_TAKER_QTY_BASE",
        maker_qty,
        "two-wallet maker/taker E2E",
    )
    ttl_seconds = int(_value("ROTOR_TESTNET_ORDER_TTL", "90"))
    wait_seconds = float(_value("ROTOR_TESTNET_FILL_WAIT_SECONDS", "20"))

    if target_market not in maker.markets_by_symbol:
        pytest.skip(f"{target_market} is not available on configured Sera testnet")
    if sibling_market not in maker.markets_by_symbol:
        pytest.skip(f"{sibling_market} is not available on configured Sera testnet")
    if maker_side not in {"bid", "ask"} or taker_side not in {"bid", "ask"}:
        pytest.skip("ROTOR_TESTNET_MAKER_SIDE/TAKER_SIDE must be bid or ask")

    maker_expiration = maker.expiration_from_server_time(ttl_seconds)
    taker_expiration = taker.expiration_from_server_time(ttl_seconds)
    maker_result: VLBatchResult | None = None
    taker_record: OrderRecord | None = None
    cleanup_errors: list[str] = []

    try:
        maker_result = maker.place_vl_batch([
            VLLeg(
                market=target_market,
                side=maker_side,
                qty_base=maker_qty,
                price=maker_price,
                expiration=maker_expiration,
            ),
            VLLeg(
                market=sibling_market,
                side=maker_side,
                qty_base=maker_qty,
                price=sibling_price,
                expiration=maker_expiration,
            ),
        ])
        assert maker_result.records
        assert maker_result.vl_batch_id

        taker_record = taker.place_order(
            target_market,
            taker_side,
            taker_qty,
            taker_price,
            taker_expiration,
        )
        assert taker_record.order_id

        fills = _poll_for_fills(
            maker,
            [record.order_id for record in maker_result.records],
            wait_seconds=wait_seconds,
        )
        if not fills:
            fills = _poll_for_fills(
                taker,
                [taker_record.order_id],
                wait_seconds=wait_seconds,
            )
        assert fills, "maker/taker flow did not report fills within wait window"
    finally:
        if maker_result is not None:
            _best_effort_cleanup(
                lambda: maker.cancel_vl_batch(maker_result.vl_batch_id),
                cleanup_errors,
                f"maker VL batch {maker_result.vl_batch_id}",
            )
        if taker_record is not None:
            _best_effort_cleanup(
                lambda: taker.cancel_order(taker_record.order_id, taker_record.uuid_int),
                cleanup_errors,
                f"taker order {taker_record.order_id}",
            )
    if cleanup_errors:
        warnings.warn(
            "testnet cleanup problems: " + "; ".join(cleanup_errors),
            RuntimeWarning,
            stacklevel=2,
        )
