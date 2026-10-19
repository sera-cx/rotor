# Price-Reference Tests

This folder tests source adapters and freshness checks for benchmark prices.

Files:

- `test_aggregator.py`: `ReferencePriceOracle` freshness and stale carry-forward handling.
- `test_edge_cases.py`: source/config edge cases for malformed data and override
  handling.
- `test_sources.py`: token-to-ISO mapping plus Wise, ECB, Fed (FRED API), BNM,
  and MAS parsing/cross-rate behavior, including the `fed` API-key requirement.

Live network checks against the real sources live in `tests/live/` (opt-in).
