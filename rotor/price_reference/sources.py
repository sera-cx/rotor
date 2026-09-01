"""External FX source adapters used by the reference-price oracle."""

from __future__ import annotations

import datetime as dt
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx

from rotor.config import get_config
from rotor.price_reference.models import PriceObservation

# Default per-source staleness windows. Intraday providers (Wise) should be
# rejected after minutes; daily-published official references (ECB/Fed/BNM/MAS)
# legitimately carry the same rate across a business day plus weekends, so they
# get a wider default. Both are overridable per source via config.
INTRADAY_MAX_AGE_S = 600.0          # 10 minutes
DAILY_MAX_AGE_S = 48 * 60 * 60.0    # 48 hours

# Default HTTP read/connect timeout for source fetches. Generous enough for slow
# public endpoints (e.g. FRED's CSV graph) while staying well under the poll
# cadence so a stuck request only costs one tick.
DEFAULT_TIMEOUT_S = 30.0


class RateSource(Protocol):
    """Minimal protocol implemented by price reference adapters."""

    # Human-readable adapter name used in ReferenceUnavailable messages.
    name: str
    # Default freshness window (seconds) the oracle applies for this provider.
    default_max_age_s: float

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Return quote-per-base rate for an ISO pair.

        `amount` is the base-currency size being worked. Most sources are pure
        reference feeds and ignore it; the Wise adapter uses it to price the
        amount-specific (fee-inclusive) effective rate via the quotes API.
        """


class WiseRateSource:
    """Wise quotes adapter — amount-aware effective FX rate.

    Uses the Wise quotes API (`POST /v3/quotes`) with the worked size as
    `sourceAmount`, so the benchmark reflects Wise's fee for the actual amount,
    not just the mid-market rate. The effective quote-per-base rate is
    `targetAmount / sourceAmount` for the cheapest enabled payment option (or a
    pinned `payIn`/`payOut`). Requires `WISE_API_TOKEN`.
    """

    # Source name carried into observations and logs.
    name = "wise"
    # Wise returns near-real-time quotes with their own observation timestamp.
    DEFAULT_MAX_AGE_S = INTRADAY_MAX_AGE_S
    # Fallback source amount when a caller does not pass one (e.g. ad-hoc checks).
    DEFAULT_AMOUNT = Decimal("1000")

    def __init__(
        self,
        *,
        base_url: str = "https://api.wise.com",
        token_env: str = "WISE_API_TOKEN",
        pay_in: str | None = None,
        pay_out: str | None = None,
        default_amount: object | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        """Configure the Wise quotes client and credential lookup."""
        # Drop a trailing slash so endpoint joins are deterministic.
        self.base_url = base_url.rstrip("/")
        # Store the config key name instead of the token value so secrets are
        # read only when a quote is requested.
        self.token_env = token_env
        # Optional payment-method pins; default selects the cheapest enabled one.
        self.pay_in = (str(pay_in).strip().upper() or None) if pay_in else None
        self.pay_out = (str(pay_out).strip().upper() or None) if pay_out else None
        # Fallback amount used only when a caller omits one.
        self.default_amount = (
            self.DEFAULT_AMOUNT
            if default_amount in (None, "")
            else _positive_decimal(default_amount)
        )
        # Per-source freshness window; defaults to the class intraday window.
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        # Allow tests to inject a fake client; otherwise create a bounded client.
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Create a Wise quote for `amount` and return the effective rate."""
        # The quotes API is authenticated; a token is mandatory.
        token = str(get_config(self.token_env, "") or "").strip()
        if not token:
            raise ValueError("WISE_API_TOKEN is required for the wise price source")
        # Use the worked size; fall back to the configured default if absent.
        source_amount = _positive_decimal(
            amount if amount is not None else self.default_amount
        )
        # Create an (un-profiled) quote priced for this exact source amount.
        response = self._http.post(
            f"{self.base_url}/v3/quotes",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "sourceCurrency": base.upper(),
                "targetCurrency": quote.upper(),
                "sourceAmount": float(source_amount),
            },
        )
        # Let httpx expose non-2xx responses as provider failures.
        response.raise_for_status()
        body = response.json()
        # Pick the cheapest enabled payment option (or a pinned one) and derive
        # the fee-inclusive quote-per-base rate from its amounts.
        option = _select_wise_option(body.get("paymentOptions"), self.pay_in, self.pay_out)
        option_source = _positive_decimal(option.get("sourceAmount", source_amount))
        option_target = _positive_decimal(option.get("targetAmount"))
        effective_rate = option_target / option_source
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=effective_rate,
            # Freshness is anchored to when Wise set the rate.
            ts=_parse_ts(body.get("rateTimestamp") or body.get("createdTime")),
            raw={
                "mid_rate": str(body.get("rate", "")),
                "effective_rate": str(effective_rate),
                "source_amount": str(option_source),
                "target_amount": str(option_target),
                "pay_in": option.get("payIn"),
                "pay_out": option.get("payOut"),
                "fee_total": str((option.get("fee") or {}).get("total", "")),
                "rate_timestamp": body.get("rateTimestamp"),
            },
        )


