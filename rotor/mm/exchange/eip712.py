"""EIP-712 type definitions + uuid_int bit packing for Sera orders.

Pure helpers: no network I/O, no key material. The order/cancel structs
should match Sera's on-chain `ORDER_TYPEHASH`; `uuid_int` should match
the composite encoding the server enforces.

Domain separator note:
    This module does NOT compute or hold the EIP-712 domain separator.
    The domain dict (chainId, verifyingContract, name, version) is
    fetched from Sera's `GET /config` by `SeraClient.bootstrap()` and
    passed to the injected signer at sign time. The signer
    (`rotor.security.key.sign_typed_data`) is the one that computes
    `keccak256("EIP712Domain(...)")` and the message digest. See the
    audit notes — if the live `/config` returns a domain whose
    `verifyingContract` differs from the on-chain ORDER_TYPEHASH
    consumer, signatures will silently mis-verify.

Signature serialization:
    By convention `eth_account` returns ``r ‖ s ‖ v`` as 65 bytes,
    serialized as ``0x``-prefixed hex (132 chars). The signer adjusts
    `v` to either ``{27, 28}`` (legacy) or ``{0, 1}`` depending on the
    underlying library; verify the consumer of this signature on the
    Sera side accepts the produced encoding (some on-chain validators
    require `v ∈ {27, 28}` while EIP-2098 packs it into the high bit
    of `s`).

References:
- sera-docs/docs/en/api-reference/market-maker-guide.md (Python reference
  for the wire payload + uuid_int packing)
- sera-docs/docs/en/api-reference/endpoints/orders.md (POST /orders contract)
"""

from __future__ import annotations

import decimal
import uuid as _uuid
from decimal import ROUND_FLOOR, Decimal

# ---------------------------------------------------------------------------
# EIP-712 message types
#
# Each dict maps the primary type name to its field schema, in the
# same shape `eth_account.messages.encode_typed_data` consumes. The
# domain separator is supplied separately from `SeraClient.eip712_domain`
# (populated by `bootstrap()` from `GET /config`).
# ---------------------------------------------------------------------------

# EIP-712 primary type for a place-order signature.
ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "Order": [
        # Wallet address that owns/signs the order.
        {"name": "user",                 "type": "address"},
        # Unix timestamp; Sera constrains this to uint48.
        {"name": "expiration",           "type": "uint48"},
        # Maker fee setting carried in the signed struct.
        {"name": "feeBps",               "type": "uint48"},
        # Settlement recipient; callers normally use the zero address.
        {"name": "recipient",            "type": "address"},
        # Token the maker sends/spends when the order fills.
        {"name": "fromToken",            "type": "address"},
        # Token the maker receives when the order fills.
        {"name": "toToken",              "type": "address"},
        # Raw token-decimal amount for `fromToken`.
        {"name": "fromAmount",           "type": "uint256"},
        # Raw token-decimal amount for `toToken`.
        {"name": "toAmount",             "type": "uint256"},
        # Optional pre-funding hint expected by Sera's order contract.
        {"name": "initialDepositAmount", "type": "uint256"},
        # Composite identifier created by `compose_uuid`.
        {"name": "uuid",                 "type": "uint256"},
    ],
}

# EIP-712 primary type for a single-order cancel signature.
CANCEL_ORDER_TYPES: dict[str, list[dict[str, str]]] = {
    "CancelOrder": [
        # Wallet address authorizing the cancellation.
        {"name": "owner",   "type": "address"},
        # Composite uuid_int/order id used by Sera for the cancel operation.
        {"name": "orderId", "type": "uint256"},
    ],
}

# EIP-712 primary type for a virtual-liquidity batch cancel signature.
CANCEL_VL_BATCH_TYPES: dict[str, list[dict[str, str]]] = {
    "CancelVLBatch": [
        # Wallet address authorizing the batch cancellation.
        {"name": "owner",     "type": "address"},
        # Sera's batch identifier, usually the primary order id.
        {"name": "vlBatchId", "type": "string"},
    ],
}

# ---------------------------------------------------------------------------
# uuid_int composite encoding
#
# Standalone order layout, and VL batch layout:
#
#   [255:252] executor id
#   [251:124] order UUID bits
#   [123:12]  group id (standalone: top 112 bits of order UUID;
#             VL batch: top 112 bits of leg-0 UUID)
#   [11:0]    leg id (standalone: zero; VL batch: array index)
# ---------------------------------------------------------------------------

# Maximum values derived from the allocated bit widths.
EXECUTOR_MAX: int = 0xF       # 4-bit field
LEG_MAX: int = 0xFFF          # 12-bit field
# Left-shift offsets for each packed uuid_int field.
EXECUTOR_SHIFT: int = 252
ORDER_SHIFT: int = 124
GROUP_SHIFT: int = 12
# Low UUID bits dropped when forming the 112-bit group field.
GROUP_UUID_DROP_BITS: int = 16  # top-112 bits of the 128-bit UUID


