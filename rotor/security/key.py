"""Signing-key loading and EIP-712 signing for Sera orders.

The maker key is read from the ``SERA_PRIVATE_KEY`` config value (environment,
root ``.env``, or optional ``rotor/secret.py``). This suits autonomous/headless
operation on a server; protect that value like any other secret.
"""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

# Expected length of a 0x-prefixed 32-byte private key string.
PRIVATE_KEY_LEN = 66

# Order of the secp256k1 curve. A valid private key is a scalar in [1, n).
# eth-account rejects keys >= n but still accepts the zero scalar, which derives
# a known, attacker-shared address — reject it explicitly.
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# Module-level state keeps the unlocked key process-local and avoids passing
# private-key material through every signing call.
_private_key: str | None = None
_address: str | None = None


class WalletNotInitialized(RuntimeError):
    """No SERA_PRIVATE_KEY is configured."""


class WalletLocked(RuntimeError):
    """A signing operation was requested before unlock()."""


class InvalidPrivateKey(ValueError):
    """The configured SERA_PRIVATE_KEY could not be loaded."""


def unlock() -> None:
    """Load the maker signing key from ``SERA_PRIVATE_KEY``."""
    global _private_key, _address

    # Unlock is idempotent so repeated CLI/runtime checks do not reload or
    # re-derive the same key.
    if _private_key is not None:
        return

    # Import lazily to avoid a config import cycle at module load time.
    from rotor.config import get_config

    # Strip whitespace because secrets often come from shell/env files.
    env_key = str(get_config("SERA_PRIVATE_KEY", "") or "").strip()
    if not env_key:
        raise WalletNotInitialized(
            "no SERA_PRIVATE_KEY configured. Set it in the environment, root "
            ".env, or optional rotor/secret.py for the maker wallet."
        )
    # Validate shape and derive the checksummed account address once.
    _private_key, _address = _private_key_and_address(
        env_key,
        error_cls=InvalidPrivateKey,
        label="SERA_PRIVATE_KEY",
    )


def is_unlocked() -> bool:
    """Return whether a private key is currently held in process memory."""
    # Auditors should note this reports in-memory state only; it does not check
    # whether configuration exists.
    return _private_key is not None


def wallet_address() -> str:
    """Return the unlocked wallet address or raise if the key is locked."""
    # Callers must unlock first so code cannot accidentally assume a signer.
    if _address is None:
        raise WalletLocked("wallet not unlocked; call `unlock()` first")
    return _address


def sign_typed_data(domain: dict, types: dict, message: dict) -> str:
    """Sign an EIP-712 typed-data message with the unlocked wallet key."""
    # Never sign with a missing key; live order placement depends on this guard.
    if _private_key is None:
        raise WalletLocked("wallet not unlocked; call `unlock()` first")
    # eth-account builds the signable digest from the domain, type schema, and
    # message fields according to EIP-712.
    signable = encode_typed_data(
        domain_data=domain,
        message_types=types,
        message_data=message,
    )
    # Account.from_key derives the account object from the raw private key for
    # this one signature.
    signed = Account.from_key(_private_key).sign_message(signable)
    signature = signed.signature.hex()
    # Normalize to the common 0x-prefixed hex string expected by APIs.
    return signature if signature.startswith("0x") else "0x" + signature


def lock() -> None:
    """Forget the private key held by this process."""
    global _private_key, _address

    # Clearing both values prevents later code from signing or reporting an
    # address after an explicit lock.
    _private_key = None
    _address = None


def _private_key_and_address(
    private_key: str,
    *,
    error_cls: type[Exception] = ValueError,
    label: str = "private key",
) -> tuple[str, str]:
    """Validate a raw private key and derive its checksum address."""
    try:
        # Enforce the expected string shape before handing it to eth-account.
        _validate_private_key(private_key)
        account = Account.from_key(private_key)
    except Exception:
        # Re-raise as the caller-selected public error type, dropping the
        # original: its message can quote the key (int()'s "invalid literal"
        # does), and a printed traceback would show it.
        raise error_cls(f"{label} is not a valid private key") from None
    # Store the original private key string and the checksum address derived by
    # eth-account.
    return private_key, account.address


def _validate_private_key(private_key: str) -> None:
    """Require the 0x-prefixed 32-byte private-key string shape and curve range."""
    # This catches common copy/paste/config mistakes before signing is attempted.
    if not private_key.startswith("0x") or len(private_key) != PRIVATE_KEY_LEN:
        raise ValueError("private key must be 0x-prefixed 64-hex-char string")
    # Reject the zero scalar and any out-of-range value: eth-account would accept
    # the zero key and derive a deterministic, non-controllable address.
    try:
        scalar = int(private_key, 16)
    except ValueError:
        # int()'s own message quotes the whole value; never chain it.
        raise ValueError("private key must be 0x-prefixed 64-hex-char string") from None
    if not (1 <= scalar < SECP256K1_N):
        raise ValueError("private key is not a valid secp256k1 scalar in [1, n)")