class EcbRateSource:
    """ECB daily euro reference rates.

    ECB publishes rates as "1 EUR = X currency". We cross through EUR to
    return quote-per-base for the requested pair.
    """

    # Source name carried into observations and logs.
    name = "ecb"
    # ECB publishes once per TARGET business day; tolerate a wider staleness window.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S

    def __init__(
        self,
        *,
        url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        """Configure the ECB XML feed client."""
        # ECB is a full feed URL, not a base URL.
        self.url = url
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        # Allow tests to inject a fake client; otherwise create a bounded client.
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch ECB daily rates and cross through EUR into quote-per-base."""
        # ECB publishes one XML document containing multiple currency rates.
        response = self._http.get(self.url)
        response.raise_for_status()
        date, rates = _parse_ecb_xml(response.text)
        # The parser always seeds EUR=1 and fills other currencies from XML.
        base_rate = rates[base.upper()]
        quote_rate = rates[quote.upper()]
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=quote_rate / base_rate,
            # Stamp the published date, not fetch time, so a stale (e.g. weekend
            # or holiday) feed is detectable by the freshness guard.
            ts=_published_ts(date),
            raw={"date": date, "rate_base": str(base_rate), "rate_quote": str(quote_rate)},
        )


class FedH10RateSource:
    """Federal Reserve H.10 FX rates via the official FRED API.

    Uses the FRED ``series/observations`` JSON API at ``api.stlouisfed.org``,
    which requires a free ``FRED_API_KEY``. The legacy ``fredgraph.csv`` graph
    endpoint is fronted by Akamai Bot Manager and tarpits/resets headless
    clients, so it is not used.

    The adapter crosses through USD. Series orientation differs by currency:
    most supported currencies are quoted as currency-per-USD, while EUR is
    quoted as USD-per-EUR, so values are normalized before crossing.
    """

    name = "fed"
    # FRED daily series lag publication and pause on US holidays.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S

    DEFAULT_SERIES = {
        "EUR": "DEXUSEU",
        "MYR": "DEXMAUS",
        "SGD": "DEXSIUS",
    }
    USD_PER_CURRENCY_SERIES = {"EUR"}

    def __init__(
        self,
        *,
        base_url: str = "https://api.stlouisfed.org/fred/series/observations",
        api_key: str = "",
        series_by_currency: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        # The free FRED API key; read lazily so the source constructs without it
        # and only fails (clearly) when a quote is actually requested.
        self.api_key = api_key
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        configured = self.DEFAULT_SERIES if series_by_currency is None else series_by_currency
        self.series_by_currency = {
            key.upper(): value
            for key, value in configured.items()
        }
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch Fed H.10/FRED rates and cross through USD."""
        base = base.upper()
        quote = quote.upper()
        base_per_usd, base_raw = self._currency_per_usd(base)
        quote_per_usd, quote_raw = self._currency_per_usd(quote)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=quote_per_usd / base_per_usd,
            # Use the oldest input observation date so a stale leg is detectable.
            ts=_published_ts(base_raw.get("date"), quote_raw.get("date")),
            raw={"base": base_raw, "quote": quote_raw},
        )

    def _currency_per_usd(self, currency: str) -> tuple[Decimal, dict[str, str]]:
        if currency == "USD":
            return Decimal("1"), {"currency": "USD", "rate": "1"}
        series = self.series_by_currency.get(currency)
        if not series:
            raise KeyError(f"Fed H.10 series not configured for {currency}")
        if not self.api_key:
            raise ValueError(
                "FRED_API_KEY is required for the fed price source "
                "(free key: https://fred.stlouisfed.org/docs/api/api_key.html)"
            )
        # Request the most recent observations, newest first, and use the first
        # usable (non-".") value — the very latest day may be unpublished.
        response = self._http.get(
            self.base_url,
            params={
                "series_id": series,
                "api_key": self.api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 10,
            },
        )
        response.raise_for_status()
        date, raw_rate = _parse_fred_observations(response.json(), series)
        rate = _positive_decimal(raw_rate)
        if currency in self.USD_PER_CURRENCY_SERIES:
            rate = Decimal("1") / rate
        return rate, {"currency": currency, "series": series, "date": date, "rate": raw_rate}


