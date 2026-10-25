"""EIP-712 types, uuid_int bit packing and the Order struct for Sera orders.

Pure helpers: no network I/O, no key material. The structs must match Sera's
on-chain `ORDER_TYPEHASH` and `uuid_int` the composite encoding the server
enforces. The signing domain comes from `GET /config` (see client.py).
"""

from __future__ import annotations

import decimal
import uuid as _uuid
from decimal import ROUND_FLOOR, Decimal

ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "Order": [
        {"name": "user",                 "type": "address"},
        # Unix seconds.
        {"name": "expiration",           "type": "uint48"},
        {"name": "feeBps",               "type": "uint48"},
        {"name": "recipient",            "type": "address"},
        # The token the maker spends, and the token it receives.
        {"name": "fromToken",            "type": "address"},
        {"name": "toToken",              "type": "address"},
        # Raw token-decimal amounts of fromToken and toToken.
        {"name": "fromAmount",           "type": "uint256"},
        {"name": "toAmount",             "type": "uint256"},
        {"name": "initialDepositAmount", "type": "uint256"},
        # The composite from `compose_uuid`.
        {"name": "uuid",                 "type": "uint256"},
    ],
}

CANCEL_ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "CancelOrder": [
        {"name": "owner",   "type": "address"},
        # The packed uuid_int, not the order's UUID string.
        {"name": "orderId", "type": "uint256"},
    ],
}

CANCEL_VL_BATCH_TYPES: dict[str, list[dict[str, str]]] = {
    "CancelVLBatch": [
        {"name": "owner",     "type": "address"},
        # The batch id: leg 0's order id.
        {"name": "vlBatchId", "type": "string"},
    ],
}

# uuid_int layout, for standalone orders and VL batch legs:
#
#   [255:252] executor id
#   [251:124] order UUID bits
#   [123:12]  group id (standalone: top 112 bits of order UUID;
#             VL batch: top 112 bits of leg-0 UUID)
#   [11:0]    leg id (standalone: zero; VL batch: array index)
EXECUTOR_MAX: int = 0xF       # 4-bit field
LEG_MAX: int = 0xFFF          # 12-bit field
EXECUTOR_SHIFT: int = 252
ORDER_SHIFT: int = 124
GROUP_SHIFT: int = 12
GROUP_UUID_DROP_BITS: int = 16  # top-112 bits of the 128-bit UUID


def new_order_id() -> str:
    """Return a fresh UUID4 string in canonical hyphenated form."""
    return str(_uuid.uuid4())


def compose_uuid(
    order_id: str,
    executor_id: int = 0,
    *,
    leg_id: int = 0,
    group_uuid: str | None = None,
) -> int:
    """Pack a UUID4 + executor into Sera's composite uint256.

    `executor_id` comes from `/health` at boot. `group_uuid` is leg 0's UUID
    for VL legs; standalone orders omit it and `leg_id`.
    """
    if not (0 <= executor_id <= EXECUTOR_MAX):
        raise ValueError(f"executor_id out of range: {executor_id}")
    if not (0 <= leg_id <= LEG_MAX):
        raise ValueError(f"leg_id out of range: {leg_id}")
    oid = int(_uuid.UUID(order_id))
    # Standalone orders use their own UUID as the group source; VL siblings
    # share the group UUID so the server can relate the batch legs.
    group_source = int(_uuid.UUID(group_uuid or order_id))
    # Top 112 bits of the 128-bit UUID — the low 16 bits are dropped so the
    # result fits the [123:12] slot.
    gid = group_source >> GROUP_UUID_DROP_BITS
    return (
        (executor_id << EXECUTOR_SHIFT)
        | (oid << ORDER_SHIFT)
        | (gid << GROUP_SHIFT)
        | leg_id
    )


