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

Optional live runner scenarios (they place real testnet orders):

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_RUN_TESTNET_SCENARIOS=1 \
ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 \
ROTOR_TESTNET_MARKET=MYRT/USDC \
ROTOR_TESTNET_SECOND_MARKET=CADC/USDC \
ROTOR_TESTNET_MID=0.245 \
ROTOR_TESTNET_SECOND_MID=0.714 \
ROTOR_TESTNET_WISE_FEE_BPS=100 \
ROTOR_TESTNET_FIXED_BPS=150 \
ROTOR_TESTNET_QTY_BASE=20 \
ROTOR_TESTNET_MAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_MAKER_API_KEY=... \
ROTOR_TESTNET_MAKER_API_SECRET=... \
ROTOR_TESTNET_TAKER_PRIVATE_KEY=0x... \
ROTOR_TESTNET_TAKER_API_KEY=... \
ROTOR_TESTNET_TAKER_API_SECRET=... \
poetry run pytest -q tests/testnet/test_vl_runner_scenarios.py
```

Real placement can leave live testnet orders behind if cancel cooldowns, funding
problems, or API failures intervene, so the scenarios are skipped unless both
`ROTOR_RUN_TESTNET_SCENARIOS=1` and `ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1` are set.

Pick markets nobody else trades on testnet, so the taker fills the maker and
not someone else's resting order. Sepolia's `XSGD/USDC` has an outside book near
the real SGD rate: at a test price of 1 the maker's bid crosses it at once, and
the taker trades with it instead of the maker. `MYRT/USDC` and `CADC/USDC` had
no outside orders as of 2026-10-23; a `POST /swap/quote` answering
`no_liquidity` both ways is a quick check.

Price the test orders near each market's real rate too. Something on testnet
takes mispriced quotes within seconds: in a 2026-10-23 run, an outside wallet
sold MYRT into a maker bid at 0.998 (MYR is worth about 0.245 USD) and its
leftover order then filled the taker. The examples use MYR/USD 0.245 and
CAD/USD 0.714, with a 1% stand-in Wise fee and a 1.5% `fixed_bps` so neither the
normal quotes nor the return orders land inside a reasonable reference price.

The live scenario harness runs the actual VL runner with a deterministic
test-only mid source (`ROTOR_TESTNET_MID` for the first market,
`ROTOR_TESTNET_SECOND_MID` for the second, defaulting to the first), lets the maker place its normal two-sided quote set, then
uses the taker wallet between ticks to fill one maker side. It verifies both
`bid -> return ask` and `ask -> return bid` behavior, including fill polling,
cancel-first requote, the return order's auction price and size (normal quotes
sit at a stand-in Wise fee, `ROTOR_TESTNET_WISE_FEE_BPS`, default 20), and VL
grouping when the configured markets share the spent token. The size must meet
both markets' minimums: Sepolia's `CADC/USDC` needs asks of at least 12.11
CADC, so a size of 1 would skip that pair. Expect about 15
minutes per direction: three ticks 305 s apart, then a wait for Sera's cancel
cooldown on exit.

`ROTOR_TESTNET_SCENARIO_BETWEEN_TICKS_SECONDS` defaults to `305` so Sera's
cancel cooldown has time to clear before the second tick. Setting it lower is
useful only when deliberately testing cooldown behavior; it can leave orders
live until expiration. `ROTOR_TESTNET_FIXED_BPS`, `ROTOR_TESTNET_RETURN_BPS`,
`ROTOR_TESTNET_BUDGET_AMOUNT`, `ROTOR_TESTNET_CLEANUP_WAIT_SECONDS`, and the
standard order TTL/fill-wait variables are optional overrides.
