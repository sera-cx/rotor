# Rotor Audit Guide

Audit the code paths that can price, sign, place, cancel, notify, or update a
live autonomous deployment. Treat comments as orientation; verify behavior
against code, tests, and Sera API responses.

## Scope

Rotor currently exposes one runtime command: `rotor mm vl`. The quote logic is
Wise's after-fee prices for normal orders, with a return auction after fills.
Telegram is optional and must never affect trading safety.

## Start Here

Read these files in order:

1. `README.md`
2. `docs/vl-mm.md`
3. `rotor/cli/main.py`
4. `rotor/mm/vl_config.py`
5. `rotor/mm/vl_runner.py`
6. `rotor/mm/positions.py`
7. `rotor/mm/grid.py` and `rotor/mm/quote_engine.py`
8. `rotor/mm/exchange/client.py`
9. `rotor/mm/exchange/eip712.py`
10. `rotor/security/key.py`
11. `rotor/updater.py`
12. `rotor/notifications/telegram.py`

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
- Tokens are priced at their currency's reference rate (Sera's 1:1 stablecoin
  assumption); rotor reads no token market price, so a depeg is not detected.
- The source rate is quote-per-base and is the pair's mid. `ecb`/`fed`/`bnm`
  are published reference rates; `wise` is the midpoint of Wise's after-fee
  sell and buy rates for the pair's `qty_base`.
- Every normal order is priced at Wise's after-fee rate for its own final
  size (`_at_wise_prices`, run before and again after trimming): bid = Wise sell
  rate, ask = Wise buy cost, cheapest enabled method with no Wise balance on
  either end, rounded away from mid. Return orders (`is_return`) keep their
  auction price. A failed or >10-minute-old Wise rate skips the pair; a bid
  below the market minimum at Wise's price is dropped.
- Bids round down and asks round up.
- Amounts round down.
- Quotes fail closed when market minimums are not met.
- Observation timestamps are the provider's published date, not fetch time, so a
  stale daily feed is detectable; a reply without a usable date is a failed
  read (never stamped "now"). Staleness windows are per-source (intraday for
  `wise`, daily for `ecb`/`fed`/`bnm`), overridable via config/CLI.
- A stale daily reference is carried forward until its carry limit (default 7
  days from the published date), because official sources do not publish on
  weekends or holidays. The oracle marks that price `stale`, the runner alerts
  on it, and a `wise` quote is never carried. `--halt-on-stale-reference`
  disables carrying.
- A pair whose own source fails (`ReferenceUnavailable`, including a rate past
  its carry limit or any stale rate under `--halt-on-stale-reference`) uses
  Wise's mid-market rate instead (`_wise_mid`, from the same reply as the sell
  quote, at most 10 minutes old), recorded in `TickReferences.fallback` and
  alerted once per episode. Only when Wise fails too is the pair skipped.
- A failing (Wise down too) or sub-minimum pair is skipped in isolation; the
  other pairs still quote. A tick is fully skipped only when no pair produces a
  quote. A pair skipped on every tick for 15 minutes is alerted once
  (`DarkPairAlerts`, persisted in `dark_pairs`), and once more when it quotes.

Positions and return orders:

- Every order is recorded in `unsettled_orders` (side, limit price, signed
  expiry) after it is signed and before it is sent, under the ids rotor signed.
  The row is removed only when Sera refuses the order with a 4xx answer. On any
  other failure (timeout, dropped connection, 5xx, unreadable reply) it stays,
  and the placement is tracked as live so the next cycle cancels it. An order
  Sera never booked has no fills (Sera answers its fill read with a JSON 404,
  detail "Order not found") and settles at zero with a warning.
- An order is settled only after its expiry plus 60 seconds of Sera server time,
  and only when none of its fills is `pending`/`confirming` (or an unknown
  status). Only `settled` fills count; `failed`/`reverted` count as zero.
- Settling an order updates its market's position and deletes its ledger row in
  one SQLite transaction, so each order is counted exactly once.
