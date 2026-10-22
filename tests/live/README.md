# Live price-source tests

Opt-in tests that call the real FX reference sources. Two groups:

- **Public (keyless):** `ecb`, `bnm` (Bank Negara Malaysia), `mas` (data.gov.sg),
  and `wise` (anonymous quotes; `WISE_API_TOKEN` is used if set). Need outbound
  network access but no credentials.
- **Credentialed:** `fed` (the official FRED API — needs `FRED_API_KEY`), skipped
  unless the key is set.

```bash
make live-prices
# or
ROTOR_RUN_LIVE_PRICES=1 poetry run python -m pytest -q tests/live
# to also exercise fed:
ROTOR_RUN_LIVE_PRICES=1 FRED_API_KEY=... poetry run python -m pytest -q tests/live
```

Skipped by default (no `ROTOR_RUN_LIVE_PRICES=1`), so `make test` stays offline.

## What they check

- Each source fetches and parses a real quote into a `PriceObservation`.
- The rate falls inside a sanity band wide enough for normal FX drift but
  narrow enough that a **flipped orientation** (the cross-rate hazard in each
  adapter) lands outside the band and fails.
- ECB forward × inverse ≈ 1 (internal consistency).
- The fetch → parse → freshness-oracle path works end-to-end (using a generous
  window so weekends/holidays, when daily feeds are legitimately 1–3 days old,
  don't make the test flaky).

## Supported pairs (out of the box)

| Source | Credential | Pairs without extra config |
|--------|------------|----------------------------|
| `ecb`  | none | any two currencies in the ECB daily XML (USD, SGD, MYR, EUR, …), crossed via EUR |
| `bnm`  | none | any currency BNM publishes (USD, SGD, EUR, …), crossed via MYR |
| `mas`  | none | USD ↔ SGD only — extend with `MAS_EXCHANGE_RATE_RESOURCE_IDS_JSON` |
| `fed`  | `FRED_API_KEY` | AUD BRL CAD CHF EUR GBP JPY MXN MYR NZD SGD ZAR (+ USD), crossed via USD — replace with `FED_H10_SERIES_JSON` |
| `wise` | none (`WISE_API_TOKEN` optional) | broad — any Wise source/target |

Pairs outside a source's configured currencies are expected to raise
(`KeyError`) — that is the adapter failing closed, not a bug.
