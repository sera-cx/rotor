"""External FX source adapters used by the reference-price oracle."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree

import httpx

from rotor.config import get_config
from rotor.price_reference.models import PriceObservation

# Default per-source staleness windows. Intraday providers (Wise) should be
# rejected after minutes; daily-published official references (ECB/BNM)
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

# A daily source's whole rate list is fetched at most this often, so one tick
# reads it once for all its pairs instead of once per pair. A failed fetch is
# remembered as long, so a hung feed costs one timeout per tick, not one per pair.
DAILY_REUSE_S = 60.0

# Default HTTP read/connect timeout for source fetches. Generous enough for slow
# public endpoints while staying well under the poll cadence, so a stuck request
# only costs one tick.
DEFAULT_TIMEOUT_S = 30.0


class RateSource:
    """Shared setup for the price reference adapters.

    `default_max_age_s` is the freshness window the oracle applies, and
    `default_max_carry_age_s` how long a stale rate may still be quoted on
    (None: never carried forward).
    """

    # Adapter name used in observations and ReferenceUnavailable messages.
    name = ""
    DEFAULT_URL = ""
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S: float | None = DAILY_MAX_CARRY_AGE_S

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_age_s: float | None = None,
        max_carry_age_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        # Drop a trailing slash so endpoint joins are deterministic.
        self.url = (url or self.DEFAULT_URL).rstrip("/")
        self.default_max_age_s = (
            self.DEFAULT_MAX_AGE_S if max_age_s is None else float(max_age_s))
        self.default_max_carry_age_s = (
            self.DEFAULT_MAX_CARRY_AGE_S if max_carry_age_s is None else float(max_carry_age_s))
        # Tests inject a fake client; otherwise a bounded one.
        self._http = http or httpx.Client(timeout=timeout)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Return the quote-per-base rate for an ISO pair. Only Wise uses
        `amount` (the base-currency size being worked)."""
        raise NotImplementedError


class WiseNoRoute(ValueError):
    """Wise answered, but does not offer this conversion (or no usable method).

    Wise sends to some currencies it cannot send from (CNH, NGN, ZAR, ...), and
    offers no route at all for others (ARS, PEN). Unlike an outage, this does
    not change from one tick to the next.
    """


