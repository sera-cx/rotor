# Test Suite

Pytest coverage for the package.

Folders:

- `algo/`: pure fixed-spread quote tests.
- `cli/`: parser, command dispatch, and wallet gating tests.
- `mm/`: Sera client, EIP-712, VL config, and VL runner tests.
- `notifications/`: optional Telegram notifier tests.
- `price_reference/`: external price source and oracle tests.
- `security/`: signing-key loading and typed-data signing tests.
- `testnet/`: opt-in real Sera testnet integration checks, skipped by default.
- `live/`: opt-in live FX source checks (ECB/BNM/MAS keyless; FRED/Wise need a
  key), skipped unless `ROTOR_RUN_LIVE_PRICES=1`.

Root-level files:

- `test_config.py`: config loading and optional secret behavior.
- `test_package_health.py`: import checks for public modules and package version.
- `test_updater.py`: safe autonomous Git update behavior.

Run the default deterministic suite:

```bash
poetry run python -m pytest -q
```

Run non-mutating real testnet checks explicitly:

```bash
ROTOR_RUN_TESTNET=1 poetry run python -m pytest -q tests/testnet
```

Run the opt-in live FX source checks explicitly (set `FRED_API_KEY` to also
cover the `fed` source):

```bash
make live-prices   # ROTOR_RUN_LIVE_PRICES=1 pytest -q tests/live
```

The two-wallet maker/taker testnet E2E is skipped unless
`ROTOR_RUN_TESTNET_E2E=1` and `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1` are both set.
That E2E places real testnet orders and uses VL for the maker quote.

The heavier live runner scenarios are separately skipped unless
`ROTOR_RUN_TESTNET_SCENARIOS=1` and `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1` are
both set. Those scenarios run the actual tick loop with maker/taker wallets and
can take several minutes because cancel/requote behavior needs the Sera cooldown
window to clear.