class BnmRateSource:
    """Bank Negara Malaysia OpenAPI FX rates crossed through MYR."""

    name = "bnm"
    # BNM publishes daily reference rates around mid-session.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S

    def __init__(
        self,
        *,
        base_url: str = "https://api.bnm.gov.my/public/exchange-rate",
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch BNM rates and cross through MYR."""
        base = base.upper()
        quote = quote.upper()
        base_myr, base_raw = self._myr_per_currency(base)
        quote_myr, quote_raw = self._myr_per_currency(quote)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=base_myr / quote_myr,
            # BNM nests the publication date under each row's rate object.
            ts=_published_ts(_bnm_row_date(base_raw), _bnm_row_date(quote_raw)),
            raw={"base": base_raw, "quote": quote_raw},
        )

    def _myr_per_currency(self, currency: str) -> tuple[Decimal, dict]:
        if currency == "MYR":
            return Decimal("1"), {"currency_code": "MYR", "unit": 1, "rate": "1"}
        response = self._http.get(
            f"{self.base_url}/{currency}",
            headers={"Accept": "application/vnd.BNM.API.v1+json"},
        )
        response.raise_for_status()
        row = response.json()["data"]
        rate = _parse_bnm_rate(row)
        return rate, row


class MasRateSource:
    """MAS/public Singapore exchange rates crossed through SGD.

    The default data.gov.sg resource covers USD/SGD. Additional currencies can
    be supplied by config when MAS publishes compatible resource ids.
    """

    name = "mas"
    # data.gov.sg exposes the latest official daily SGD rates.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S

    DEFAULT_RESOURCE_IDS = {
        "USD": "d_046ff8d521a218d9178178cfbfc45c2c",
    }

    def __init__(
        self,
        *,
        url: str = "https://data.gov.sg/api/action/datastore_search",
        resource_ids: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        configured = self.DEFAULT_RESOURCE_IDS if resource_ids is None else resource_ids
        self.resource_ids = {
            key.upper(): value
            for key, value in configured.items()
        }
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch MAS rates and cross through SGD."""
        base = base.upper()
        quote = quote.upper()
        base_sgd, base_raw = self._sgd_per_currency(base)
        quote_sgd, quote_raw = self._sgd_per_currency(quote)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=base_sgd / quote_sgd,
            # The datastore record carries the official rate date.
            ts=_published_ts(base_raw.get("date"), quote_raw.get("date")),
            raw={"base": base_raw, "quote": quote_raw},
        )

    def _sgd_per_currency(self, currency: str) -> tuple[Decimal, dict]:
        if currency == "SGD":
            return Decimal("1"), {"currency": "SGD", "rate": "1"}
        resource_id = self.resource_ids.get(currency)
        if not resource_id:
            raise KeyError(f"MAS resource id not configured for {currency}")
        response = self._http.get(
            self.url,
            params={"resource_id": resource_id, "limit": 1, "sort": "date desc"},
        )
        response.raise_for_status()
        row = response.json()["result"]["records"][0]
        field = f"exchange_rate_{currency.lower()}"
        rate = _positive_decimal(row[field])
        return rate, {"currency": currency, "resource_id": resource_id, **row}


