"""Typed errors raised by `rotor.mm.exchange.client`."""

from __future__ import annotations

# Only the codes we actually branch on live as constants. Everything else
# is documented in sera-docs/docs/en/api-reference/endpoints/orders.md and
# carried through verbatim in `SeraError.error_code`.
STP_BLOCKED = "STP_BLOCKED"
CANCEL_COOLDOWN = "CANCEL_COOLDOWN"


class SeraError(Exception):
    """Raised on any 4xx response from Sera with a typed `error_code`.

    Attributes:
        error_code: Sera's typed code (e.g. ``"STP_BLOCKED"``,
            ``"AMOUNT_BELOW_MIN"``); empty string if absent.
        detail: Human-readable detail message.
        status_code: HTTP status code (4xx).
        payload: The raw response body dict (or ``{}`` if non-dict).
    """

    def __init__(
        self,
        error_code: str | None,
        detail: str,
        status_code: int,
        payload: dict | None = None,
    ) -> None:
        """Initialize a typed Sera error.

        Args:
            error_code: Sera's `error_code` string, or None if absent.
            detail: Human-readable detail text.
            status_code: HTTP status code from the response.
            payload: Optional raw body dict; stored for debugging.
        """
        # Normalize missing codes to an empty string so callers can compare
        # without repeatedly handling None.
        self.error_code = error_code or ""
        # Preserve the human-readable detail for logs/operator output.
        self.detail = detail
        # Preserve status/payload for tests and deeper debugging.
        self.status_code = status_code
        self.payload = payload or {}
        # Exception text includes the Sera code and HTTP status for one-line logs.
        super().__init__(f"{error_code}: {detail} (HTTP {status_code})")


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
        # Accept both old/new code key spellings defensively.
        code = envelope.get("error_code") or envelope.get("code")
        # Accept both old/new message key spellings defensively.
        msg = envelope.get("detail") or envelope.get("message") or ""
    else:
        # Non-dict detail bodies still get surfaced as text.
        code = None
        msg = str(envelope) if envelope is not None else ""

    cls = SeraError
    if status_code == 429:
        # Prefer Sera's typed code; only fall back to a message-substring guess
        # when no error_code is present, so a generic 429 that merely mentions
        # "cooldown" is not misclassified as a per-order cancel cooldown.
        if code:
            is_cooldown = code == CANCEL_COOLDOWN
        else:
            is_cooldown = "cooldown" in msg.lower()
        cls = SeraCancelCooldown if is_cooldown else SeraRateLimit
    # Keep the raw dict payload when available for postmortem inspection.
    return cls(code, msg, status_code, payload=body if isinstance(body, dict) else None)
