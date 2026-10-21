# Testnet Tests

These tests call the real Sera testnet and are skipped unless explicitly enabled.
They are intended for integration boundaries, not pure helper functions.

Read-only public checks:

```bash
ROTOR_RUN_TESTNET=1 poetry run pytest -q tests/testnet
```

Optional signing-parity check against testnet's preview endpoint, still non-placing:

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_TESTNET_OWNER_ADDRESS=0x... \
ROTOR_TESTNET_MARKET=XSGD/USDC \
ROTOR_TESTNET_QTY_BASE=1 \
ROTOR_TESTNET_PRICE=1 \
poetry run pytest -q tests/testnet
```

Optional authenticated read checks:

```bash
ROTOR_RUN_TESTNET=1 \
SERA_API_KEY=... \
SERA_API_SECRET=... \
ROTOR_TESTNET_OWNER_ADDRESS=0x... \
poetry run pytest -q tests/testnet
```

Optional two-wallet maker/taker E2E:

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_RUN_TESTNET_E2E=1 \
ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 \
ROTOR_TESTNET_MARKET=XSGD/USDC \
ROTOR_TESTNET_SECOND_MARKET=EURC/USDC \
ROTOR_TESTNET_QTY_BASE=1 \
ROTOR_TESTNET_PRICE=1 \
ROTOR_TESTNET_MAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_MAKER_API_KEY=... \
ROTOR_TESTNET_MAKER_API_SECRET=... \
ROTOR_TESTNET_TAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_TAKER_API_KEY=... \
ROTOR_TESTNET_TAKER_API_SECRET=... \
poetry run pytest -q tests/testnet
```

The E2E test places the maker quote through `place_vl_batch()` with two VL legs,
then places an opposite taker order from the second wallet and polls fills from
both sides. `ROTOR_TESTNET_SECOND_PRICE`, `ROTOR_TESTNET_TAKER_PRICE`,
`ROTOR_TESTNET_TAKER_QTY_BASE`, `ROTOR_TESTNET_ORDER_TTL`, and
`ROTOR_TESTNET_FILL_WAIT_SECONDS` are optional overrides.

Real placement can leave live testnet orders behind if cancel cooldowns, funding
problems, or API failures intervene. The E2E test is therefore skipped unless
both `ROTOR_RUN_TESTNET_E2E=1` and `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1` are set.

Optional live runner scenarios:

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_RUN_TESTNET_SCENARIOS=1 \
ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 \
ROTOR_TESTNET_MARKET=XSGD/USDC \
ROTOR_TESTNET_SECOND_MARKET=EURC/USDC \
ROTOR_TESTNET_MID=1 \
ROTOR_TESTNET_QTY_BASE=1 \
ROTOR_TESTNET_MAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_MAKER_API_KEY=... \
ROTOR_TESTNET_MAKER_API_SECRET=... \
ROTOR_TESTNET_TAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_TAKER_API_KEY=... \
ROTOR_TESTNET_TAKER_API_SECRET=... \
poetry run pytest -q tests/testnet/test_vl_runner_scenarios.py
```

The live scenario harness runs the actual VL runner with a deterministic
test-only mid source, lets the maker place its normal two-sided quote set, then
uses the taker wallet between ticks to fill one maker side. It verifies both
`bid -> return ask` and `ask -> return bid` behavior, including fill polling,
cancel-first requote, `return_bps` pricing, and VL grouping when the configured
markets share the spent token.

`ROTOR_TESTNET_SCENARIO_BETWEEN_TICKS_SECONDS` defaults to `305` so Sera's
cancel cooldown has time to clear before the second tick. Setting it lower is
useful only when deliberately testing cooldown behavior; it can leave orders
live until expiration. `ROTOR_TESTNET_FIXED_BPS`, `ROTOR_TESTNET_RETURN_BPS`,
`ROTOR_TESTNET_BUDGET_AMOUNT`, `ROTOR_TESTNET_CLEANUP_WAIT_SECONDS`, and the
standard order TTL/fill-wait variables are optional overrides.
