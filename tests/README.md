# Test Suite

`make test` (`poetry run python -m pytest -q`) runs the offline suite. These
opt-in groups are skipped by default:

- `tests/live/`: the real FX sources, with `ROTOR_RUN_LIVE_PRICES=1`; see
  [live/README.md](live/README.md).
- `tests/testnet/`: non-mutating checks against the real Sera testnet, with
  `ROTOR_RUN_TESTNET=1`.
- The live runner scenarios (`ROTOR_RUN_TESTNET_SCENARIOS=1`) place real testnet
  orders, so on top of `ROTOR_RUN_TESTNET=1` they also need
  `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1`. See [testnet/README.md](testnet/README.md)
  for their wallet and market settings.

Every testnet test reads `ROTOR_TESTNET_TIMEOUT`, its HTTP timeout in seconds
(default 15).
