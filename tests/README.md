# Test Suite

`make test` (`poetry run python -m pytest -q`) runs the offline suite. These
opt-in groups are skipped by default:

- `tests/live/`: the real FX sources, with `ROTOR_RUN_LIVE_PRICES=1`; see
  [live/README.md](live/README.md).
- `tests/testnet/`: non-mutating checks against the real Sera testnet, with
  `ROTOR_RUN_TESTNET=1`.
- The two-wallet maker/taker E2E (`ROTOR_RUN_TESTNET_E2E=1`) and the live runner
  scenarios (`ROTOR_RUN_TESTNET_SCENARIOS=1`) place real testnet orders, so on
  top of `ROTOR_RUN_TESTNET=1` each also needs `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1`. See
  [testnet/README.md](testnet/README.md) for their wallet and market settings.

Every testnet test reads `ROTOR_TESTNET_TIMEOUT`, its HTTP timeout in seconds
(default 15).