class CoinGeckoRateSource:
    """CoinGecko simple-price adapter — crypto-native intraday FX reference.

    CoinGecko prices one asset in many fiat currencies at once, so a fiat pair is
    obtained by asking for a single bridge asset in both legs and crossing it —
    the same shape as `EcbRateSource` crossing through EUR:

        SGD/USD = (USD per bridge) / (SGD per bridge)

    The bridge defaults to a fiat-pegged stablecoin, which keeps crypto basis out
    of the crossed rate to first order; a depegged or thinly-priced bridge would
    push straight into the quote, so it stays operator-configurable. Both legs
    come from one response and therefore share one publication timestamp.

    The endpoint is public. An API key is optional and only raises rate limits.
    """

    # Source name carried into observations and logs.
    name = "coingecko"
    # CoinGecko refreshes continuously; hold it to the intraday window.
    DEFAULT_MAX_AGE_S = INTRADAY_MAX_AGE_S
    # Fiat-pegged bridge asset used to cross two fiat legs in one request.
    DEFAULT_BRIDGE_ID = "tether"
    # Demo keys use this header; Pro deployments override it with the base URL.
    DEFAULT_API_KEY_HEADER = "x-cg-demo-api-key"

    def __init__(
        self,
        *,
        base_url: str = "https://api.coingecko.com/api/v3",
        bridge_id: str | None = None,
        api_key: str = "",
        api_key_header: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        """Configure the CoinGecko simple-price client and bridge asset."""
        # Drop a trailing slash so endpoint joins are deterministic.
        self.base_url = base_url.rstrip("/")
        # Empty/whitespace config falls back to the default bridge.
        self.bridge_id = (str(bridge_id).strip() or self.DEFAULT_BRIDGE_ID) if bridge_id else self.DEFAULT_BRIDGE_ID
        # Optional; absent means the public rate limit applies.
        self.api_key = str(api_key or "").strip()
        self.api_key_header = (
            str(api_key_header).strip() if api_key_header else self.DEFAULT_API_KEY_HEADER
        )
        # Per-source freshness window; defaults to the class intraday window.
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        # Allow tests to inject a fake client; otherwise create a bounded client.
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch both legs in one request and cross them through the bridge."""
        base_ccy = base.upper()
        quote_ccy = quote.upper()
        # One request covers both legs; de-duplicate so a same-currency pair
        # still asks for a valid single vs_currency.
        vs_currencies = sorted({base_ccy.lower(), quote_ccy.lower()})
        headers = {self.api_key_header: self.api_key} if self.api_key else None
        response = self._http.get(
            f"{self.base_url}/simple/price",
            params={
                "ids": self.bridge_id,
                "vs_currencies": ",".join(vs_currencies),
                "include_last_updated_at": "true",
            },
            headers=headers,
        )
        # Let httpx expose non-2xx responses (including 429s) as provider failures.
        response.raise_for_status()
        base_per_bridge, quote_per_bridge, last_updated = _coingecko_bridge_legs(
            response.json(), self.bridge_id, base_ccy, quote_ccy
        )
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            # Bridge units cancel, leaving quote-per-base.
            rate=quote_per_bridge / base_per_bridge,
            # Stamp CoinGecko's own update time so a stalled feed is detectable
            # by the freshness guard rather than looking fresh on fetch time.
            ts=_parse_ts(last_updated),
            raw={
                "bridge_id": self.bridge_id,
                "rate_base": str(base_per_bridge),
                "rate_quote": str(quote_per_bridge),
                "last_updated_at": last_updated,
            },
        )


def _pair(base: str, quote: str) -> str:
    """Format a normalized ISO pair label."""
    return f"{base.upper()}/{quote.upper()}"


def _select_wise_option(options: Any, pay_in: str | None, pay_out: str | None) -> dict:
    """Choose the Wise payment option to price the effective rate from.

    Disabled options (funding methods unavailable to the account) are skipped.
    A pinned `payIn`/`payOut` wins; otherwise the cheapest enabled option (the
    one returning the most target currency) is used.
    """
    if not isinstance(options, list) or not options:
        raise ValueError("Wise quote returned no payment options")
    enabled = [
        opt for opt in options
        if isinstance(opt, dict) and not opt.get("disabled")
    ]
    if not enabled:
        raise ValueError("Wise quote returned no enabled payment options")
    if pay_in or pay_out:
        for opt in enabled:
            if (not pay_in or str(opt.get("payIn", "")).upper() == pay_in) and (
                not pay_out or str(opt.get("payOut", "")).upper() == pay_out
            ):
                return opt
        raise ValueError(
            f"Wise quote has no enabled option for payIn={pay_in} payOut={pay_out}"
        )
    # Cheapest enabled option = the one delivering the most target currency.
    return max(enabled, key=lambda opt: _option_target(opt))


def _option_target(option: dict) -> Decimal:
    """Return a payment option's target amount, or zero if unparseable."""
    try:
        return Decimal(str(option.get("targetAmount")))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _published_ts(*dates: Any) -> float:
    """Return the oldest parseable publication timestamp, or now if none.

    Daily references cross two currency observations; the crossed rate is only
    as fresh as its oldest input. A missing/unparseable date falls back to the
    current time so a source that omits a date is not falsely flagged stale.
    """
    stamps = [ts for ts in (_date_ts(date) for date in dates) if ts is not None]
    return min(stamps) if stamps else time.time()


def _date_ts(value: Any) -> float | None:
    """Parse a publication date/time into POSIX seconds, or None if unusable."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        # Accepts both date-only ("2026-05-31") and full ISO 8601 timestamps.
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    # Treat naive/date-only values as UTC midnight for deterministic freshness.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _bnm_row_date(row: Any) -> Any:
    """Extract a BNM row's publication date when present."""
    # Real BNM rows nest the date under `rate`; the synthetic MYR row has none.
    if isinstance(row, dict):
        rate = row.get("rate")
        if isinstance(rate, dict):
            return rate.get("date")
        return row.get("date")
    return None


def _positive_decimal(value: Any) -> Decimal:
    """Parse a positive Decimal from provider input."""
    try:
        # Convert through str to avoid binary float artifacts.
        out = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"rate is not decimal-like: {value!r}") from exc
    # Rates at or below zero are invalid for FX conversion and quoting.
    if out <= 0:
        raise ValueError(f"rate must be positive, got {out}")
    return out


