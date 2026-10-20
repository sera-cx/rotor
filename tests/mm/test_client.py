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
            "sera_address": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
            "limits": {"vl_batch": {"min": 2, "max": 50}},
            "eip712_domain": {
                "name": "Sera",
                "version": "1",
                "chainId": 11155111,
                "verifyingContract": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
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


def test_preview_order_sends_unsigned_unauthed_wire():
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {"uuid": "42"},
            "eip712_types": eip712.ORDER_TYPES,
        },
    })
    captured: list[dict] = []
    client = _make_client(routes, captured)
    client.bootstrap()

    preview = client.preview_order("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)

    post = [r for r in captured if r["method"] == "POST"][-1]
    assert post["path"].endswith("/orders/preview")
    assert "signature" not in post["body"]
    assert post["headers"].get("authorization") is None
    assert preview.amount == "100.00"
    assert preview.price == "0.250000"


@pytest.mark.parametrize(
    "preview_body",
    [
        {
            "normalized_price": "0.250000",
            "eip712_order": {"uuid": "42"},
            "eip712_types": eip712.ORDER_TYPES,
        },
        {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {"uuid": "42"},
        },
        {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {},
            "eip712_types": eip712.ORDER_TYPES,
        },
    ],
)
def test_preview_order_requires_canonical_response(preview_body):
    client = _make_client(_build_mock_routes({"POST /orders/preview": preview_body}))
    client.bootstrap()

    with pytest.raises(RuntimeError, match="POST /orders/preview"):
        client.preview_order("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)


def test_place_order_previewed_signs_normalized_payload():
    signed: list[dict] = []

    def signer(domain, types, message):
        signed.append({"domain": domain, "types": types, "message": message})
        return SIG_FIXED

    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {
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
            },
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders": (201, {"order_id": "server-id"}),
    })
    captured: list[dict] = []
    client = _make_client(routes, captured, signer=signer)
    client.bootstrap()

    rec = client.place_order_previewed("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)

    assert rec.order_id == "server-id"
    assert signed == [{
        "domain": {
            "name": "Sera",
            "version": "1",
            "chainId": 11155111,
            "verifyingContract": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
        },
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
            "uuid": 42,
        },
    }]
    post = [r for r in captured if r["method"] == "POST"][-1]
    assert post["body"]["amount"] == "100.00"
    assert post["body"]["price"] == "0.250000"
    assert post["body"]["signature"] == SIG_FIXED
    assert post["headers"]["authorization"] == "Bearer testkey:testsecret"


