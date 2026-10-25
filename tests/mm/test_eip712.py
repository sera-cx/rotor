"""Tests for rotor.mm.exchange.eip712 — uuid_int packing + struct shape."""

from __future__ import annotations

import uuid

import pytest

from rotor.mm.exchange import eip712


# Bit-layout slices of a packed uuid_int.
def _executor(packed: int) -> int: return (packed >> 252) & 0xF
def _order_id_bits(packed: int) -> int: return (packed >> 124) & ((1 << 128) - 1)
def _group_id(packed: int) -> int: return (packed >> 12) & ((1 << 112) - 1)
def _leg_id(packed: int) -> int: return packed & 0xFFF


def test_compose_uuid_standalone_layout():
    """Standalone order: leg=0, group=order_id >> 16, executor in top 4 bits."""
    oid = "00000000-0000-4000-8000-000000000001"
    packed = eip712.compose_uuid(oid, executor_id=0)

    raw = int(uuid.UUID(oid))
    assert _executor(packed) == 0
    assert _leg_id(packed) == 0
    assert _order_id_bits(packed) == raw
    assert _group_id(packed) == raw >> 16


def test_compose_uuid_vl_layout_uses_leg0_group_and_leg_id():
    leg0 = "00000000-0000-4000-8000-00000000abcd"
    leg1 = "11111111-1111-4111-8111-111111111111"

    packed0 = eip712.compose_uuid(leg0, executor_id=3, leg_id=0, group_uuid=leg0)
    packed1 = eip712.compose_uuid(leg1, executor_id=3, leg_id=1, group_uuid=leg0)

    assert _executor(packed0) == 3
    assert _executor(packed1) == 3
    assert _order_id_bits(packed0) == int(uuid.UUID(leg0))
    assert _order_id_bits(packed1) == int(uuid.UUID(leg1))
    assert _group_id(packed0) == int(uuid.UUID(leg0)) >> 16
    assert _group_id(packed1) == int(uuid.UUID(leg0)) >> 16
    assert _leg_id(packed0) == 0
    assert _leg_id(packed1) == 1


def test_compose_uuid_executor_id_lands_in_top_4_bits():
    oid = "00000000-0000-4000-8000-000000000001"
    for ex in (0, 1, 7, 0xF):
        packed = eip712.compose_uuid(oid, executor_id=ex)
        assert (packed >> 252) & 0xF == ex


@pytest.mark.parametrize(("order_id", "kwargs"), [
    pytest.param("00000000-0000-4000-8000-000000000001", {"executor_id": 16}, id="executor-16"),
    pytest.param("00000000-0000-4000-8000-000000000001", {"executor_id": -1}, id="executor-neg"),
    pytest.param("00000000-0000-4000-8000-000000000001", {"leg_id": 4096}, id="leg-4096"),
    pytest.param("00000000-0000-4000-8000-000000000001", {"leg_id": -1}, id="leg-neg"),
    pytest.param("not-a-uuid", {}, id="not-a-uuid"),
])
def test_compose_uuid_rejects_out_of_range_fields_and_bad_uuids(order_id, kwargs):
    with pytest.raises(ValueError):
        eip712.compose_uuid(order_id, **kwargs)


def test_eip712_type_definitions_match_sera_spec():
    """ORDER_TYPES must match SeraLib.ORDER_TYPEHASH ordering and types."""
    expected = [
        ("user",                 "address"),
        ("expiration",           "uint48"),
        ("feeBps",               "uint48"),
        ("recipient",            "address"),
        ("fromToken",            "address"),
        ("toToken",              "address"),
        ("fromAmount",           "uint256"),
        ("toAmount",             "uint256"),
        ("initialDepositAmount", "uint256"),
        ("uuid",                 "uint256"),
    ]
    fields = [(f["name"], f["type"]) for f in eip712.ORDER_TYPES["Order"]]
    assert fields == expected


def test_cancel_types_carry_only_owner_and_id():
    assert eip712.CANCEL_ORDER_TYPES["CancelOrder"] == [
        {"name": "owner",   "type": "address"},
        {"name": "orderId", "type": "uint256"},
    ]


def test_cancel_vl_batch_type_uses_string_batch_id():
    assert eip712.CANCEL_VL_BATCH_TYPES["CancelVLBatch"] == [
        {"name": "owner",     "type": "address"},
        {"name": "vlBatchId", "type": "string"},
    ]