def _parse_ts(value: Any) -> float:
    """Parse common provider timestamp shapes into POSIX seconds."""
    # Missing timestamp falls back to now, which is useful for simple providers
    # that only return a current rate.
    if value is None:
        return time.time()
    # Numeric timestamps are already seconds.
    if isinstance(value, int | float):
        return float(value)
    # String timestamps may be numeric, ISO 8601, or Wise's strict format.
    text = str(value).strip()
    if not text:
        return time.time()
    # Python's fromisoformat expects an explicit +00:00 instead of trailing Z.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        # Try numeric strings first.
        return float(text)
    except ValueError:
        pass
    try:
        # Accept ISO 8601 variants first.
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        # Wise historically used this offset-aware format.
        parsed = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    # Treat naive timestamps as UTC so freshness checks are deterministic.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _parse_ecb_xml(text: str) -> tuple[str, dict[str, Decimal]]:
    """Extract the ECB publication date and currency-rate table from XML."""
    # ElementTree parses provider XML; malformed XML naturally raises here.
    root = ElementTree.fromstring(text)
    # ECB rates are quoted against EUR, so seed EUR at 1 for crossing.
    rates = {"EUR": Decimal("1")}
    date = ""
    for elem in root.iter():
        # ECB uses nested Cube elements; ignore envelope/container tags.
        if not elem.tag.endswith("Cube"):
            continue
        if "time" in elem.attrib:
            date = elem.attrib["time"]
        currency = elem.attrib.get("currency")
        rate = elem.attrib.get("rate")
        # Only leaf Cube elements carry currency/rate attributes.
        if currency and rate:
            rates[currency.upper()] = _positive_decimal(rate)
    # A useful feed must have a publication date and at least one non-EUR rate.
    if not date or len(rates) == 1:
        raise ValueError("ECB XML did not contain daily rates")
    return date, rates


