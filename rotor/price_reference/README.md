# Price Reference

This package turns one named FX source into the mid used by each pair in
`rotor mm vl`, and prices every pair's normal orders through Wise.

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

Freshness windows, weekend carry-forward and their overrides are described in
[docs/vl-mm.md](../../docs/vl-mm.md#price-freshness); every setting is listed in
[`.env.example`](../../.env.example).