def test_place_order_4xx_raises_typed_sera_error():
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {
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
            },
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders": (400, {
            "detail": {"detail": "self-trade prevented", "error_code": "STP_BLOCKED"},
        }),
    })
    client = _make_client(routes)
    client.bootstrap()
    with pytest.raises(errors_mod.SeraError) as ei:
        client.place_order_previewed("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)
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


def test_place_vl_batch_previews_each_leg_and_posts_signed_batch():
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {
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
            },
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders/vl/batch": (201, {
            "order_ids": ["leg-0", "leg-1"],
            "amendments": [{
                "order_id": "leg-1",
                "original_amount": "100.00",
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
    client = _make_client(routes, captured)
    client.bootstrap()

    result = client.place_vl_batch([
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
        VLLeg("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999),
    ])

    previews = [r for r in captured if r["path"].endswith("/orders/preview")]
    assert len(previews) == 2
    uuid0 = int(previews[0]["body"]["uuid_int"])
    uuid1 = int(previews[1]["body"]["uuid_int"])
    assert uuid0 & 0xFFF == 0
    assert uuid1 & 0xFFF == 1
    assert (uuid0 >> 12) & ((1 << 112) - 1) == (uuid1 >> 12) & ((1 << 112) - 1)

    post = [r for r in captured if r["path"].endswith("/orders/vl/batch")][-1]
    assert len(post["body"]["orders"]) == 2
    assert post["body"]["orders"][0]["signature"] == SIG_FIXED
    assert post["body"]["orders"][0]["amount"] == "100.00"
    assert post["headers"]["authorization"] == "Bearer testkey:testsecret"
    assert result.vl_batch_id == "leg-0"
    assert result.order_ids == ["leg-0", "leg-1"]
    assert result.records[1].order_id == "leg-1"
    assert result.amendments[0].actual_amount == "80.00"
    assert result.vl_group is not None
    assert result.vl_group.spent_token == "USDC"


def test_place_vl_batch_tracks_batch_on_mismatched_order_id_count(caplog):
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": {
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
            },
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders/vl/batch": (201, {"order_ids": ["leg-0"]}),
    })
    client = _make_client(routes)
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

    assert result.vl_batch_id == "leg-0"
    assert result.order_ids == ["leg-0"]
    assert [r.order_id for r in result.records] == ["leg-0"]
    assert "tracking batch leg-0 for cancellation" in caplog.text


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


def test_bootstrap_uses_domain_fallback_when_config_omits_eip712_domain():
    routes = _build_mock_routes({
        "GET /config": {
            "chain_id": 11155111,
            "sera_address": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
            "limits": {"vl_batch": {"min": 3, "max": 5}},
        },
    })
    client = _make_client(routes)

    client.bootstrap()

    assert client.eip712_domain == {
        "name": "Sera",
        "version": "1",
        "chainId": 11155111,
        "verifyingContract": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
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


def test_get_config_limits_refreshes_vl_limits():
    routes = _build_mock_routes({
        "GET /config": {
            "chain_id": 11155111,
            "sera_address": "0xd0fc92d8ef9be26d7288fca1d6458f675e72b83a",
            "limits": {"vl_batch": {"min": 4, "max": 9}},
        },
    })
    client = _make_client(routes)
    client.bootstrap()

    limits = client.get_config_limits()

    assert limits["vl_batch"] == {"min": 4, "max": 9}
    assert client.vl_batch_min == 4
    assert client.vl_batch_max == 9


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


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"recipient": "0x000000000000000000000000000000000000dead"}, "recipient"),
        ({"user": "0x000000000000000000000000000000000000beef"}, "user"),
        ({"fromToken": MYRC["address"]}, "fromToken"),
        ({"toToken": USDC["address"]}, "toToken"),
        ({"fromAmount": str(25000000 * 10)}, "fromAmount"),
        ({"toAmount": "1"}, "toAmount"),
    ],
)
def test_place_order_rejects_preview_that_does_not_match_intent(overrides, match):
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": _good_order(**overrides),
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders": (201, {"order_id": "server-id"}),
    })
    client = _make_client(routes)
    client.bootstrap()

    with pytest.raises(errors_mod.SeraPreviewMismatch, match=match):
        client.place_order_previewed("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)


def test_place_order_accepts_matching_preview_with_rounding_slack():
    # fromAmount off by a couple of base units (rounding) must still be accepted.
    routes = _build_mock_routes({
        "POST /orders/preview": {
            "normalized_amount": "100.00",
            "normalized_price": "0.250000",
            "eip712_order": _good_order(fromAmount="25000001"),
            "eip712_types": eip712.ORDER_TYPES,
        },
        "POST /orders": (201, {"order_id": "server-id"}),
    })
    client = _make_client(routes)
    client.bootstrap()

    rec = client.place_order_previewed("MYRC/USDC", "bid", Decimal("100"), Decimal("0.25"), 999)
    assert rec.order_id == "server-id"


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
# Preview-free placement (mainnet deployments)
#
# Mainnet deployments do not expose POST /orders/preview. Rotor
# must detect that once and then derive the canonical struct locally rather
# than failing every order.
# ---------------------------------------------------------------------------


def _routes_without_preview() -> dict:
    """Mock routes for a deployment that has no preview endpoint."""
    routes = _build_mock_routes()
    # FastAPI answers a POST to a path it only knows for other methods with
    # 405; a wholly unknown path gives 404. Cover the 405 shape here.
    routes["POST /orders/preview"] = (405, {"detail": "Method Not Allowed"})
    return routes


def test_preview_falls_back_to_local_struct_when_endpoint_absent():
    """A 405 from preview latches local struct building instead of raising."""
    captured: list[dict] = []
    client = _make_client(_routes_without_preview(), captured)
    client.bootstrap()

    preview = client.preview_order(
        "MYRC/USDC", "bid", Decimal("1000"), Decimal("1.25"), 1800000000,
    )

    # The deployment is now known to lack preview.
    assert client.preview_supported is False
    # A locally derived struct still carries everything placement needs.
    order = preview.eip712_order
    assert order["user"] == OWNER.lower()
    assert order["fromToken"] == USDC["address"].lower()   # bid spends quote
    assert order["toToken"] == MYRC["address"].lower()
    assert order["fromAmount"] == 1250 * 10**6             # 1000 * 1.25, 6dp
    assert order["toAmount"] == 1000 * 10**18              # 1000 base, 18dp
    assert preview.eip712_types == eip712.ORDER_TYPES


def test_preview_not_retried_after_endpoint_known_absent():
    """Once latched, later orders skip the doomed preview round trip."""
    captured: list[dict] = []
    client = _make_client(_routes_without_preview(), captured)
    client.bootstrap()

    client.preview_order(
        "MYRC/USDC", "bid", Decimal("1"), Decimal("1"), 1800000000)
    first = sum(1 for c in captured if c["path"].endswith("/orders/preview"))
    client.preview_order(
        "MYRC/USDC", "ask", Decimal("2"), Decimal("3"), 1800000000)
    second = sum(1 for c in captured if c["path"].endswith("/orders/preview"))

    # Exactly one probe total, not one per order.
    assert first == 1
    assert second == 1


def test_preview_error_other_than_missing_endpoint_still_raises():
    """A 422 rejection is about this order and must not be swallowed."""
    routes = _build_mock_routes()
    routes["POST /orders/preview"] = (422, {"detail": "Invalid request"})
    client = _make_client(routes)
    client.bootstrap()

    with pytest.raises(errors_mod.SeraError):
        client.preview_order(
            "MYRC/USDC", "bid", Decimal("1000"), Decimal("1.25"), 1800000000)
    # A per-order rejection says nothing about endpoint availability.
    assert client.preview_supported is not False


def test_local_struct_passes_the_preview_mismatch_guard():
    """Locally built orders satisfy the same anti-tamper check as server ones."""
    routes = _routes_without_preview()
    routes["POST /orders"] = (201, {"order_id": "server-id"})
    client = _make_client(routes)
    client.bootstrap()

    # place_order_previewed runs _assert_preview_matches on whatever struct it
    # is handed; a locally derived one must pass that guard unchanged.
    record = client.place_order_previewed(
        "MYRC/USDC", "ask", Decimal("5"), Decimal("2"), 1800000000)
    assert record.order_id


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