class WiseRateSource(RateSource):
    """Wise quotes API (`POST /v3/quotes`): what converting through Wise costs.

    Rotor uses Wise for after-fee rates: what someone actually gets converting a
    given amount through Wise, by the cheapest enabled transfer method that does
    not involve a Wise balance (a bank transfer or local debit, say). Fees fall
    with size, so each rate is priced for an exact amount.

    - `sell_rate`: quote currency received per base unit when converting an
      amount of base into quote.
    - `buy_rate`: quote currency paid per base unit to buy an amount of base.
    - `mid_rate`, and `quote` as a mid source: Wise's own mid-market rate.

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

    DEFAULT_URL = "https://api.wise.com"

    def __init__(self, *, token_env: str = "WISE_API_TOKEN", **kwargs) -> None:
        super().__init__(**kwargs)
        # A stale Wise quote means the feed is broken, not closed: never carry
        # it, whatever carry limit is configured.
        self.default_max_carry_age_s = None
        # Store the config key name instead of the token value so secrets are
        # read only when a quote is requested.
        self.token_env = token_env
        # (source, target, amount field, amount) -> (fetched at, reply)
        self._replies: dict[tuple[str, str, str, str], tuple[float, dict]] = {}
        # When Wise last failed to answer at all, and how.
        self._unreachable: tuple[float, Exception] | None = None

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Wise's mid-market rate, as a pair's mid source."""
        return self.mid_rate(
            base, quote, amount if amount is not None else self.DEFAULT_AMOUNT)

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

    def mid_rate(self, base: str, quote: str, qty_base: Decimal) -> PriceObservation:
        """Wise's own mid-market rate, from the same reply `sell_rate` reads.

        When Wise cannot send from base, the reply `buy_rate` reads carries the
        same mid-market rate the other way round.
        """
        try:
            body = self._reply(base, quote, "sourceAmount", qty_base)
            rate = _positive_decimal(body.get("rate"))
        except WiseNoRoute:
            body = self._reply(quote, base, "targetAmount", qty_base)
            rate = 1 / _positive_decimal(body.get("rate"))
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=rate,
            ts=_wise_ts(body.get("rateTimestamp") or body.get("createdTime")),
        )

    def _observation(
        self, base: str, quote: str, rate: Decimal, body: dict, option: dict
    ) -> PriceObservation:
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=rate,
            # Freshness is anchored to when Wise set the rate.
            ts=_wise_ts(body.get("rateTimestamp") or body.get("createdTime")),
        )

    def _reply(self, source: str, target: str, amount_field: str, amount: Decimal) -> dict:
        """Wise's quote for converting `source` into `target`, one side fixed.

        A reply fetched within REUSE_S is reused. If a request fails, the last
        reply for the same amount is returned instead; callers reject it once
        its rate is older than the freshness window. Once Wise fails to answer
        at all (a hang, a refused connection), it is not asked again for
        REUSE_S, so a tick waits out one timeout rather than one per request.
        """
        amount = _positive_decimal(amount)
        key = (source.upper(), target.upper(), amount_field, str(amount))
        now = time.time()
        cached = self._replies.get(key)
        if cached is not None and now - cached[0] < self.REUSE_S:
            return cached[1]
        if self._unreachable is not None and now - self._unreachable[0] < self.REUSE_S:
            if cached is not None:
                return cached[1]
            raise self._unreachable[1]
        headers = {"Content-Type": "application/json"}
        # Wise answers anonymous quote requests too; a token only raises limits.
        token = str(get_config(self.token_env, "") or "").strip()
        if any(char.isspace() or not char.isprintable() for char in token):
            # An HTTP library error would quote the header, token and all.
            raise ValueError(
                f"{self.token_env} contains whitespace or a control character; check its value")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self._http.post(
                f"{self.url}/v3/quotes",
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
        except Exception as exc:
            if isinstance(exc, httpx.TransportError):
                self._unreachable = (now, exc)
            if cached is not None:
                return cached[1]
            # Wise answers 422 for a route it does not offer.
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 422:
                raise WiseNoRoute(
                    f"Wise does not convert {source.upper()} to {target.upper()}") from exc
            raise
        # Forget replies too old to quote on, so the cache stays small.
        self._replies = {
            k: v for k, v in self._replies.items() if now - v[0] <= self.default_max_age_s
        }
        self._replies[key] = (now, body)
        self._unreachable = None
        return body


class EcbRateSource(RateSource):
    """ECB daily euro reference rates.

    ECB publishes rates as "1 EUR = X currency". We cross through EUR to
    return quote-per-base for the requested pair.
    """

    # Source name carried into observations and logs.
    name = "ecb"
    # ECB publishes once per TARGET business day; tolerate a wider staleness window.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S = DAILY_MAX_CARRY_AGE_S

    # A full feed URL, not a base URL.
    DEFAULT_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._feed = _Reuse(DAILY_REUSE_S)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch ECB daily rates and cross through EUR into quote-per-base."""
        # ECB publishes one XML document containing every currency's rate.
        date, rates = self._feed.get(self._fetch)
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
        )

    def _fetch(self) -> tuple[str, dict[str, Decimal]]:
        response = self._http.get(self.url)
        response.raise_for_status()
        return _parse_ecb_xml(response.text)


class BnmRateSource(RateSource):
    """Bank Negara Malaysia OpenAPI FX rates crossed through MYR."""

    name = "bnm"
    # BNM publishes daily reference rates around mid-session.
    DEFAULT_MAX_AGE_S = DAILY_MAX_AGE_S
    DEFAULT_MAX_CARRY_AGE_S = DAILY_MAX_CARRY_AGE_S

    DEFAULT_URL = "https://api.bnm.gov.my/public/exchange-rate"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = _Reuse(DAILY_REUSE_S)

    def quote(
        self, base: str, quote: str, *, amount: Decimal | None = None
    ) -> PriceObservation:
        """Fetch BNM rates and cross through MYR."""
        base = base.upper()
        quote = quote.upper()
        # One read of BNM's latest session gives both currencies. The
        # per-currency endpoint defaults to the 11:30 interbank session, which
        # covers only a few currencies (no AED, CNY, or IDR, for one).
        rows = self._session.get(self._latest_session)
        base_myr, base_raw = _myr_per_currency(rows, base)
        quote_myr, quote_raw = _myr_per_currency(rows, quote)
        return PriceObservation(
            source=self.name,
            pair=_pair(base, quote),
            rate=base_myr / quote_myr,
            # BNM nests the publication date under each row's rate object.
            ts=_published_ts(_bnm_row_date(base_raw), _bnm_row_date(quote_raw)),
        )

    def _latest_session(self) -> dict[str, dict]:
        response = self._http.get(
            self.url,
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


class _Reuse:
    """The last result (or error) of a fetch, reused for `seconds`."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._at: float | None = None
        self._value: Any = None
        self._error: Exception | None = None

    def get(self, fetch: Callable[[], Any]) -> Any:
        now = time.time()
        if self._at is None or now - self._at >= self.seconds:
            self._at = now
            try:
                self._value, self._error = fetch(), None
            except Exception as exc:
                self._value, self._error = None, exc
        if self._error is not None:
            raise self._error
        return self._value


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
        raise WiseNoRoute("Wise quote returned no enabled transfer method")
    return max(usable, key=_option_rate)


def _option_rate(option: dict) -> Decimal:
    """Target received per unit of source for one option, or zero if unparseable."""
    try:
        return Decimal(str(option.get("targetAmount"))) / Decimal(str(option.get("sourceAmount")))
    except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
        return Decimal("0")


def _published_ts(*dates: Any) -> float:
    """Return the oldest parseable publication timestamp.

    Daily references cross two currency observations; the crossed rate is only
    as fresh as its oldest input. A reply with no usable date at all is a
    failed read: stamping it "now" would make a rate of unknown age look fresh.
    """
    stamps = [ts for ts in (parse_ts(date) for date in dates) if ts is not None]
    if not stamps:
        raise ValueError(f"no usable publication date in {dates!r}")
    return min(stamps)


def parse_ts(value: Any) -> float | None:
    """Parse an API date or timestamp into POSIX seconds, or None if unusable.

    Accepts numbers (already seconds), date-only values and ISO 8601 times;
    naive values are read as UTC so freshness checks are deterministic.
    """
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
        parsed = parsed.replace(tzinfo=dt.UTC)
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


def _wise_ts(value: Any) -> float:
    """The time Wise set a rate. A reply without one is a failed read, never
    "now": a rate of unknown age must not look fresh."""
    ts = parse_ts(value)
    if ts is None:
        raise ValueError(f"Wise reply has no timestamp it can read: {value!r}")
    return ts


def _parse_ecb_xml(text: str) -> tuple[str, dict[str, Decimal]]:
    """Extract the ECB publication date and currency-rate table from XML."""
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
