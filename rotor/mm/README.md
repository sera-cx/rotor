# Market-Making Runtime Package

VL market-making runtime and shared quote helpers.

Files and folders:

- `quote_engine.py`: shared helpers for building the fixed-spread algo/client,
  formatting log values, handling signals, and tracking live orders.
- `vl_config.py`: TOML loader and validator for VL strategy config.
- `vl_runner.py`: cadence loop that builds quotes (isolating any failing pair),
  groups compatible intents into Sera VL batches, cancels/replaces tracked
  orders, and can call the daily Git updater. Holds a single-instance lock on the
  SQLite state, drops orders Sera reports as gone, and (with `--telegram`) alerts
  on a quoting stall and while pairs quote on carried-forward daily rates.
- `exchange/`: Sera REST client, EIP-712 payload helpers, and typed API errors.