def new_order_id() -> str:
    """Return a fresh UUID4 string in canonical hyphenated form.

    Returns:
        A new UUID4, e.g. ``"f47ac10b-58cc-4372-a567-0e02b2c3d479"``.
    """
    # UUID4 gives a fresh random order id before Sera converts it into uuid_int.
    return str(_uuid.uuid4())


def compose_uuid(
    order_id: str,
    executor_id: int = 0,
    *,
    leg_id: int = 0,
    group_uuid: str | None = None,
) -> int:
    """Pack a UUID4 + executor into Sera's composite uint256.

    Args:
        order_id: UUID4 string for this leg.
        executor_id: 4-bit executor ID (0..15). Comes from `/health` at
            boot.
        leg_id: 12-bit VL leg index. Standalone orders use zero.
        group_uuid: UUID whose top 112 bits form the shared group id. Standalone
            orders omit this so their own UUID provides the group id.

    Returns:
        The composite uint256 as a Python ``int``.

    Raises:
        ValueError: If `executor_id` or `leg_id` is outside its field range.
    """
    # Reject values that would overflow their reserved bit fields.
    if not (0 <= executor_id <= EXECUTOR_MAX):
        raise ValueError(f"executor_id out of range: {executor_id}")
    if not (0 <= leg_id <= LEG_MAX):
        raise ValueError(f"leg_id out of range: {leg_id}")
    # Parse the per-order UUID into its 128-bit integer representation.
    oid = int(_uuid.UUID(order_id))
    # Standalone orders use their own UUID as the group source; VL siblings
    # share the group UUID so the server can relate the batch legs.
    group_source = int(_uuid.UUID(group_uuid or order_id))
    # Top 112 bits of the 128-bit UUID — the low 16 bits are dropped so the
    # result fits the [123:12] slot.
    gid = group_source >> GROUP_UUID_DROP_BITS
    # OR each shifted field into the final uint256 composite value.
    return (
        (executor_id << EXECUTOR_SHIFT)
        | (oid << ORDER_SHIFT)
        | (gid << GROUP_SHIFT)
        | leg_id
    )


# ---------------------------------------------------------------------------
# Canonical Order struct construction
#
# Rotor signs only orders it builds itself, on every network, never a struct a
# server hands it. The server rebuilds the struct from the submitted order and
# recovers the signer, so the local struct must be the one the server derives.
# Golden vectors captured from testnet's `POST /orders/preview` pin that.
#
# Everything below mirrors the server's own struct derivation byte-for-byte. Any divergence produces a struct hash the server will not
# recover the expected signer from, and the order is rejected. When changing
# this code, re-run `tests/mm/test_order_struct.py`, which proves
# the local struct equals the server's own preview output.
# ---------------------------------------------------------------------------

# The server derives every amount in the signed struct under a 50-digit
# Decimal context. Python's
# default is 28: leaving it unset silently changes `amount * price` for
# high-precision inputs, which changes the floored raw amount, which changes
# the signature. This constant is load-bearing.
SERVER_DECIMAL_PREC: int = 50

# Solidity uint256 ceiling; the server rejects raw amounts above it.
UINT256_MAX: int = (1 << 256) - 1

# `recipient` is always the zero address for maker orders.
ZERO_ADDRESS: str = "0x0000000000000000000000000000000000000000"


def raw_amount(amount: Decimal, decimals: int) -> int:
    """Convert a natural token amount to floored raw integer units.

    Mirrors `app/signature.py::_raw_amount`: multiply by 10**decimals and
    truncate toward zero (ROUND_FLOOR), under the server's Decimal precision.
    """
    with decimal.localcontext() as ctx:
        # Match the server context so the multiplication rounds identically.
        ctx.prec = SERVER_DECIMAL_PREC
        try:
            truncated = (amount * Decimal(10 ** decimals)).quantize(
                Decimal(1), rounding=ROUND_FLOOR
            )
        except decimal.InvalidOperation as exc:
            # A value needing more digits than the context allows cannot be
            # quantized. The server hits the same wall (its own uint256 guard
            # sits after this call and is unreachable for such values), so it
            # would fail the request anyway — fail here with a clear message
            # instead of signing or sending anything.
            raise ValueError(
                f"amount too large to represent at {SERVER_DECIMAL_PREC} "
                f"significant digits: {amount}e{decimals}"
            ) from exc
    result = int(truncated)
    if result < 0:
        raise ValueError(f"amount must be non-negative, got {result}")
    if result > UINT256_MAX:
        raise ValueError(f"amount exceeds uint256: {result}")
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
        # A bid spends the quote token to receive base.
        from_token, to_token = quote_token, base_token
        from_amount_raw, to_amount_raw = quote_amount_raw, base_amount_raw
    elif side == "ask":
        # An ask spends the base token to receive quote.
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
