# Market-Making Tests

This folder tests the Sera-facing market-making runtime pieces.

Files:

- `test_client.py`: Sera REST client bootstrapping, preview/place/cancel flows,
  VL batch handling, fills, and typed error behavior.
- `test_eip712.py`: `uuid_int` packing and EIP-712 message shape.
- `test_quote_engine.py`: shared algo/client/time helper behavior.
- `test_vl_config.py`: TOML validation for budgets, markets, price sources,
  and per-pair fixed-spread overrides.
- `test_positions.py`: position netting from fills and the return-order price
  (reverse Dutch auction, hold at the fill price, `return_bps` cap).
- `test_vl_runner.py`: VL grouping, fill settlement and exactly-once counting,
  return-order sizing, budget caps, and placement tracking.
- `test_vl_runner_loop.py`: multi-tick run-loop behavior — quote replacement,
  fills settling into positions and returning through the auction, `--once`
  settlement across runs, per-pair isolation, and auto-update restart.
  Also covers quoting on carried-forward weekend rates and their alerts.
- `test_vl_runner_edges.py`: additional mock-backed VL edge cases (amendments,
  cancelled/gone legs, oversized-group splitting, single-instance lock).
