# Price Reference

This package turns one named FX source into the benchmark rate used by each pair
in `rotor mm vl`.

Files:

- `__init__.py`: public exports for source adapters, data models, config
  loaders, and symbol mapping helpers.
- `aggregator.py`: freshness-checking `ReferencePriceOracle` wrapper.
- `config.py`: config-driven factory for `wise`, `ecb`, `fed`, and `bnm`
  sources plus optional token-to-ISO overrides.
- `models.py`: dataclasses for raw source observations and quote-safe reference
  prices.
- `sources.py`: Wise, ECB, Fed H.10, and BNM source adapters.
- `symbols.py`: Sera token symbol to ISO currency mapping.

Available sources:

- `wise`: Wise quotes API (`POST /v3/quotes`). Works without a key;
  `WISE_API_TOKEN` is optional and only raises Wise's rate limits. Its
  after-fee rates for an exact amount, by the cheapest enabled transfer method
  that does not involve a Wise balance, are `sell_rate` (quote received per
  base converting base into quote) and `buy_rate` (quote paid per base buying
  base). The runner places every pair's normal orders at them. As a mid source
  (`quote`, or `mid_rate`), it returns Wise's mid-market rate, read from the
  buy-direction reply (inverted) when Wise cannot convert from the base. A
  route Wise does not offer (HTTP 422, or no usable transfer method) raises
  `WiseNoRoute`, which the runner tells apart from an outage. A reply is reused
  for 60 seconds, and on a failed request the last reply comes back for the
  caller's freshness check.
- `ecb`: ECB daily euro reference rates, crossed through EUR. Keyless. Pure mid.
- `fed`: Federal Reserve H.10 rates via the official FRED API
  (`api.stlouisfed.org/fred/series/observations`), crossed through USD. Needs a
  free `FRED_API_KEY` (the legacy `fredgraph.csv` scrape endpoint is fronted by
  Akamai Bot Manager and unreliable from servers). Built-in series cover AUD,
  BRL, CAD, CHF, EUR, GBP, JPY, MXN, MYR, NZD, SGD, and ZAR. Which way a series
  quotes is read from its FRED id: `DEXUS<cc>` is U.S. dollars per unit (AUD,
  EUR, GBP, NZD in the H.10 release) and `DEX<cc>US` is units per U.S. dollar;
  a series named neither way is rejected at startup. `FED_H10_SERIES_JSON`
  replaces the built-in list.
- `bnm`: Bank Negara Malaysia OpenAPI FX rates, crossed through MYR. Keyless.
  `ecb` and `bnm` each publish one list of every currency; it is fetched at
  most once a minute (`DAILY_REUSE_S`) and shared by all pairs, and a failed
  fetch is remembered as long, so a hung feed costs one timeout per tick.
There is no MAS source: MAS's only public daily dataset (on data.gov.sg)
stopped in October 2015, and its current API needs a registered key. ECB and
BNM both publish SGD.

Only `fed` requires a credential; `wise` (token optional), `ecb`, and `bnm` are
keyless.

## Freshness

Each observation is stamped with the provider's **published** timestamp (Wise's
quote time; the ECB `Cube time`; the FRED/BNM row date), not fetch time, so a
stale (e.g. weekend or holiday) feed is detectable. A reply without a usable
date is a failed read, never stamped with the current time. `ReferencePriceOracle`
treats an observation older than the source's window as stale:

- `wise`: 10 minutes (intraday).
- `ecb` / `fed` / `bnm`: 48 hours (daily references legitimately carry
  the same rate across a business day plus weekends).

Override one source with `<SOURCE>_MAX_AGE_S` (e.g. `ECB_MAX_AGE_S=259200`), all
of them with `PRICE_REFERENCE_MAX_AGE_S`, or everything at runtime with the
`--max-reference-age` CLI flag.

Past its window a daily reference is **carried forward** rather than rejected:
`ecb`, `fed`, and `bnm` publish nothing on weekends or public holidays, so
the oracle still returns the last published rate, marked `stale` with its
`age_s`, until that rate is 7 days old. Past that it raises `ReferenceTooStale`,
a `ReferenceUnavailable` subclass. `wise` declares no carry limit and is rejected
as soon as it is stale, whatever limit is configured. Change a daily source's
limit with `<SOURCE>_MAX_CARRY_AGE_S` or `PRICE_REFERENCE_MAX_CARRY_AGE_S`
(neither applies to `wise`), or disable carrying with
`ReferencePriceOracle(..., carry_stale=False)` (`--halt-on-stale-reference` on
the CLI).

The bot maps token markets to fiat ISO pairs before pricing:

| Sera market | FX pair |
| --- | --- |
| `XSGD/USDC` | `SGD/USD` |
| `MYRT/USDT` | `MYR/USD` |
| `EURC/USDC` | `EUR/USD` |

Environment config:

```bash
export WISE_API_TOKEN="..."            # optional: higher Wise rate limits
export FRED_API_KEY="..."              # required for the fed source
export FED_H10_SERIES_JSON='{"EUR":"DEXUSEU","MYR":"DEXMAUS","SGD":"DEXSIUS"}'
export ECB_MAX_AGE_S="172800"          # optional per-source staleness override
export BNM_MAX_CARRY_AGE_S="604800"    # optional: how long a stale daily rate is still quoted
```

The same `KEY=VALUE` lines can be placed in a root `.env`. Optional local
`rotor/secret.py` config remains a Python fallback:

```python
WISE_API_TOKEN = "..."
WISE_BASE_URL = "https://api.wise.com"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FRED_API_KEY = "..."  # free key: https://fred.stlouisfed.org/docs/api/api_key.html
FRED_API_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
FED_H10_SERIES_JSON = {"EUR": "DEXUSEU", "MYR": "DEXMAUS", "SGD": "DEXSIUS"}
BNM_EXCHANGE_RATE_BASE_URL = "https://api.bnm.gov.my/public/exchange-rate"
```

ECB, Fed H.10, and BNM are official/reference sources. They are useful as
transparent fallbacks or regional official feeds, but they are not live tick
sources in the same way Wise can be. `fed` uses the official FRED API and needs
a free `FRED_API_KEY`; ECB and BNM are keyless.