# Canonical Order struct construction.
#
# Rotor signs only orders it builds itself, on every network, never a struct a
# server hands it. The server rebuilds the struct from the submitted order and
# recovers the signer, so everything below must mirror the server's own struct
# derivation byte for byte; any divergence gets the order rejected. Golden
# vectors captured from testnet's `POST /orders/preview` pin that: when
# changing this code, re-run `tests/mm/test_order_struct.py`.

# The server derives every amount in the signed struct under a 50-digit
# Decimal context. Python's default is 28: leaving it unset silently changes
# `amount * price` for high-precision inputs, which changes the floored raw
# amount, which changes the signature. This constant is load-bearing.
SERVER_DECIMAL_PREC: int = 50

# `recipient` is always the zero address for maker orders.
ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"


def raw_amount(amount: Decimal, decimals: int) -> int:
    """Convert a natural token amount to floored raw integer units.

    Mirrors `app/signature.py::_raw_amount`: multiply by 10**decimals and
    truncate toward zero (ROUND_FLOOR), under the server's Decimal precision.
    """
    with decimal.localcontext() as ctx:
        ctx.prec = SERVER_DECIMAL_PREC
        try:
            truncated = (amount * Decimal(10 ** decimals)).quantize(
                Decimal(1), rounding=ROUND_FLOOR
            )
        except decimal.InvalidOperation as exc:
            # A value needing more digits than the context allows cannot be
            # quantized. That precision is the only upper bound needed: 10**50
            # is far below the uint256 ceiling (~1.16e77). The server hits the
            # same wall, so it would fail the request anyway — fail here with a
            # clear message instead of signing or sending anything.
            raise ValueError(
                f"amount too large to represent at {SERVER_DECIMAL_PREC} "
                f"significant digits: {amount}e{decimals}"
            ) from exc
    result = int(truncated)
    if result < 0:
        raise ValueError(f"amount must be non-negative, got {result}")
    return result


def build_order_struct(
    *,
    owner_address: str,
    side: str,
    amount: Decimal,
    price: Decimal,
    base_address: str,
    quote_address: str,
    base_decimals: int,
    quote_decimals: int,
    uuid_int: int,
    expiration: int = 0,
) -> dict:
    """Build the canonical EIP-712 Order struct without calling the server.

    Mirrors `app/signature.py::build_order_struct`. Token decimals and
    addresses come from `GET /markets`, which the server populates from the
    same token registry its own signature verifier reads.
    """
    with decimal.localcontext() as ctx:
        # The server stringifies both inputs before converting to Decimal, so
        # do the same to avoid inheriting binary-float artifacts.
        ctx.prec = SERVER_DECIMAL_PREC
        amt = Decimal(str(amount))
        prc = Decimal(str(price))
        base_amount_raw = raw_amount(amt, base_decimals)
        # Quote leg is floored AFTER multiplying, matching the server exactly.
        quote_amount_raw = raw_amount(amt * prc, quote_decimals)

    # The server looks tokens up by lowercased address and stores them that
    # way, so the struct always carries lowercase token addresses.
    base_token = base_address.strip().lower()
    quote_token = quote_address.strip().lower()

    if side == "bid":
        from_token, to_token = quote_token, base_token
        from_amount_raw, to_amount_raw = quote_amount_raw, base_amount_raw
    elif side == "ask":
        from_token, to_token = base_token, quote_token
        from_amount_raw, to_amount_raw = base_amount_raw, quote_amount_raw
    else:
        raise ValueError(f"Invalid side: {side}")

    # Field order is irrelevant to the hash (the type schema fixes it) but is
    # kept identical to the server's for diff-friendly comparison.
    return {
        "user": owner_address,
        "expiration": int(expiration),
        "feeBps": 0,
        "recipient": ZERO_ADDRESS,
        "fromToken": from_token,
        "toToken": to_token,
        "fromAmount": from_amount_raw,
        "toAmount": to_amount_raw,
        "initialDepositAmount": 0,
        "uuid": uuid_int,
    }
