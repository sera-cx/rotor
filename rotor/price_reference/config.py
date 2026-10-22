"""Config loading for named price references."""

from __future__ import annotations

from rotor.config import get_config, get_config_json
from rotor.price_reference.sources import (
    BnmRateSource,
    EcbRateSource,
    FedH10RateSource,
    MasRateSource,
    RateSource,
    WiseRateSource,
)


def load_price_reference_source(name: str | None = None) -> RateSource:
    """Return one explicitly named FX source.

    Each source applies its own default staleness window (intraday for Wise,
    daily for the official references). An operator can override any source's
    window with `<SOURCE>_MAX_AGE_S` (e.g. `ECB_MAX_AGE_S=259200`) or the global
    `PRICE_REFERENCE_MAX_AGE_S`. How long a stale daily rate is still quoted on
    comes from `<SOURCE>_MAX_CARRY_AGE_S` or `PRICE_REFERENCE_MAX_CARRY_AGE_S`;
    Wise is intraday and never carries a stale quote, so neither applies to it.
    """
    # CLI-provided names override config; default to Wise for fresher live-ish
    # FX data when no explicit source is configured.
    source_name = str(name or get_config("PRICE_REFERENCE_SOURCE", "wise")).lower()
    if source_name == "wise":
        # Base URL is configurable so tests/deployments can point at mocks or
        # alternate Wise environments. Optional payment-method pins and a default
        # amount tune the amount-aware quotes pricing.
        return WiseRateSource(
            base_url=str(get_config("WISE_BASE_URL", "https://api.wise.com")),
            token_env="WISE_API_TOKEN",
            pay_in=get_config("WISE_PAY_IN", None),
            pay_out=get_config("WISE_PAY_OUT", None),
            default_amount=get_config("WISE_DEFAULT_AMOUNT", None),
            max_age_s=_max_age_override("WISE"),
        )
    if source_name == "ecb":
        # ECB is configured by a single XML feed URL.
        return EcbRateSource(
            url=str(
                get_config(
                    "ECB_RATES_URL",
                    "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                )
            ),
            max_age_s=_max_age_override("ECB"),
            max_carry_age_s=_max_carry_age_override("ECB"),
        )
    if source_name in {"fed", "h10", "fed_h10"}:
        return FedH10RateSource(
            base_url=str(
                get_config(
                    "FRED_API_BASE_URL",
                    "https://api.stlouisfed.org/fred/series/observations",
                )
            ),
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
            base_url=str(
                get_config(
                    "BNM_EXCHANGE_RATE_BASE_URL",
                    "https://api.bnm.gov.my/public/exchange-rate",
                )
            ),
            max_age_s=_max_age_override("BNM"),
            max_carry_age_s=_max_carry_age_override("BNM"),
        )
    if source_name == "mas":
        return MasRateSource(
            url=str(
                get_config(
                    "MAS_EXCHANGE_RATE_URL",
                    "https://data.gov.sg/api/action/datastore_search",
                )
            ),
            resource_ids=(
                get_config_json("MAS_EXCHANGE_RATE_RESOURCE_IDS_JSON", None)
                or MasRateSource.DEFAULT_RESOURCE_IDS
            ),
            max_age_s=_max_age_override("MAS"),
            max_carry_age_s=_max_carry_age_override("MAS"),
        )
    # Keep accepted source names closed so misspellings fail loudly.
    raise ValueError("PRICE_REFERENCE_SOURCE must be one of: wise, ecb, fed, bnm, mas")


def _max_age_override(prefix: str) -> float | None:
    """Return a per-source (then global) staleness override in seconds, if set."""
    # A source-specific window wins over the global one; both are optional.
    return _seconds_override(f"{prefix}_MAX_AGE_S", "PRICE_REFERENCE_MAX_AGE_S")


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
        if value <= 0:
            raise ValueError(f"{key} must be > 0, got {value}")
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
