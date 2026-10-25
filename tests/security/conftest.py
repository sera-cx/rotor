"""Pytest fixtures for the rotor.security layer."""

from __future__ import annotations

import pytest

from rotor import config as config_module
from rotor.security import key as wallet_key


@pytest.fixture(autouse=True)
def _locked_wallet(monkeypatch: pytest.MonkeyPatch):
    """Start each test with no key held and no SERA_PRIVATE_KEY, and drop any
    key a test unlocks when it ends, so state doesn't leak between tests.
    Tests that want a key set it via their own monkeypatch after this runs.
    """
    monkeypatch.delenv("SERA_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", True)
    monkeypatch.setattr(wallet_key, "_account", None)


@pytest.fixture
def known_keypair() -> tuple[str, str]:
    """A deterministic test private key + its derived address.

    The key is 64 hex bytes of 0xaa; the address is computed at runtime so
    the test doesn't depend on a hardcoded checksum.
    """
    from eth_account import Account
    pk = "0x" + ("aa" * 32)
    addr = Account.from_key(pk).address
    return pk, addr
