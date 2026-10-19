# Rotor Audit Guide

Audit the code paths that can price, sign, place, cancel, notify, or update a
live autonomous deployment. Treat comments as orientation; verify behavior
against code, tests, and Sera API responses.

## Scope

Rotor currently exposes one runtime command: `rotor mm vl`. The quote logic is
fixed-spread only. Telegram is optional and must never affect trading safety.

## Start Here

Read these files in order:

1. `README.md`
2. `docs/vl-mm.md`
3. `rotor/cli/main.py`
4. `rotor/mm/vl_config.py`
5. `rotor/mm/vl_runner.py`
6. `rotor/mm/quote_engine.py`
7. `rotor/mm/exchange/client.py`
8. `rotor/mm/exchange/eip712.py`
9. `rotor/security/key.py`
10. `rotor/updater.py`
11. `rotor/notifications/telegram.py`

## First Commands

```bash
git status --short
poetry run ruff check rotor tests
poetry run python -m pytest -q
poetry check
```

## Core Invariants

Pricing:

- All price, size, budget, and rate math uses `Decimal`.
- The source rate is quote-per-base. `wise` prices the pair's size via the Wise
  quotes API and returns the fee-inclusive effective rate for that amount; the
  other sources (`ecb`/`fed`/`bnm`/`mas`) are pure mid/reference rates.
- Bids round down and asks round up.
- Amounts round down.
- Quotes fail closed when market minimums are not met.
- Observation timestamps are the provider's published date, not fetch time, so a
  stale daily feed is detectable. Staleness windows are per-source (intraday for
  `wise`, daily for `ecb`/`fed`/`bnm`/`mas`), overridable via config/CLI.
- A stale daily reference is carried forward until its carry limit (default 7
  days from the published date), because official sources do not publish on
  weekends or holidays. The oracle marks that price `stale`, the runner alerts
  on it, and a `wise` quote is never carried. `--halt-on-stale-reference`
  disables carrying.
- A failing, past-carry-limit, or sub-minimum pair is skipped in isolation; the
  other pairs still quote. A tick is fully skipped only when no pair produces a
  quote.
- Return-side quotes use `return_bps` from the latest source mid.

Live order lifecycle:

- Dry-run never signs, places, or cancels.
- Live mode unlocks the wallet before any signing path.
- Rotor reattaches configured open orders on startup when possible.
- Startup cancellation is default-on and runs before the first quote tick.
- If boot cancellation leaves placements live, Rotor waits 5 minutes and retries
  once while keeping any remaining placements tracked.
- `.rotor_state.sqlite` stores only unresolved active orders and pending return
  sides; cancelled/completed rows are pruned instead of retained as history.
- Each live cadence replaces the tracked quote set; old placements are cancelled
  before fresh placements.
- Cancel cooldowns keep local tracking for later retry, but an order Sera reports
  as gone (404 / not-found / already-cancelled / already-filled) is dropped so a
  stuck cancel cannot wedge quoting until restart.
- VL batch results are reconciled: server-cancelled legs are not tracked, and a
  clipped (amended) leg tracks its accepted size so a full fill at that size
  prunes. A malformed batch response keeps a cancelable `vl_batch_id` rather than
  orphaning the live orders.
- Partial placement failures cancel placements already created in that tick.
- Only one instance may use a state file: startup takes an exclusive advisory
  lock on `<state>.lock` and refuses to start if another process holds it.
- `--cancel-on-exit` is opt-in and cancels only tracked placements; an
  auto-update restart cancels tracked placements before re-exec regardless.

Sera signing and API:

- `client.bootstrap()` loads the EIP-712 domain before signing.
- Order placement signs Sera's preview-normalized payload, not a local guess, but
  first verifies the preview's `user`, `recipient`, token pair, and amounts
  against the intended order and refuses to sign a mismatch.
- `SERA_BASE_URL` must be `https://` (rejected otherwise, except `localhost`).
- Integer typed-data fields are coerced before signing (decimal and hex strings;
  array-typed fields left for `eth-account`).