- Each tick reads the fills of every ledger order and every live order once.
  Quoting adds the visible (not `failed`/`reverted`) fills of unsettled orders
  on top of the stored positions; only settled fills are ever stored.
- Only Sera's own "Order not found" 404 means "no fills". Any other 404 (an
  edge or proxy error page), a reply without an `items` list (an HTML 200, an
  empty body), or a row rotor cannot parse makes the read fail; likewise
  `GET /orders` without an `orders`/`trades` list is an error, not an empty
  book.
- An order whose fills cannot be read (an error, or a row rotor cannot parse)
  is never settled that tick, and its market is not quoted that tick; other
  markets are. After 12 ticks in a row it is reported as an error (and through
  Telegram when enabled).
- The breakeven is the order's limit price, which fills meet or beat.
- A return bid is never above the position's fill price and an ask never below
  it; bids round down and asks round up, so grid rounding cannot cross it.
- A return order is never closer to mid than `return_bps`, never spends more than
  the normal order it replaces, and replaces only the normal order on its side.
- A position under `RETURN_MIN_SHARE` (10%) of the normal order on its return
  side, or below the market minimum, keeps that normal order.
- All price math, including the auction's time fraction, is `Decimal`.

Live order lifecycle:

- Startup retries only temporary failures (network errors, Sera 5xx/429, a
  degraded `/health`), at most 3 attempts per clock hour counted in
  `<state>.boot.json`, then waits for the next hour; other failures stop at
  once. The Telegram notifier exists before any startup step, so failures alert.
- Dry-run never signs, places, or cancels.
- Live mode unlocks the wallet before any signing path.
- Rotor reattaches only open orders it placed (`_known_order_ids`: tracked
  orders plus the settlement ledger). Any other open order on a configured
  market stops startup (exit 1, alert) and is alerted once mid-run; rotor never
  adopts, and so never cancels, it.
- Startup cancellation is default-on and runs before the first quote tick.
- If boot cancellation leaves placements live, Rotor waits 5 minutes and retries
  once while keeping any remaining placements tracked.
- `.rotor_state.sqlite` stores unresolved active orders, unsettled orders, open
  positions, and open stale-reference alerts; resolved rows are pruned instead of
  retained as history. Positions persist until returned.
- Each live cadence replaces the tracked quote set; old placements are cancelled
  before fresh placements, and rotor then waits (up to about 5 s,
  `_wait_until_gone`) until Sera no longer lists them, because Sera applies a
  cancel after acknowledging it and a VL batch holds its budget until then.
- If the newest tracked order clears the cancel-cooldown guard within 60 seconds,
  the tick waits for it; otherwise the tick is skipped.
- Cancel cooldowns keep local tracking for later retry, but an order Sera reports
  as gone (404, a 400 whose message says "not found", or a not-found /
  already-cancelled / already-filled code) is dropped so a stuck cancel cannot
  wedge quoting until restart.
- VL batch results are reconciled: server-cancelled legs are not tracked, and a
  clipped (amended) leg tracks its accepted size so a full fill at that size
  prunes. A malformed batch response keeps a cancelable `vl_batch_id` rather than
  orphaning the live orders.
- Each VL batch and each standalone order is placed on its own: one Sera
  rejects is logged with its error code and skipped, and the tick's other
  placements still go out and are tracked. Nothing that placed is cancelled.
- Only one instance may use a state file: startup takes an exclusive advisory
  lock on `<state>.lock` and refuses to start if another process holds it.
- A state file belongs to one wallet, chain id, and Sera contract: the first
  live run records them (`identity` table) and any run with others refuses to
  start, so positions never move between networks or wallets. Dry-run never
  opens the state file.
- The stall watchdog (`StallWatchdog`) alerts when no fresh quote set has gone
  out for max(3 polls, 15 minutes). Its clock is in the state file, so `--once`
  fires share it; a fire checks only after placing nothing, so a successful
  fire never alerts on the gap before it.
