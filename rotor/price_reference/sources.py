"""External FX source adapters used by the reference-price oracle."""

from __future__ import annotations

import datetime as dt
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from xml.etree import ElementTree

import httpx

from rotor.config import get_config
from rotor.price_reference.models import PriceObservation

# Default per-source staleness windows. Intraday providers (Wise) should be
# rejected after minutes; daily-published official references (ECB/Fed/BNM)
# legitimately carry the same rate across a business day plus weekends, so they
# get a wider default. Both are overridable per source via config.
INTRADAY_MAX_AGE_S = 600.0          # 10 minutes
DAILY_MAX_AGE_S = 48 * 60 * 60.0    # 48 hours

# How long a daily reference may still be quoted on once it is past its
# freshness window. Official daily sources publish nothing on weekends or public
# holidays, so instead of a pair going dark until they publish again, the runner
# keeps quoting on the last published rate (with a Telegram warning) until it is
# this old. A week covers a weekend plus a run of holidays, while a feed that has
# really died still stops being quoted. Intraday sources declare no carry: a
# stale Wise quote means the feed is broken, not closed.
DAILY_MAX_CARRY_AGE_S = 7 * 24 * 60 * 60.0  # 7 days

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
    # How long a stale observation may still be quoted on, or None to never carry
    # one forward. Optional: the oracle treats a missing value as None.
    default_max_carry_age_s: float | None

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Return quote-per-base rate for an ISO pair.

        `amount` is the base-currency size being worked. Most sources are pure
        reference feeds and ignore it; the Wise adapter prices its after-fee
        rates for that amount.
        """


class WiseRateSource:
    """Wise quotes API (`POST /v3/quotes`): what converting through Wise costs.

    Rotor uses Wise for after-fee rates: what someone actually gets converting a
    given amount through Wise, by the cheapest enabled transfer method that does
    not involve a Wise balance (a bank transfer or local debit, say). Fees fall
    with size, so each rate is priced for an exact amount.

    - `sell_rate`: quote currency received per base unit when converting an
      amount of base into quote.
    - `buy_rate`: quote currency paid per base unit to buy an amount of base.
    - `quote`: as a mid source, for currencies no central-bank source covers,
      halfway between the two for the worked amount.

    Wise prices anonymous quote requests; `WISE_API_TOKEN` only raises its rate
    limits.
    """

    # Source name carried into observations and logs.
    name = "wise"
    # Wise returns near-real-time quotes with their own observation timestamp.
    DEFAULT_MAX_AGE_S = INTRADAY_MAX_AGE_S
    # A stale Wise quote means the feed is broken, not closed: never carry it,
    # whatever carry limit is configured for the daily sources.
    DEFAULT_MAX_CARRY_AGE_S = None
    # Amount priced when a caller does not pass one (e.g. ad-hoc checks).
    DEFAULT_AMOUNT = Decimal("1000")
    # A reply fetched this recently is reused, so one tick asks Wise once per
    # currency pair, direction, and amount.
    REUSE_S = 60.0

    def __init__(
        self,
        *,
        base_url: str = "https://api.wise.com",
        token_env: str = "WISE_API_TOKEN",
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
        # Per-source freshness window; defaults to the class intraday window.
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        self.default_max_carry_age_s = self.DEFAULT_MAX_CARRY_AGE_S
        # Allow tests to inject a fake client; otherwise create a bounded client.
        self._http = http or httpx.Client(timeout=timeout)
        # (source, target, amount field, amount) -> (fetched at, reply)
        self._replies: dict[tuple[str, str, str, str], tuple[float, dict]] = {}

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Mid for `amount` of base: halfway between Wise's sell and buy rates."""
        qty = _positive_decimal(amount if amount is not None else self.DEFAULT_AMOUNT)
        sell = self.sell_rate(base, quote, qty)
        buy = self.buy_rate(base, quote, qty)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=(sell.rate + buy.rate) / 2,
            ts=min(sell.ts, buy.ts),
            raw={"sell_rate": str(sell.rate), "buy_rate": str(buy.rate), "amount": str(qty)},
        )

    def sell_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        """Quote received per base unit converting `qty_base` of base into quote."""
        body = self._reply(base, quote, "sourceAmount", qty_base)
        option = _cheapest_wise_option(body)
        rate = _positive_decimal(option.get("targetAmount")) / _positive_decimal(
            option.get("sourceAmount"))
        return self._observation(base, quote, rate, body, option)

    def buy_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        """Quote paid per base unit to buy exactly `qty_base` of base."""
        body = self._reply(quote, base, "targetAmount", qty_base)
        option = _cheapest_wise_option(body)
        rate = _positive_decimal(option.get("sourceAmount")) / _positive_decimal(
            option.get("targetAmount"))
        return self._observation(base, quote, rate, body, option)

    def _observation(
        self, base: str, quote: str, rate: Decimal, body: dict, option: dict
    ) -> PriceObservation:
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=rate,
            # Freshness is anchored to when Wise set the rate.
            ts=_parse_ts(body.get("rateTimestamp") or body.get("createdTime")),
            raw={
                "mid_rate": str(body.get("rate", "")),
                "pay_in": option.get("payIn"),
                "pay_out": option.get("payOut"),
            },
        )

    def _reply(self, source: str, target: str, amount_field: str, amount: Decimal) -> dict:
        """Wise's quote for converting `source` into `target`, one side fixed.

        A reply fetched within REUSE_S is reused. If a request fails, the last
        reply for the same amount is returned instead; callers reject it once
        its rate is older than the freshness window.
        """
        amount = _positive_decimal(amount)
        key = (source.upper(), target.upper(), amount_field, str(amount))
        now = time.time()
        cached = self._replies.get(key)
        if cached is not None and now - cached[0] < self.REUSE_S:
            return cached[1]
        headers = {"Content-Type": "application/json"}
        # Wise answers anonymous quote requests too; a token only raises limits.
        token = str(get_config(self.token_env, "") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._http.post(
                f"{self.base_url}/v3/quotes",
                headers=headers,
                json={
                    "sourceCurrency": source.upper(),
                    "targetCurrency": target.upper(),
                    amount_field: float(amount),
                },
            )
            # Let httpx expose non-2xx responses as provider failures.
            response.raise_for_status()
            body = response.json()
        except Exception:
            if cached is not None:
                return cached[1]
            raise
        # Forget replies too old to quote on, so the cache stays small.
        self._replies = {
            k: v for k, v in self._replies.items() if now - v[0] <= self.default_max_age_s
        }
        self._replies[key] = (now, body)
        return body


class EcbRateSource:
    """ECB daily euro reference rates.

    ECB publishes rates as "1 EUR = X currency". We cross through EUR to
    return quote-per-base for the requested pair.
    """

    # Source name carried into observations and logs.
    name = "ecb"
    # ECB publishes once per TARGET business day; tolerate a wider staleness window.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S = DAILY_MAX_CARRY_AGE_S

    def __init__(
        self,
        *,
        url: str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        max_carry_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        """Configure the ECB XML feed client."""
        # ECB is a full feed URL, not a base URL.
        self.url = url
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        self.default_max_carry_age_s = (
            self.DEFAULT_MAX_CARRY_AGE_S
            if max_carry_age_s is None
            else float(max_carry_age_s)
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

    The adapter crosses through USD. Each series' id says which way it quotes,
    following the H.10 release: ``DEXUS<cc>`` is U.S. dollars per currency unit
    (the release's asterisked currencies: AUD, EUR, GBP, NZD) and ``DEX<cc>US``
    is currency units per U.S. dollar (all others). A series named neither way
    is rejected when the source is built, rather than guessed.
    """

    name = "fed"
    # FRED daily series lag publication and pause on US holidays.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S = DAILY_MAX_CARRY_AGE_S

    # Every H.10 currency a token maps to (rotor.price_reference.symbols).
    DEFAULT_SERIES = {
        "AUD": "DEXUSAL",
        "BRL": "DEXBZUS",
        "CAD": "DEXCAUS",
        "CHF": "DEXSZUS",
        "EUR": "DEXUSEU",
        "GBP": "DEXUSUK",
        "JPY": "DEXJPUS",
        "MXN": "DEXMXUS",
        "MYR": "DEXMAUS",
        "NZD": "DEXUSNZ",
        "SGD": "DEXSIUS",
        "ZAR": "DEXSFUS",
    }

    def __init__(
        self,
        *,
        base_url: str = "https://api.stlouisfed.org/fred/series/observations",
        api_key: str = "",
        series_by_currency: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        max_carry_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        # The free FRED API key; read lazily so the source constructs without it
        # and only fails (clearly) when a quote is actually requested.
        self.api_key = api_key
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        self.default_max_carry_age_s = (
            self.DEFAULT_MAX_CARRY_AGE_S
            if max_carry_age_s is None
            else float(max_carry_age_s)
        )
        configured = self.DEFAULT_SERIES if series_by_currency is None else series_by_currency
        self.series_by_currency = {
            key.upper(): value
            for key, value in configured.items()
        }
        # Checked here so a misnamed series fails at startup, not mid-quote.
        self._usd_per_unit = {
            currency: _fred_quotes_usd_per_unit(series)
            for currency, series in self.series_by_currency.items()
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
        if self._usd_per_unit[currency]:
            rate = Decimal("1") / rate
        return rate, {"currency": currency, "series": series, "date": date, "rate": raw_rate}


def _fred_quotes_usd_per_unit(series: str) -> bool:
    """Return whether an H.10 FRED series quotes U.S. dollars per currency unit."""
    if re.fullmatch(r"DEXUS[A-Z]{2}", series):
        return True
    if re.fullmatch(r"DEX[A-Z]{2}US", series):
        return False
    raise ValueError(
        f"cannot tell which way FRED series {series!r} quotes; H.10 series are "
        "named DEXUS<cc> (U.S. dollars per unit) or DEX<cc>US (units per U.S. dollar)"
    )


class BnmRateSource:
    """Bank Negara Malaysia OpenAPI FX rates crossed through MYR."""

    name = "bnm"
    # BNM publishes daily reference rates around mid-session.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S = DAILY_MAX_CARRY_AGE_S

    def __init__(
        self,
        *,
        base_url: str = "https://api.bnm.gov.my/public/exchange-rate",
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        max_carry_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s)
        )
        self.default_max_carry_age_s = (
            self.DEFAULT_MAX_CARRY_AGE_S
            if max_carry_age_s is None
            else float(max_carry_age_s)
        )
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch BNM rates and cross through MYR."""
        base = base.upper()
        quote = quote.upper()
        # One read of BNM's latest session gives both currencies. The
        # per-currency endpoint defaults to the 11:30 interbank session, which
        # covers only a few currencies (no AED, CNY, or IDR, for one).
        rows = self._latest_session()
        base_myr, base_raw = _myr_per_currency(rows, base)
        quote_myr, quote_raw = _myr_per_currency(rows, quote)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=base_myr / quote_myr,
            # BNM nests the publication date under each row's rate object.
            ts=_published_ts(_bnm_row_date(base_raw), _bnm_row_date(quote_raw)),
            raw={"base": base_raw, "quote": quote_raw},
        )

    def _latest_session(self) -> dict[str, dict]:
        response = self._http.get(
            self.base_url,
            headers={"Accept": "application/vnd.BNM.API.v1+json"},
        )
        response.raise_for_status()
        return {
            str(row.get("currency_code", "")).upper(): row
            for row in response.json().get("data", [])
            if isinstance(row, dict)
        }


def _myr_per_currency(rows: dict[str, dict], currency: str) -> tuple[Decimal, dict]:
    """MYR per one unit of `currency` from a BNM session's rows."""
    if currency == "MYR":
        return Decimal("1"), {"currency_code": "MYR", "unit": 1, "rate": "1"}
    row = rows.get(currency)
    if row is None:
        raise ValueError(f"BNM publishes no {currency} rate")
    return _parse_bnm_rate(row), row


def _pair(base: str, quote: str) -> str:
    """Format a normalized ISO pair label."""
    return f"{base.upper()}/{quote.upper()}"


def _cheapest_wise_option(body: Any) -> dict:
    """The enabled payment option giving the most target per unit of source.

    Options that pay in from or out to a Wise balance are left out: they are
    conversions inside Wise, not what settling a trade through Wise costs.
    """
    options = body.get("paymentOptions") if isinstance(body, dict) else None
    if not isinstance(options, list) or not options:
        raise ValueError("Wise quote returned no payment options")
    usable = [
        opt for opt in options
        if isinstance(opt, dict)
        and not opt.get("disabled")
        and "BALANCE" not in (str(opt.get("payIn", "")).upper(), str(opt.get("payOut", "")).upper())
    ]
    if not usable:
        raise ValueError("Wise quote returned no enabled transfer method")
    return max(usable, key=_option_rate)


def _option_rate(option: dict) -> Decimal:
    """Target received per unit of source for one option, or zero if unparseable."""
    try:
        return Decimal(str(option.get("targetAmount"))) / Decimal(str(option.get("sourceAmount")))
    except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
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