def _parse_fred_observations(body: Any, series: str) -> tuple[str, str]:
    """Return the latest usable (date, value) from a FRED API observations body.

    The API returns ``{"observations": [{"date": ..., "value": ...}, ...]}``.
    With ``sort_order=desc`` the newest row is first; missing values are ".".
    """
    if not isinstance(body, dict):
        raise ValueError(f"FRED API returned a non-object body for {series}")
    observations = body.get("observations")
    if not isinstance(observations, list):
        raise ValueError(f"FRED API response has no observations for {series}")
    for row in observations:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "").strip()
        value = str(row.get("value") or "").strip()
        if date and value and value not in {".", "na", "NA"}:
            return date, value
    raise ValueError(f"FRED API returned no usable values for {series}")


def _parse_bnm_rate(row: dict) -> Decimal:
    """Normalize one BNM rate row into MYR per one currency unit."""
    unit = _positive_decimal(row.get("unit", 1))
    rate = dict(row.get("rate") or {})
    middle = rate.get("middle_rate")
    if middle is not None:
        return _positive_decimal(middle) / unit
    buying = rate.get("buying_rate")
    selling = rate.get("selling_rate")
    if buying is None or selling is None:
        raise ValueError(f"BNM row has no usable rate: {row!r}")
    return (_positive_decimal(buying) + _positive_decimal(selling)) / Decimal("2") / unit


def _coingecko_bridge_legs(
    body: Any, bridge_id: str, base: str, quote: str
) -> tuple[Decimal, Decimal, Any]:
    """Extract both fiat legs of the bridge asset from a simple-price payload.

    Returns `(base_per_bridge, quote_per_bridge, last_updated_at)`. Missing keys
    are raised as ValueError rather than KeyError so the oracle attributes the
    failure to the provider with a message naming what was unsupported.
    """
    entry = body.get(bridge_id) if isinstance(body, dict) else None
    if not isinstance(entry, dict):
        # An unknown coin id returns `{}` rather than an error status.
        raise ValueError(f"coingecko returned no prices for bridge id {bridge_id!r}")

    def leg(currency: str) -> Decimal:
        # simple/price keys vs_currencies in lower case.
        value = entry.get(currency.lower())
        if value is None:
            raise ValueError(
                f"coingecko does not price {bridge_id!r} in {currency} "
                "(unsupported vs_currency)"
            )
        return _positive_decimal(value)

    return leg(base), leg(quote), entry.get("last_updated_at")
