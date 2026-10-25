"""Opt-in tests against the real Sera testnet API."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from rotor.config import get_config
from rotor.mm.exchange import eip712
from rotor.mm.exchange.client import SeraClient

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
