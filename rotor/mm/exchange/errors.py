"""Typed errors raised by `rotor.mm.exchange.client`."""

from __future__ import annotations


class SeraError(Exception):
    """Raised on any 4xx response from Sera with a typed `error_code`.

    Attributes:
        error_code: Sera's typed code (e.g. ``"PAIR_INACTIVE"``,
            ``"INVALID_DECIMAL_FORMAT"``); empty string if absent.
        detail: Human-readable detail message.
        status_code: HTTP status code (4xx).
    """

    def __init__(self, error_code: str | None, detail: str, status_code: int) -> None:
        """Initialize a typed Sera error; a missing code becomes ``""``."""
        # Normalize missing codes to an empty string so callers can compare
        # without repeatedly handling None.
        self.error_code = error_code or ""
        self.detail = detail
        self.status_code = status_code
        # Exception text includes the Sera code (when there is one) and HTTP
        # status for one-line logs and alerts.
        prefix = f"{self.error_code}: " if self.error_code else ""
        super().__init__(f"{prefix}{detail} (HTTP {status_code})")


class SeraCancelCooldown(SeraError):
    """429 on POST /orders/cancel — per-`order_id` 5-minute cooldown."""


class SeraRateLimit(SeraError):
    """429 rate-limit response that is not a cancel-cooldown response."""


class SeraUnavailable(RuntimeError):
    """Sera reports it is not ready to trade yet (a degraded GET /health)."""


def from_response(status_code: int, body: dict) -> SeraError:
    """Build a typed error from a parsed 4xx response body.

    Args:
        status_code: HTTP status code (4xx).
        body: Parsed JSON response body. May be any shape — non-dict
            bodies and missing/oddly-nested ``detail`` envelopes are
            handled defensively (code becomes "", detail becomes
            ``str(envelope)``).

    Returns:
        `SeraCancelCooldown` only for cancel-cooldown 429 responses,
        `SeraRateLimit` for other 429 responses, else `SeraError`.
    """
    # Sera usually wraps typed errors inside a `detail` object.
    envelope = body.get("detail") if isinstance(body, dict) else None
    if isinstance(envelope, dict):
        # Sera passes on two upstream envelope shapes: error_code/detail, and
        # code/message (an order-service reply such as a cancel's "not found").
        code = envelope.get("error_code") or envelope.get("code")
        msg = envelope.get("detail") or envelope.get("message") or ""
        # Validation errors name the field and the value Sera refused.
        if envelope.get("field"):
            msg += f" ({envelope['field']}={envelope.get('submitted')!r})"
    else:
        # Non-dict detail bodies still get surfaced as text.
        code = None
        msg = str(envelope) if envelope is not None else ""

    cls = SeraError
    if status_code == 429:
        # Sera's cancel cooldown answers {"detail": "Cancel cooldown not yet
        # elapsed ..."} and its wallet rate limit {"detail": "Rate limit
        # exceeded"}, neither with an error code, so the message tells them apart.
        cls = SeraCancelCooldown if "cooldown" in msg.lower() else SeraRateLimit
    return cls(code, msg, status_code)
