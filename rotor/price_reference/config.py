"""Config loading for named price references."""

from __future__ import annotations

import math

from rotor.config import get_config, get_config_json, require_https
from rotor.price_reference.sources import (
    BnmRateSource,
    EcbRateSource,
    FedH10RateSource,
    RateSource,
    WiseRateSource,
)


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
    if source_name == "wise":
        # Base URL is configurable so tests/deployments can point at mocks or
        # alternate Wise environments.
        return WiseRateSource(
            base_url=_url("WISE_BASE_URL", "https://api.wise.com"),
            token_env="WISE_API_TOKEN",
        )
    if source_name == "ecb":
        # ECB is configured by a single XML feed URL.
        return EcbRateSource(
            url=_url("ECB_RATES_URL", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"),
            max_age_s=_max_age_override("ECB"),
            max_carry_age_s=_max_carry_age_override("ECB"),
        )
    if source_name == "fed":
        return FedH10RateSource(
            base_url=_url("FRED_API_BASE_URL", "https://api.stlouisfed.org/fred/series/observations"),
            api_key=str(get_config("FRED_API_KEY", "") or ""),
            series_by_currency=(
                get_config_json("FED_H10_SERIES_JSON", None)
                or FedH10RateSource.DEFAULT_SERIES
            ),
            max_age_s=_max_age_override("FED"),
            max_carry_age_s=_max_carry_age_override("FED"),
        )
    if source_name == "bnm":
        return BnmRateSource(
            base_url=_url("BNM_EXCHANGE_RATE_BASE_URL", "https://api.bnm.gov.my/public/exchange-rate"),
            max_age_s=_max_age_override("BNM"),
            max_carry_age_s=_max_carry_age_override("BNM"),
        )
    # Keep accepted source names closed so misspellings fail loudly.
    raise ValueError(f"price source {name!r} must be one of: wise, ecb, fed, bnm")


def _max_age_override(prefix: str) -> float | None:
    """Return a per-source (then global) staleness override in seconds, if set."""
    # A source-specific window wins over the global one; both are optional.
    return _seconds_override(f"{prefix}_MAX_AGE_S", "PRICE_REFERENCE_MAX_AGE_S")


def _url(name: str, default: str) -> str:
    """A source URL from config; https only, since tokens and prices travel on it."""
    return require_https(name, str(get_config(name, default) or default))


def _max_carry_age_override(prefix: str) -> float | None:
    """Return a per-source (then global) stale carry-forward limit, if set."""
    # Same precedence as the freshness window: the source-specific key wins.
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
    # Prefer the explicit JSON env var, but also accept the Python config value
    # used by `rotor.secret`.
    configured = (
        get_config_json("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", None)
        or get_config_json("PRICE_REFERENCE_TOKEN_TO_ISO", {})
    )
    # Normalize both sides to uppercase because token and ISO symbols are
    # compared case-insensitively throughout the reference layer.
    return {
        str(k).upper(): str(v).upper()
        for k, v in dict(configured).items()
    }
