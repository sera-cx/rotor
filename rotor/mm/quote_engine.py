"""Shared quoting helpers for Sera market-making runners."""

from __future__ import annotations

import datetime as dt
import logging
import signal
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from rotor.config import get_config
from rotor.mm.exchange.client import SeraClient
from rotor.security import key as wallet_key

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


@dataclass
class LiveOrder:
    """Small local handle for one resting Sera order."""

    side: str
    order_id: str
    # Packed id required for cancel signatures.
    uuid_int: int
    # Local placement/reattach time used for cooldown guards.
    placed_at: float
    market: str = ""
    # Original base-token size when locally known; used to prune full fills.
    qty_base: Decimal | None = None


def build_client(*, dry_run: bool, log: logging.Logger) -> SeraClient:
    """Build the Sera client using Rotor's configured credentials."""
    # Dry-run never unlocks the wallet (the CLI skips it), so it needs no key.
    if dry_run:
        owner = ZERO_ADDRESS

        def signer(_domain, _types, _message):
            # A dry-run must never sign; this fails loudly if a code path tries.
            raise RuntimeError("dry-run signer should never be called")
    else:
        owner = wallet_key.wallet_address()
        signer = wallet_key.sign_typed_data
    # Credentials and base URL resolve from the environment or `.env`.
    return SeraClient(
        owner_address=owner,
        signer=signer,
        api_key=get_config("SERA_API_KEY", ""),
        api_secret=get_config("SERA_API_SECRET", ""),
        base_url=get_config(
            "SERA_BASE_URL", "https://api.testnet.sera.cx/api/v1"),
        log=log,
    )


def all_requote_safe(orders: list[LiveOrder], min_requote_seconds: float) -> bool:
    """Return true once every tracked order is past the local cooldown guard."""
    # Sera cancellation has cooldown behavior; local guard avoids predictable 429s.
    now = time.time()
    return all(now - order.placed_at >= min_requote_seconds for order in orders)


def sleep_remaining(started: float, poll_seconds: float, stop: StopFlag | None = None) -> None:
    """Sleep for the remainder of a poll interval, interruptibly when possible."""
    # Clamp at zero so slow ticks do not request a negative sleep.
    remaining = max(0.0, poll_seconds - (time.time() - started))
    # An event-backed stop flag lets SIGINT/SIGTERM wake the loop immediately
    # instead of waiting out the full poll interval.
    if stop is not None:
        stop.wait(remaining)
    else:
        time.sleep(remaining)


def parse_ts(value) -> float | None:
    """Parse common API timestamp values into POSIX seconds."""
    # Missing/empty timestamps let callers fall back to current time.
    if value is None:
        return None
    # Numeric timestamps are already seconds.
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Convert trailing-Z UTC form to Python's accepted +00:00 form.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        # Accept ISO 8601 values from API records.
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    # Treat naive timestamps as UTC to keep cooldown math deterministic.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.timestamp()


def fmt(value: Decimal) -> str:
    """Format a Decimal compactly for logs."""
    # normalize() removes exponent noise; "f" keeps fixed-point output.
    return format(value.normalize(), "f")


class StopFlag:
    """Signal-backed stop flag for long-running MM loops.

    Backed by a ``threading.Event`` so a handler firing during a sleep wakes the
    loop immediately (via ``wait``) instead of the process waiting out a full
    poll interval or a multi-minute boot-cooldown sleep before it can shut down.
    """

    def __init__(self) -> None:
        """Initialize as not-yet-stopped."""
        self._event = threading.Event()
        # A second signal: stop now, skipping any wait in shutdown cleanup.
        self._forced = threading.Event()

    @property
    def requested(self) -> bool:
        """Return whether a stop has been requested."""
        return self._event.is_set()

    def request(self) -> None:
        """Request a stop programmatically (e.g. from tests)."""
        self._event.set()

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds, returning early if a stop is set."""
        return self._event.wait(max(0.0, timeout))

    @property
    def forced(self) -> bool:
        """Return whether a second stop signal asked to skip cleanup waits."""
        return self._forced.is_set()

    def wait_forced(self, timeout: float) -> bool:
        """Block up to `timeout` seconds, returning early on a second stop signal."""
        return self._forced.wait(max(0.0, timeout))

    def install(self) -> None:
        """Install SIGINT/SIGTERM handlers that flip the stop flag."""
        def handler(_signum, _frame):
            # Signal handlers should do minimal work; setting the event both
            # flips `requested` and wakes any in-progress `wait`. A second
            # signal also cuts short a shutdown wait.
            if self._event.is_set():
                self._forced.set()
            self._event.set()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
