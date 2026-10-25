"""Config loading for named price references."""

from __future__ import annotations

import math

from rotor.config import get_config, get_config_json, require_https
from rotor.price_reference.sources import (
    BnmRateSource,
    EcbRateSource,
    RateSource,
    WiseRateSource,
)

# price_source -> its adapter and the setting that may override its URL.
_SOURCES: dict[str, tuple[type[RateSource], str]] = {
    "wise": (WiseRateSource, "WISE_BASE_URL"),
    "ecb": (EcbRateSource, "ECB_RATES_URL"),
    "bnm": (BnmRateSource, "BNM_EXCHANGE_RATE_BASE_URL"),
}


def load_price_reference_source(name: str) -> RateSource:
    """Return the FX source a pair's `price_source` names.

    Each source applies its own default staleness window (intraday for Wise,
    daily for the official references). An operator can override a daily
    source's window with `<SOURCE>_MAX_AGE_S` (e.g. `ECB_MAX_AGE_S=259200`) or
    the global `PRICE_REFERENCE_MAX_AGE_S`. How long a stale daily rate is still
    quoted on comes from `<SOURCE>_MAX_CARRY_AGE_S` or
    `PRICE_REFERENCE_MAX_CARRY_AGE_S`. Wise prices every normal order, so its
    intraday window is fixed: none of these overrides applies to it.
    """
    source_name = str(name).lower()
    if source_name not in _SOURCES:
        # Keep accepted source names closed so misspellings fail loudly.
        raise ValueError(f"price source {name!r} must be one of: wise, ecb, bnm")
    cls, url_setting = _SOURCES[source_name]
    url = _url(url_setting, cls.DEFAULT_URL)
    if cls is WiseRateSource:
        return WiseRateSource(url=url)
    prefix = source_name.upper()
    return cls(
        url=url,
        max_age_s=_max_age_override(prefix),
        max_carry_age_s=_max_carry_age_override(prefix),
    )


def _max_age_override(prefix: str) -> float | None:
    """Return a per-source (then global) staleness override in seconds, if set."""
    return _seconds_override(f"{prefix}_MAX_AGE_S", "PRICE_REFERENCE_MAX_AGE_S")


def _url(name: str, default: str) -> str:
    """A source URL from config; https only, since tokens and prices travel on it."""
    return require_https(name, str(get_config(name, default) or default))


def _max_carry_age_override(prefix: str) -> float | None:
    """Return a per-source (then global) stale carry-forward limit, if set."""
    return _seconds_override(f"{prefix}_MAX_CARRY_AGE_S", "PRICE_REFERENCE_MAX_CARRY_AGE_S")


def _seconds_override(*keys: str) -> float | None:
    """Return the first configured positive number of seconds among `keys`."""
    for key in keys:
        raw = get_config(key, None)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be a number of seconds, got {raw!r}") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a positive number of seconds, got {raw!r}")
        return value
    return None


def load_token_to_iso() -> dict[str, str]:
    """Return optional token-to-ISO overrides."""
    configured = get_config_json("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", {})
    # Normalize both sides to uppercase because token and ISO symbols are
    # compared case-insensitively throughout the reference layer.
    return {
        str(k).upper(): str(v).upper()
        for k, v in dict(configured).items()
    }