- `--cancel-on-exit` is opt-in and cancels only tracked placements, waiting out
  Sera's 5-minute cancel cooldown once (a second stop signal skips it); a stop
  seen before Phase 7 places nothing new; leftovers are alerted and the loop
  exits 1. An auto-update restart cancels tracked placements once before
  re-exec regardless (the next process cancels leftovers at boot).

Sera signing and API:

- `client.bootstrap()` loads the EIP-712 domain before signing.
- Every order is built locally by `eip712.build_order_struct` and signed as
  `eip712.ORDER_TYPES`; rotor never signs a struct or type schema a server
  returns. Fee, recipient, and initial deposit are always zero, and expiration
  and uuid are rotor's own. Golden vectors from testnet's preview endpoint
  prove the local struct is the one the server verifies.
- Bootstrap refuses a `/config` that names any contract other than the known
  Sera contract for mainnet (chain 1) or Sepolia (chain 11155111); an unknown
  chain is allowed with a warning.
- A malformed `/markets` row is skipped with a warning (a configured market in
  it is then reported as unlisted); a reply with no markets list fails bootstrap.
- On mainnet, bootstrap also refuses `/markets` metadata that disagrees with
  `rotor/mm/exchange/known_tokens.py`: a market symbol that does not match its
  two tokens, a pinned symbol at another address or decimals, or a pinned
  address under another symbol. Tokens listed after the pin are accepted as
  reported.
- `SERA_BASE_URL` must be `https://` (rejected otherwise, except `localhost`).
- Integer typed-data fields are coerced before signing (decimal and hex strings;
  array-typed fields left for `eth-account`).
- `uuid_int` packing rejects out-of-range executor and leg values.
- Cancel signatures use the packed order id or VL batch id expected by Sera.
- Order fills and open orders are paginated. Past the page cap (5,000 fills,
  10,000 open orders) the read fails instead of returning a partial list.
- API-key requests are paced to Sera's documented per-wallet limits (reads
  10/s, trades 5/s, cancels 2/s). A rate-limit 429 is retried (twice, honouring
  Retry-After); a 503 is retried only for requests safe to repeat (GETs,
  cancels, single-order placement, which Sera dedupes on order_id),
  never for a VL batch. A cancel-cooldown 429 is never retried.
- `SERA_API_KEY`/`SERA_API_SECRET` are trimmed, and one still holding whitespace
  or a control character is refused at startup by name, never by value (the
  HTTP library would otherwise quote the rejected header, secret included).
- An invalid `SERA_PRIVATE_KEY` raises without a chained cause (`from None`),
  since int()'s message would quote it; any other unlock failure prints only
  its type. FRED takes its key in the URL, so FRED errors are re-raised with
  the key replaced by `<FRED_API_KEY>`.
- API keys stay in headers; private keys, bearer tokens, signatures, and API
  secrets must not appear in logs.

VL behavior:

- Intents are grouped by spent token.
- VL is used only for budgeted groups meeting Sera's batch minimum.
- A configured market Sera does not list at boot is skipped with a warning and
  (with `--telegram`) an important alert; boot fails only if Sera lists none.
- A running loop re-runs `client.bootstrap()` hourly (10 minutes after a
  failure, or after a 422 / below-minimum rejection) and rebuilds its pairs.
  Bootstrap replaces the client's domain, limits, executor id, and markets only
  after every read and check succeeds, so a failed refresh leaves the last good
  metadata in place.
- A market Sera rejects as not active (error code `PAIR_INACTIVE`, or a VL
  batch message naming the pair) is dropped from that batch, which is re-sent
  without it, and is not quoted again until the next successful refresh.
- Singleton or unbudgeted groups use standalone placement.
- Local budget checks count spend the way Sera locks it: a VL batch counts as
  its largest leg (siblings share one budget and only one can fill at a time),
  each standalone order counts in full, and batches and standalone orders on one
  token add up. A tick is rejected when that exceeds the token's budget.