- `uuid_int` packing rejects out-of-range executor and leg values.
- Cancel signatures use the packed order id or VL batch id expected by Sera.
- Order fills are paginated, so a high-fill order's quantity is not undercounted.
- API keys stay in headers; private keys, bearer tokens, signatures, and API
  secrets must not appear in logs.

VL behavior:

- Intents are grouped by spent token.
- VL is used only for budgeted groups meeting Sera's batch minimum.
- Singleton or unbudgeted groups use standalone placement.
- Local budget checks reject when the aggregate spend of all intents sharing a
  token exceeds that token's configured budget.
- Oversized same-spent-token groups are split into chunks of at most Sera's batch
  maximum, so one large group cannot fail every tick.
- Sera remains the authoritative shared-budget enforcer.
- Fills mark the opposite side for the next cadence quote; Rotor does not place
  extra return orders.

Autonomous updates:

- `--auto-update` is disabled by default.
- The updater only runs `git pull --ff-only`.
- Dirty worktrees, detached branches, non-Git directories, and pull failures are
  skipped or logged without stopping the loop.
- A successful pull stops the loop, runs normal shutdown cleanup, and re-execs
  the same command.

Telegram:

- `--telegram` is disabled by default.
- Missing token, missing optional dependency, or send failures must not crash the
  loop.
- Telegram logs stay compact; failures are one-line warnings.
- Notification payloads should not include secrets, signatures, or raw order
  payloads.

Logging:

- Default live logs are `WARNING` with compact one-line output.
- Routine per-tick quote, placement, cancellation, and reattach traces are
  `DEBUG` only.
- Rotor does not create log files by itself.

## Focused Test Map

```bash
poetry run python -m pytest -q tests/algo
poetry run python -m pytest -q tests/cli
poetry run python -m pytest -q tests/mm
poetry run python -m pytest -q tests/notifications
poetry run python -m pytest -q tests/price_reference
poetry run python -m pytest -q tests/security
poetry run python -m pytest -q tests/test_updater.py
```

Opt-in Sera testnet checks:

```bash
ROTOR_RUN_TESTNET=1 poetry run python -m pytest -q tests/testnet
```

The default testnet checks are non-mutating. The two-wallet maker/taker E2E
places real testnet orders only when both `ROTOR_RUN_TESTNET_E2E=1` and
`ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1` are set. The heavier live runner scenarios
are separately gated by `ROTOR_RUN_TESTNET_SCENARIOS=1` and use maker/taker
wallets to fill the actual loop between ticks.

Highest-signal files:

- `tests/mm/test_vl_runner_loop.py`: multi-tick quote replacement, fill-driven
  return-side tightening, tick-error recovery, and auto-update restart.
- `tests/mm/test_client.py`: Sera preview/sign/place/cancel behavior.
- `tests/mm/test_eip712.py`: EIP-712 schema and packed UUID layout.
- `tests/security/test_key.py`: private-key loading, signing, and lock behavior.
- `tests/notifications/test_telegram.py`: Telegram degradation and routing.
- `tests/test_updater.py`: autonomous Git update safety.
- `tests/testnet/test_sera_integration.py`: real Sera testnet public read,
  preview, authenticated read boundaries, and explicitly gated two-wallet
  maker/taker fill flow using VL placement.
- `tests/testnet/test_vl_runner_scenarios.py`: explicitly gated live runner
  fill/return requote scenarios using maker and taker wallets.

## Red Flags

Stop and investigate if you find:

- Float math in pricing, rates, sizes, or budgets.
- A path that signs before Sera bootstrap.
- A path that places or cancels in dry-run mode.
- A partial placement path that leaves already placed orders unmanaged.
- A cancel failure path that drops local order tracking.
- A stale source quote that still places orders outside the carry rules: a stale
  `wise` quote, a daily rate past its carry limit, or any stale rate under
  `--halt-on-stale-reference`.
- Destructive Git commands in the updater.
- Telegram or logging output that includes secrets or signatures.
- Default logs that emit per-tick chatter or grow local log files.

## Report Template

```text
Title:
Severity: Critical / High / Medium / Low / Informational
Files:
Impact:
Steps to reproduce:
Expected behavior:
Actual behavior:
Suggested fix:
Tests to add:
```

End the report with the tree state audited, commands run, output summary, and
any tests intentionally skipped.
