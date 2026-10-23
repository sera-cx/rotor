"""Tests for the Sera REST client and its order/VL flows."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from rotor.mm.exchange import eip712
from rotor.mm.exchange import errors as errors_mod
from rotor.mm.exchange.client import SeraClient, VLLeg
from rotor.mm.exchange.known_tokens import KNOWN_TOKENS

USDC = {
    "address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "decimals": 6,
    "symbol": "USDC",
}
MYRC = {
    "address": "0x3ed03e95dd894235090b3d4a49e0c3239edce59e",
    "decimals": 18,
    "symbol": "MYRC",
}
OWNER = "0xc0ffee0000000000000000000000000000000001"
SIG_FIXED = "0x" + "11" * 65


def _build_mock_routes(extra: dict[str, Any] | None = None) -> dict:
    base = {
        "GET /config": {
            "chain_id": 11155111,
            "sera_address": "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e",
            "limits": {"vl_batch": {"min": 2, "max": 50}},
            "eip712_domain": {
                "name": "Sera",
                "version": "1",
                "chainId": 11155111,
                "verifyingContract": "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e",
            },
        },
        "GET /health": {
            "status": "healthy",
            "executor_id": 0,
            "relayer_executor_id": 0,
            "signature_ready": True,
        },
        "GET /markets": {
            "markets": [{
                "symbol": "MYRC/USDC",
                "base_symbol": "MYRC",
                "quote_symbol": "USDC",
                "base_address": MYRC["address"],
                "quote_address": USDC["address"],
                "base_decimals": 18,
                "quote_decimals": 6,
                "tick_precision": 6,
                "quantity_precision": 2,
                "price_step": "0.000001",
                "amount_step": "0.01",
            }],
        },
    }
    if extra:
        base.update(extra)
    return base


def _stub_signer(domain, types, message):
    return SIG_FIXED


def _make_client(
    routes: dict,
    captured: list[dict] | None = None,
    *,
    signer=_stub_signer,
) -> SeraClient:
    captured = captured if captured is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.url.params),
            "body": json.loads(request.content) if request.content else None,
            "headers": dict(request.headers),
        })
        key = f"{request.method} {request.url.path.replace('/api/v1', '')}"
        body = routes.get(key)
        if isinstance(body, tuple):
            status, payload = body
            return httpx.Response(status, json=payload)
        if body is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json=body)

    c = SeraClient(
        owner_address=OWNER,
        signer=signer,
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://api.sera.cx/api/v1",
    )
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    # Pacing and retry waits are tested separately; don't sleep in tests.
    c._sleep = lambda _seconds: None
    return c


@pytest.mark.parametrize(("key", "secret"), [
    ("key123\n", "s3cr3t-secret"),
    (" key123", "s3cr3t-secret\r\n"),
    ("key123", "s3cr3t-secret\t"),
])
def test_api_credentials_are_trimmed_and_sent_clean(key, secret):
    headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers["Authorization"])
        return httpx.Response(200, json={"orders": []})

    client = SeraClient(owner_address=OWNER, signer=_stub_signer, api_key=key,
                        api_secret=secret, base_url="https://api.sera.cx/api/v1")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    client.get_open_orders()

    assert headers == ["Bearer key123:s3cr3t-secret"]


@pytest.mark.parametrize(("key", "secret", "name"), [
    ("key 123", "s3cr3t-secret", "SERA_API_KEY"),
    ("key123", "s3cr3t\nsecret", "SERA_API_SECRET"),
    ("key123", "s3cr3t\x00secret", "SERA_API_SECRET"),
])
def test_api_credentials_with_inner_whitespace_are_refused_without_echoing_them(
    key, secret, name,
):
    with pytest.raises(ValueError) as raised:
        SeraClient(owner_address=OWNER, signer=_stub_signer, api_key=key,
                   api_secret=secret, base_url="https://api.sera.cx/api/v1")

    message = str(raised.value)
    assert name in message
    assert "s3cr3t" not in message and "key 123" not in message


def test_bootstrap_populates_market_cache():
    captured: list[dict] = []
    client = _make_client(_build_mock_routes(), captured)
    client.bootstrap()

    assert client.eip712_domain["chainId"] == 11155111
    assert client.executor_id == 0
    assert client.vl_batch_min == 2
    assert client.vl_batch_max == 50
    assert set(client.markets_by_symbol) == {"MYRC/USDC"}
    assert client.markets_by_symbol["MYRC/USDC"].price_step == Decimal("0.000001")
    assert [(r["method"], r["path"].replace("/api/v1", "")) for r in captured] == [
        ("GET", "/config"),
        ("GET", "/health"),
        ("GET", "/markets"),
    ]


def test_place_order_4xx_raises_typed_sera_error():
    routes = _build_mock_routes({
        "POST /orders": (400, {
            "detail": {"detail": "self-trade prevented", "error_code": "STP_BLOCKED"},
        }),
    })
    client = _make_client(routes)
    client.bootstrap()
    with pytest.raises(errors_mod.SeraError) as ei:
        client.place_order("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)
    assert ei.value.error_code == "STP_BLOCKED"
    assert ei.value.status_code == 400


def test_cancel_order_429_returns_false():
    routes = _build_mock_routes({
        "POST /orders/cancel": (429, {
            "detail": {"detail": "cooldown", "error_code": "CANCEL_COOLDOWN"},
        }),
    })
    client = _make_client(routes)
    client.bootstrap()
    assert client.cancel_order("some-id", 42) is False


def test_cancel_order_429_rate_limit_propagates():
    routes = _build_mock_routes({
        "POST /orders/cancel": (429, {"detail": "Rate limit exceeded"}),
    })
    client = _make_client(routes)
    client.bootstrap()

    with pytest.raises(errors_mod.SeraRateLimit):
        client.cancel_order("some-id", 42)


def test_cancel_order_200_returns_true():
    signed: list[dict] = []

    def signer(domain, types, message):
        signed.append({"types": types, "message": message})
        return SIG_FIXED

    routes = _build_mock_routes({"POST /orders/cancel": {"status": "ok"}})
    client = _make_client(routes, signer=signer)
    client.bootstrap()

    assert client.cancel_order("some-id", 42) is True
    assert signed == [{
        "types": eip712.CANCEL_ORDER_TYPES,
        "message": {"owner": OWNER, "orderId": 42},
    }]


def test_place_vl_batch_tracks_batch_on_mismatched_order_id_count(caplog):
    routes = _build_mock_routes({
        "POST /orders/vl/batch": (201, {"order_ids": ["leg-0"]}),
    })
    captured: list[dict] = []
    client = _make_client(routes, captured)
    client.bootstrap()

    # The batch is live on Sera; a short order_ids list must NOT orphan it.
    # We keep a cancelable vl_batch_id and the correlated prefix of records,
    # logging the mismatch loudly instead of raising and dropping tracking.
    import logging

    with caplog.at_level(logging.ERROR):
        result = client.place_vl_batch([
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
        ])

    # The batch is named by the id rotor signed for leg 0.
    leg0 = captured[-1]["body"]["orders"][0]["order_id"]
    assert result.vl_batch_id == leg0
    assert result.order_ids == ["leg-0"]
    assert [r.order_id for r in result.records] == ["leg-0"]
    assert f"tracking batch {leg0} for cancellation" in caplog.text


def test_place_vl_batch_reads_a_reply_with_null_fields():
    routes = _build_mock_routes({
        "POST /orders/vl/batch": (201, {
            "order_ids": None,
            "amendments": [{"order_id": "x", "original_amount": None, "actual_amount": None}],
        }),
    })
    captured: list[dict] = []
    client = _make_client(routes, captured)
    client.bootstrap()

    result = client.place_vl_batch([
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
    ])

    sent = [order["order_id"] for order in captured[-1]["body"]["orders"]]
    assert result.order_ids == sent
    assert result.amendments[0].actual_amount == ""


def test_cancel_vl_batch_posts_signed_batch_cancel():
    signed: list[dict] = []

    def signer(domain, types, message):
        signed.append({"types": types, "message": message})
        return SIG_FIXED

    routes = _build_mock_routes({"POST /orders/vl/cancel": {"status": "ok"}})
    captured: list[dict] = []
    client = _make_client(routes, captured, signer=signer)
    client.bootstrap()

    assert client.cancel_vl_batch("primary-id") is True

    assert signed == [{
        "types": eip712.CANCEL_VL_BATCH_TYPES,
        "message": {"owner": OWNER, "vlBatchId": "primary-id"},
    }]
    post = [r for r in captured if r["path"].endswith("/orders/vl/cancel")][-1]
    assert post["body"] == {
        "owner_address": OWNER,
        "vl_batch_id": "primary-id",
        "signature": SIG_FIXED,
    }


def test_cancel_vl_batch_429_returns_false():
    routes = _build_mock_routes({
        "POST /orders/vl/cancel": (429, {
            "detail": {"detail": "cooldown", "error_code": "CANCEL_COOLDOWN"},
        }),
    })
    client = _make_client(routes)
    client.bootstrap()

    assert client.cancel_vl_batch("primary-id") is False


def test_cancel_vl_batch_429_rate_limit_propagates():
    routes = _build_mock_routes({
        "POST /orders/vl/cancel": (429, {"detail": "Rate limit exceeded"}),
    })
    client = _make_client(routes)
    client.bootstrap()

    with pytest.raises(errors_mod.SeraRateLimit):
        client.cancel_vl_batch("primary-id")


def test_get_open_orders_uses_owner_query_and_bearer_auth():
    routes = _build_mock_routes({
        "GET /orders": {"orders": [{"trade_id": "id-1"}]},
    })
    captured: list[dict] = []
    client = _make_client(routes, captured)
    client.bootstrap()

    rows = client.get_open_orders()

    assert rows == [{"trade_id": "id-1"}]
    get = [r for r in captured if r["path"].endswith("/orders")][-1]
    assert get["query"]["owner_address"] == OWNER
    assert get["headers"]["authorization"] == "Bearer testkey:testsecret"


def test_get_order_fills_uses_order_path_and_bearer_auth():
    routes = _build_mock_routes({
        "GET /fills/order-1": {
            "items": [{"maker_order_id": "order-1", "quantity": "5"}],
        },
    })
    captured: list[dict] = []
    client = _make_client(routes, captured)
    client.bootstrap()

    rows = client.get_order_fills("order-1")

    assert rows == [{"maker_order_id": "order-1", "quantity": "5"}]
    get = [r for r in captured if r["path"].endswith("/fills/order-1")][-1]
    assert get["query"] == {"limit": "100", "offset": "0"}
    assert get["headers"]["authorization"] == "Bearer testkey:testsecret"


def test_a_failed_refresh_keeps_the_last_good_metadata():
    routes = _build_mock_routes()
    client = _make_client(routes)
    client.bootstrap()
    # The running bot's hourly refresh reads a new batch limit, then a /markets
    # reply with no markets list. Nothing from that half-read refresh is kept.
    routes["GET /config"]["limits"] = {"vl_batch": {"min": 3, "max": 5}}
    routes["GET /markets"] = {"detail": "<html>maintenance</html>"}

    with pytest.raises(RuntimeError, match="no markets list"):
        client.bootstrap()

    assert (client.vl_batch_min, client.vl_batch_max) == (2, 50)
    assert set(client.markets_by_symbol) == {"MYRC/USDC"}
    assert client.markets_by_symbol["MYRC/USDC"].price_step == Decimal("0.000001")


def test_one_malformed_market_row_is_skipped_not_fatal(caplog):
    routes = _build_mock_routes()
    good = routes["GET /markets"]["markets"][0]
    routes["GET /markets"] = {"markets": [
        {**good, "symbol": "XSGD/USDC", "base_decimals": None},
        good,
    ]}
    client = _make_client(routes)

    client.bootstrap()

    assert set(client.markets_by_symbol) == {"MYRC/USDC"}
    assert "skipping malformed GET /markets row 'XSGD/USDC'" in caplog.text


def test_bootstrap_uses_domain_fallback_when_config_omits_eip712_domain():
    routes = _build_mock_routes({
        "GET /config": {
            "chain_id": 11155111,
            "sera_address": "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e",
            "limits": {"vl_batch": {"min": 3, "max": 5}},
        },
    })
    client = _make_client(routes)

    client.bootstrap()

    assert client.eip712_domain == {
        "name": "Sera",
        "version": "1",
        "chainId": 11155111,
        "verifyingContract": "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e",
    }
    assert client.vl_batch_min == 3
    assert client.vl_batch_max == 5


def test_bootstrap_rejects_incomplete_domain():
    routes = _build_mock_routes({
        "GET /config": {
            "chain_id": None,
            "sera_address": "",
        },
    })
    client = _make_client(routes)

    with pytest.raises(RuntimeError, match="incomplete domain"):
        client.bootstrap()


@pytest.mark.parametrize(
    ("health", "match"),
    [
        ({"status": "degraded", "executor_id": 0}, "not healthy"),
        (
            {"status": "healthy", "executor_id": 0, "signature_ready": False},
            "signature verification",
        ),
        (
            {
                "status": "healthy",
                "executor_id": 1,
                "relayer_executor_id": 2,
                "signature_ready": True,
            },
            "executor drift",
        ),
        ({"status": "healthy", "signature_ready": True}, "omitted executor_id"),
    ],
)
def test_bootstrap_rejects_unsafe_health_state(health, match):
    client = _make_client(_build_mock_routes({"GET /health": health}))

    with pytest.raises(RuntimeError, match=match):
        client.bootstrap()


def test_bootstrap_reads_vl_batch_limits_from_config():
    routes = _build_mock_routes({
        "GET /config": {
            "chain_id": 11155111,
            "sera_address": "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e",
            "limits": {"vl_batch": {"min": 4, "max": 9}},
        },
    })
    client = _make_client(routes)
    client.bootstrap()

    assert (client.vl_batch_min, client.vl_batch_max) == (4, 9)

def test_server_time_is_public_and_drives_expiration():
    routes = _build_mock_routes({"GET /system/time": {"timestamp": "1713168000"}})
    captured: list[dict] = []
    client = _make_client(routes, captured)

    assert client.get_server_time() == 1713168000
    assert client.expiration_from_server_time(600) == 1713168600
    requests = [r for r in captured if r["path"].endswith("/system/time")]
    assert [r["headers"].get("authorization") for r in requests] == [None, None]


def test_server_time_rejects_missing_timestamp():
    client = _make_client(_build_mock_routes({"GET /system/time": {}}))

    with pytest.raises(RuntimeError, match="invalid timestamp"):
        client.get_server_time()


def test_cancel_order_propagates_non_cooldown_error():
    routes = _build_mock_routes({
        "POST /orders/cancel": (400, {
            "detail": {"detail": "bad cancel", "error_code": "BAD_CANCEL"},
        }),
    })
    client = _make_client(routes)
    client.bootstrap()

    with pytest.raises(errors_mod.SeraError) as ei:
        client.cancel_order("some-id", 42)
    assert ei.value.error_code == "BAD_CANCEL"


def test_place_vl_batch_rejects_local_min_and_max_limits():
    client = _make_client(_build_mock_routes())
    client.bootstrap()
    client.vl_batch_min = 2
    client.vl_batch_max = 2

    with pytest.raises(ValueError, match="at least 2 legs"):
        client.place_vl_batch([
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
        ])

    with pytest.raises(ValueError, match="cannot exceed 2 legs"):
        client.place_vl_batch([
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.24"), 999),
            VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.23"), 999),
        ])


@pytest.mark.parametrize("reply", [
    (200, {}),                                   # empty or non-JSON body
    (200, {"detail": "<html>maintenance</html>"}),
    (200, {"data": []}),                         # a renamed key
])
def test_reads_without_seras_list_are_errors_not_empty(reply):
    client = _make_client(_build_mock_routes({
        "GET /fills/o-1": reply,
        "GET /orders": reply,
    }))

    with pytest.raises(RuntimeError, match="returned no items list"):
        client.get_order_fills("o-1")
    with pytest.raises(RuntimeError, match="returned no orders list"):
        client.get_open_orders()


def test_get_open_orders_accepts_trades_response_shape():
    routes = _build_mock_routes({
        "GET /orders": {"trades": [{"trade_id": "trade-1"}]},
    })
    client = _make_client(routes)
    client.bootstrap()

    assert client.get_open_orders() == [{"trade_id": "trade-1"}]


def _good_order(**overrides):
    order = {
        "user": OWNER,
        "expiration": "999",
        "feeBps": "0",
        "recipient": "0x0000000000000000000000000000000000000000",
        "fromToken": USDC["address"],
        "toToken": MYRC["address"],
        "fromAmount": "25000000",
        "toAmount": str(100 * 10**18),
        "initialDepositAmount": "0",
        "uuid": "42",
    }
    order.update(overrides)
    return order


def test_client_requires_https_base_url():
    with pytest.raises(ValueError, match="https"):
        SeraClient(
            owner_address=OWNER,
            signer=_stub_signer,
            api_key="k",
            api_secret="s",
            base_url="http://api.sera.cx/api/v1",
        )


def test_client_allows_http_localhost_for_dev():
    client = SeraClient(
        owner_address=OWNER,
        signer=_stub_signer,
        api_key="k",
        api_secret="s",
        base_url="http://localhost:8000/api/v1",
    )
    client.close()


def test_non_json_success_response_returns_empty_dict():
    routes = _build_mock_routes({"GET /config": (204, {})})
    client = _make_client(routes)

    assert client._request("GET", "/config", authed=False, expect_status=(204,)) == {}


def test_get_order_fills_follows_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fills/o1"):
            offset = int(request.url.params["offset"])
            if offset == 0:
                return httpx.Response(200, json={"items": [{"quantity": "1"}] * 100})
            if offset == 100:
                return httpx.Response(200, json={"items": [{"quantity": "1"}] * 30})
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"detail": "not found"})

    client = SeraClient(
        owner_address=OWNER,
        signer=_stub_signer,
        api_key="k",
        api_secret="s",
        base_url="https://api.sera.cx/api/v1",
    )
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    # 100 (full page) then 30 (short page → stop): 130 total, both pages read.
    assert len(client.get_order_fills("o1")) == 130
    client.close()


def _paged_client(handler) -> SeraClient:
    client = SeraClient(owner_address=OWNER, signer=_stub_signer, api_key="k",
                        api_secret="s", base_url="https://api.sera.cx/api/v1")
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._sleep = lambda _seconds: None
    return client


def test_order_amounts_go_on_the_wire_as_plain_decimals():
    # A config size written as 5e3 parses (and quantizes) to an exponent form.
    client = _make_client(_build_mock_routes())
    client.bootstrap()

    order = client.build_order(
        "MYRC/USDC", "bid", Decimal("5E+3").quantize(Decimal("1E+1")), Decimal("2.5E-1"), 999)

    assert (order.wire["amount"], order.wire["price"]) == ("5000", "0.25")


def test_order_amounts_go_on_the_wire_without_trailing_zeros():
    # A size taken from fills keeps their 18 decimals through the grid
    # rounding, and Sera rejects "20.000000" as INVALID_DECIMAL_FORMAT.
    client = _make_client(_build_mock_routes())
    client.bootstrap()

    order = client.build_order(
        "MYRC/USDC", "ask", Decimal("20.000000"), Decimal("1.007960"), 999)
    fine = client.build_order(
        "MYRC/USDC", "ask", Decimal("0.000001"), Decimal("1E+2"), 999)

    assert (order.wire["amount"], order.wire["price"]) == ("20", "1.00796")
    assert (fine.wire["amount"], fine.wire["price"]) == ("0.000001", "100")
    assert order.struct == client.build_order(
        "MYRC/USDC", "ask", Decimal("20"), Decimal("1.00796"), 999,
        order_id=order.order_id).struct


def test_get_order_fills_fails_rather_than_undercount_past_its_page_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"quantity": "1"}] * 100})

    with pytest.raises(RuntimeError, match="more than 200 fills"):
        _paged_client(handler).get_order_fills("o1", max_pages=2)


def test_get_open_orders_reads_every_page_up_to_total():
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        rows = [{"trade_id": f"t{offset + i}"} for i in range(2 if offset < 4 else 1)]
        return httpx.Response(200, json={"trades": rows, "total": 5})

    client = _paged_client(handler)

    assert [row["trade_id"] for row in client.get_open_orders(page_limit=2)] == [
        "t0", "t1", "t2", "t3", "t4"]
    assert offsets == [0, 2, 4]
    with pytest.raises(RuntimeError, match="more than 4 open orders"):
        client.get_open_orders(page_limit=2, max_pages=2)


def test_from_response_cooldown_classification_prefers_typed_code():
    # Typed cancel-cooldown code → cooldown.
    cooldown = errors_mod.from_response(
        429, {"detail": {"error_code": "CANCEL_COOLDOWN", "detail": "wait"}})
    assert isinstance(cooldown, errors_mod.SeraCancelCooldown)

    # A different typed code that merely mentions cooldown is NOT cancel-cooldown.
    rate = errors_mod.from_response(
        429, {"detail": {"error_code": "RATE_LIMITED", "detail": "in cooldown"}})
    assert isinstance(rate, errors_mod.SeraRateLimit)
    assert not isinstance(rate, errors_mod.SeraCancelCooldown)

    # No typed code: fall back to the message substring.
    legacy = errors_mod.from_response(429, {"detail": "please wait, cooldown active"})
    assert isinstance(legacy, errors_mod.SeraCancelCooldown)


def test_from_response_names_the_refused_field():
    # Sera's 422 body as captured from testnet.
    exc = errors_mod.from_response(422, {"detail": {
        "detail": "Invalid request", "error_code": "INVALID_DECIMAL_FORMAT",
        "field": "amount", "submitted": "20.000000", "normalized": "20"}})

    assert str(exc) == (
        "INVALID_DECIMAL_FORMAT: Invalid request (amount='20.000000') (HTTP 422)")


def test_non_json_error_response_becomes_typed_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream down")

    client = SeraClient(
        owner_address=OWNER,
        signer=_stub_signer,
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://api.sera.cx/api/v1",
    )
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(errors_mod.SeraError) as ei:
        client._request("GET", "/anything")
    assert ei.value.status_code == 500
    assert ei.value.payload == {"detail": "upstream down"}


# ---------------------------------------------------------------------------
# Pacing and retries
# ---------------------------------------------------------------------------

def _scripted_client(replies: list[tuple[int, dict, dict]]):
    """A client whose requests get `replies` in order, on a fake clock."""
    sent: list[str] = []
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(f"{request.method} {request.url.path.replace('/api/v1', '')}")
        status, payload, headers = queue.pop(0) if queue else (200, {}, {})
        return httpx.Response(status, json=payload, headers=headers)

    client = SeraClient(
        owner_address=OWNER,
        signer=_stub_signer,
        api_key="testkey",
        api_secret="testsecret",
        base_url="https://api.sera.cx/api/v1",
    )
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    clock = [100.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    client._monotonic = lambda: clock[0]
    client._sleep = sleep
    return client, sent, sleeps


RATE_LIMITED = (429, {"detail": {"error_code": "RATE_LIMITED", "detail": "slow down"}}, {"Retry-After": "2"})
SERVER_ERROR = (503, {"detail": "Service temporarily unavailable; please retry"}, {"Retry-After": "1"})


def test_api_key_requests_are_spaced_to_seras_per_wallet_limits():
    client, sent, sleeps = _scripted_client([])

    client._request("GET", "/fills/a")
    client._request("GET", "/fills/b")
    client._request("GET", "/config", authed=False)
    for _ in range(3):
        client._request("POST", "/orders/cancel", json_body={})

    # Reads 10/s, cancels 2/s; the public /config call is not paced.
    assert sleeps == pytest.approx([0.1, 0.5, 0.5])
    assert len(sent) == 6


def test_a_rate_limited_request_is_retried_after_retry_after():
    client, sent, sleeps = _scripted_client([RATE_LIMITED, (200, {"items": []}, {})])

    assert client._request("POST", "/orders/vl/batch", json_body={}) == {"items": []}
    assert sent == ["POST /orders/vl/batch", "POST /orders/vl/batch"]
    assert 2.0 in sleeps


def test_a_cancel_cooldown_is_not_retried():
    cooldown = (429, {"detail": {"error_code": errors_mod.CANCEL_COOLDOWN, "detail": "wait"}}, {})
    client, sent, _ = _scripted_client([cooldown])

    with pytest.raises(errors_mod.SeraCancelCooldown):
        client._request("POST", "/orders/cancel", json_body={})
    assert len(sent) == 1


def test_a_server_error_is_retried_only_where_repeating_is_harmless():
    client, sent, _ = _scripted_client([SERVER_ERROR, (200, {"items": []}, {}), SERVER_ERROR])

    assert client._request("GET", "/fills/a") == {"items": []}
    # A VL batch may already have been placed when the error came back.
    with pytest.raises(errors_mod.SeraError) as raised:
        client._request("POST", "/orders/vl/batch", json_body={})
    assert raised.value.status_code == 503
    assert sent == ["GET /fills/a", "GET /fills/a", "POST /orders/vl/batch"]


def test_retries_give_up_after_two():
    client, sent, _ = _scripted_client([RATE_LIMITED] * 5)

    with pytest.raises(errors_mod.SeraRateLimit):
        client._request("GET", "/fills/a")
    assert len(sent) == 3


def test_vault_balances_are_totals_in_token_units():
    routes = _build_mock_routes({"GET /balances": {"owner_address": OWNER, "balances": [
        {"symbol": "USDC", "decimals": 6, "wallet_balance": "9000000",
         "vault_available": "400000000", "vault_frozen": "100500000", "vault_total": "500500000"},
        {"symbol": "myrc", "decimals": 18, "vault_total": "2500000000000000000"},
    ]}})
    captured: list[dict] = []
    client = _make_client(routes, captured)

    # Wallet (non-vault) funds cannot back orders and are not counted.
    assert client.get_vault_balances() == {"USDC": Decimal("500.5"), "MYRC": Decimal("2.5")}
    assert captured[-1]["query"] == {"owner_address": OWNER}


# ---------------------------------------------------------------------------
# Orders are built and signed locally; the signing contract is pinned
# ---------------------------------------------------------------------------

SEPOLIA_SERA = "0x83475a1bd98a8dc2dcd507a747e4dc85da241d6e"


def test_place_order_signs_a_locally_built_order_and_never_previews():
    signed: list[dict] = []

    def signer(domain, types, message):
        signed.append({"domain": domain, "types": types, "message": message})
        return SIG_FIXED

    routes = _build_mock_routes({"POST /orders": (201, {"order_id": "server-id"})})
    captured: list[dict] = []
    client = _make_client(routes, captured, signer=signer)
    client.bootstrap()

    rec = client.place_order("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)

    posts = [r for r in captured if r["method"] == "POST"]
    assert [r["path"].replace("/api/v1", "") for r in posts] == ["/orders"]
    body = posts[0]["body"]
    assert rec.order_id == "server-id"
    # Every signed field is rotor's own: Sera's Order type, no fee, no
    # recipient, no deposit, rotor's expiration and uuid, exact amounts.
    assert signed == [{
        "domain": client.eip712_domain,
        "types": eip712.ORDER_TYPES,
        "message": {
            "user": OWNER,
            "expiration": 999,
            "feeBps": 0,
            "recipient": "0x0000000000000000000000000000000000000000",
            "fromToken": USDC["address"],
            "toToken": MYRC["address"],
            "fromAmount": 25000000,
            "toAmount": 100 * 10**18,
            "initialDepositAmount": 0,
            "uuid": int(body["uuid_int"]),
        },
    }]
    assert (body["amount"], body["price"], body["signature"]) == ("100", "0.25", SIG_FIXED)
    assert posts[0]["headers"]["authorization"] == "Bearer testkey:testsecret"


def test_place_vl_batch_builds_and_signs_each_leg_locally():
    signed: list[dict] = []

    def signer(domain, types, message):
        signed.append({"types": types, "message": message})
        return SIG_FIXED

    routes = _build_mock_routes({
        "POST /orders/vl/batch": (201, {
            "order_ids": ["leg-0", "leg-1"],
            "amendments": [{
                "order_id": "leg-1",
                "original_amount": "100",
                "actual_amount": "80.00",
                "reason": "budget_clip",
            }],
            "cancelled": [],
            "fills": [{"order_id": "leg-0", "trades": [{"quantity": "1"}]}],
            "vl_group": {
                "primary_id": "leg-0",
                "max_budget": "25",
                "budget_consumed": "1",
                "spent_token": "USDC",
            },
        }),
    })
    captured: list[dict] = []
    client = _make_client(routes, captured, signer=signer)
    client.bootstrap()

    result = client.place_vl_batch([
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
    ])

    posts = [r for r in captured if r["method"] == "POST"]
    assert [r["path"].replace("/api/v1", "") for r in posts] == ["/orders/vl/batch"]
    orders = posts[0]["body"]["orders"]
    assert [o["signature"] for o in orders] == [SIG_FIXED, SIG_FIXED]
    assert all(entry["types"] == eip712.ORDER_TYPES for entry in signed)
    assert [entry["message"]["uuid"] for entry in signed] == [int(o["uuid_int"]) for o in orders]
    # Both legs share one group and carry their leg index.
    uuid0, uuid1 = (int(o["uuid_int"]) for o in orders)
    assert (uuid0 & 0xFFF, uuid1 & 0xFFF) == (0, 1)
    assert (uuid0 >> 12) & ((1 << 112) - 1) == (uuid1 >> 12) & ((1 << 112) - 1)
    assert result.vl_batch_id == orders[0]["order_id"]
    assert result.records[1].order_id == "leg-1"
    assert result.amendments[0].actual_amount == "80.00"
    assert result.vl_group is not None and result.vl_group.spent_token == "USDC"


def _config_route(chain_id: int, contract: str) -> dict:
    return {"GET /config": {
        "chain_id": chain_id,
        "sera_address": contract,
        "eip712_domain": {
            "name": "Sera", "version": "1", "chainId": chain_id, "verifyingContract": contract,
        },
    }}


@pytest.mark.parametrize("chain_id", [1, 11155111])
def test_bootstrap_refuses_another_contract_on_a_known_network(chain_id):
    client = _make_client(_build_mock_routes(_config_route(chain_id, "0x" + "ab" * 20)))

    with pytest.raises(RuntimeError, match=f"names Sera contract 0x{'ab' * 20} for chain {chain_id}"):
        client.bootstrap()
    assert client.eip712_domain is None


def test_bootstrap_accepts_the_known_contract_in_any_case():
    checksummed = "0x83475A1bD98a8DC2DCd507A747e4DC85da241D6e"
    client = _make_client(_build_mock_routes(_config_route(11155111, checksummed)))

    client.bootstrap()

    assert client.eip712_domain["verifyingContract"].lower() == SEPOLIA_SERA


def test_bootstrap_warns_on_a_network_it_does_not_know(caplog):
    client = _make_client(_build_mock_routes(_config_route(31337, "0x" + "cd" * 20)))

    client.bootstrap()

    assert "chain 31337 is not a known Sera network" in caplog.text


# ---------------------------------------------------------------------------
# Mainnet token metadata is checked against the pinned list
# ---------------------------------------------------------------------------

MAINNET_SERA = "0xB5C50C5D5f038404F85970b7f5B7259C4AC0E198"


def _mainnet_market(base: str, quote: str, **overrides) -> dict:
    tokens = KNOWN_TOKENS[1]
    row = {
        "symbol": f"{base}/{quote}",
        "base_symbol": base,
        "quote_symbol": quote,
        "base_address": tokens.get(base, ("0x" + "77" * 20, 6))[0],
        "quote_address": tokens[quote][0],
        "base_decimals": tokens.get(base, ("", 6))[1],
        "quote_decimals": tokens[quote][1],
        "tick_precision": 6,
        "quantity_precision": 2,
    }
    row.update(overrides)
    return row


def _mainnet_client(*markets: dict) -> SeraClient:
    routes = _build_mock_routes(_config_route(1, MAINNET_SERA))
    routes["GET /markets"] = {"markets": list(markets)}
    return _make_client(routes)


def test_bootstrap_accepts_genuine_mainnet_token_metadata():
    client = _mainnet_client(_mainnet_market("XSGD", "USDC"), _mainnet_market("NEWT", "USDC"))

    client.bootstrap()

    # A token Sera lists later (not pinned) is accepted as reported.
    assert set(client.markets_by_symbol) == {"XSGD/USDC", "NEWT/USDC"}


@pytest.mark.parametrize(
    ("market", "match"),
    [
        # XSGD/USDC whose base is really IDRT: bids priced for SGD would buy IDRT.
        (lambda: _mainnet_market("XSGD", "USDC", base_address=_mainnet_market("IDRT", "USDC")["base_address"]),
         "lists XSGD at 0x"),
        # The same swap behind a name rotor does not pin.
        (lambda: _mainnet_market("XSGDX", "USDC", base_address=_mainnet_market("IDRT", "USDC")["base_address"]),
         "lists IDRT's address 0x"),
        # A market symbol that does not match its own tokens.
        (lambda: _mainnet_market("IDRT", "USDC", symbol="XSGD/USDC"), "lists XSGD/USDC with tokens IDRT/USDC"),
        # Right address, wrong decimals.
        (lambda: _mainnet_market("XSGD", "USDC", base_decimals=18), "lists XSGD at .* with 18 decimals"),
    ],
)
def test_bootstrap_refuses_mainnet_token_metadata_that_disagrees_with_the_pins(market, match):
    client = _mainnet_client(market())

    with pytest.raises(RuntimeError, match=match):
        client.bootstrap()


def test_a_bad_sera_base_url_is_refused_without_echoing_it():
    pasted_key = "0x" + "ab" * 32

    with pytest.raises(ValueError, match="SERA_BASE_URL must be an https:// URL") as raised:
        SeraClient(owner_address=OWNER, signer=_stub_signer, api_key="k",
                   api_secret="s", base_url=pasted_key)

    assert pasted_key not in str(raised.value)


def test_nothing_is_signed_before_bootstrap():
    client = _make_client(_build_mock_routes())
    client.markets_by_symbol = {}

    with pytest.raises((RuntimeError, KeyError), match="bootstrap"):
        client.sign_order("MYRC/USDC", "bid", Decimal("1"), Decimal("0.25"), 999)