- Before that check, any single order that would spend more than its token's
  budget, or than the vault holds of that token (from one `GET /balances` per
  tick), is trimmed to fit (size rounded down), or dropped if trimming takes it
  below the market minimum. An order on a token the vault does not hold is not
  sent. If balances cannot be read, budgets alone apply.
- Oversized same-spent-token groups are split into chunks of at most Sera's batch
  maximum, so one large group cannot fail every tick.
- Sera remains the authoritative shared-budget enforcer.
- A position's return order takes the place of the normal order on its side;
  Rotor never places a second order per market side.

Autonomous updates:

- `--auto-update` is disabled by default.
- The updater only runs `git pull --ff-only` (plus read-only `rev-parse` and
  `status`).
- Changed tracked files, detached branches, non-Git directories, and pull
  failures skip the update without stopping the loop, logged as warnings and
  alerted with `--telegram`. Untracked files do not block a pull.
- Whenever the checkout is at a commit other than the running one, the new code
  must pass `--check-config` (same argv; imports the runner and loads the config,
  no network/wallet/state) before a restart; otherwise the old version keeps
  running and the check is retried next interval.
- A restart stops the loop, runs normal shutdown cleanup, and re-execs the same
  command. A stop signal received during the update check wins: rotor exits
  instead. A failed pull's reason is passed on with any URL credentials
  replaced by `<credentials>`.

Telegram:

- `--telegram` is disabled by default.
- Missing token, missing optional dependency, or send failures must not crash the
  loop.
- Telegram logs stay compact; failures are one-line warnings.
- Notification payloads should not include secrets, signatures, or raw order
  payloads.
- The bot token is in every Telegram request URL: the token is trimmed, a send
  failure's message has it replaced with `<TELEGRAM_TOKEN>`, and the `httpx`,
  `httpcore`, and `telegram` loggers are capped at WARNING whatever
  `--log-level` is, so request URLs are never logged.

Logging:

- Default live logs are `WARNING` with compact one-line output.
- Startup logs (WARNING) and sends one line naming the network and wallet.
  A dry run logs its intended orders at WARNING, since that is its output.
- An order whose fills are still pending/confirming an hour after expiry is
  warned about once (`STUCK_SETTLEMENT_SECONDS`).
- An unexpected error that ends the loop is logged and sent as an important
  alert before rotor exits.
- Routine per-tick quote, placement, cancellation, and reattach traces are
  `DEBUG` only.
- Rotor does not create log files by itself.

## Focused Test Map

```bash
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

- `tests/mm/test_vl_runner_loop.py`: multi-tick quote replacement, a fill
  settling into a position and walking the return auction to the fill price,
  `--once` settlement across runs, tick-error recovery, a placement whose answer
  was lost, a market deactivated mid-run, metadata refresh, and auto-update
  restart.
- `tests/mm/test_positions.py`: position netting and the return-order price.
- `tests/mm/test_vl_runner.py`: fill settlement statuses and exactly-once
  counting, return-order sizing, and ledger recording on placement.
- `tests/mm/test_client.py`: local order building and signing, contract
  pinning, placement, cancel, pacing, and retry behavior.
- `tests/mm/test_eip712.py`: EIP-712 schema and packed UUID layout.
- `tests/security/test_key.py`: private-key loading, signing, and lock behavior.
- `tests/notifications/test_telegram.py`: Telegram degradation and routing.
- `tests/test_updater.py`: autonomous Git update safety.
- `tests/testnet/test_sera_integration.py`: real Sera testnet public read,
  signing parity with testnet's preview, authenticated read boundaries, and explicitly gated two-wallet
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
- An order sent before it is in `unsettled_orders`, a row removed for anything
  but a 4xx refusal, or a row that leaves the ledger without its settled fills
  applied in the same transaction (a fill missed or counted twice).
- A return order priced through its position's fill price, or sized above the
  normal order it replaces.
- A stale mid used outside the carry rules (a stale `wise` quote, a daily rate
  past its carry limit, or any stale rate under `--halt-on-stale-reference`)
  instead of being replaced by Wise's live mid.
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
