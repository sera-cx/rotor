# Virtual Liquidity Market Maker

`rotor mm vl` is a fixed-spread, cadence-based Sera VL runner. Each tick:

1. Fetches the latest source mid for every configured pair.
2. Builds one bid and one ask at `fixed_bps` from the mid.
3. Polls fills on the tracked live quote set.
4. Cancels tracked old placements when the local cancel guard permits.
5. Places the fresh quote set, using VL for eligible same-spent-token groups.

If a fill is detected, the next quote set tightens the opposite side to
`return_bps` from the latest source mid. Rotor does not place extra return
orders.

## Config

```toml
[[budget]]
token = "USDC"
amount = "5000"

fixed_bps = "50"
return_bps = "2"

[[pair]]
market = "XSGD/USDC"
price_source = "wise"
qty_base = "2000"

[[pair]]
market = "EURC/USDC"
price_source = "ecb"
qty_base = "1000"
fixed_bps = "40"
```

Rules:

- At least one `[[budget]]` is required.
- Markets cannot be duplicated, including inverse duplicates.
- `price_source` is `wise`, `ecb`, `fed`, `bnm`, or `mas`. `wise` needs
  `WISE_API_TOKEN` and `fed` (the official FRED API) needs a free `FRED_API_KEY`;
  `ecb`, `bnm`, and `mas` are keyless.
- `wise` is amount-aware: it prices a Wise quote for the pair's `qty_base` and
  uses the **fee-inclusive effective rate** for the cheapest enabled payment
  method, so its benchmark already reflects Wise's conversion cost at that size.
  The other sources are pure mid/reference rates. Pin a Wise method with
  `WISE_PAY_IN`/`WISE_PAY_OUT` if you do not want cheapest-enabled.
- `fixed_bps` can be global or overridden per pair.
- `return_bps` must be positive.

See `rotor/price_reference/README.md` for provider-specific endpoint and
cross-rate details.

## Price Freshness

Each source observation is stamped with the provider's published timestamp and
checked against a per-source freshness window: 10 minutes for `wise` (intraday)
and 48 hours for the daily references (`ecb`, `fed`, `bnm`, `mas`). Override a
single source with `<SOURCE>_MAX_AGE_S`, all of them with
`PRICE_REFERENCE_MAX_AGE_S`, or everything at runtime with `--max-reference-age`.

Daily references publish nothing on weekends or public holidays, so a stale daily
rate is carried forward: the pair keeps quoting on the last published rate until
that rate is 7 days old (`<SOURCE>_MAX_CARRY_AGE_S` or
`PRICE_REFERENCE_MAX_CARRY_AGE_S` to change). With `--telegram`, Rotor warns once
on the important channel when pairs start quoting on carried rates, repeats the
warning on the normal channel every 6 hours, and reports when fresh rates return
or when a rate passes its limit and the pair stops quoting. These alert episodes
are kept in the state database, so `--once` runs do not repeat them. A stale
`wise` quote is never carried. Pass `--halt-on-stale-reference` to skip every
stale pair instead.

Pairs are isolated within a tick: if one pair's reference is unavailable or past
its carry limit, or its quote fails market minimums, only that pair is skipped —
the other configured pairs still quote. A tick is fully skipped only when no pair
produces a quote.

## Dry Run

```bash
poetry run rotor mm vl \
  --config rotor/vl.config.toml \
  --dry-run \
  --once
```

Dry run fetches Sera market metadata and reference prices, builds quotes, and
prints only logs. It does not sign, place, or cancel orders.

## Live Run

```bash
poetry run rotor mm vl \
  --config rotor/vl.config.toml \
  --poll 305 \
  --order-ttl 600
```

Live mode requires `SERA_API_KEY`, `SERA_API_SECRET`, `SERA_BASE_URL`, and
`SERA_PRIVATE_KEY`. `SERA_BASE_URL` must be `https://` (an `http://` endpoint is
rejected, except `localhost` for development) because the maker signs orders
against it. Use `--cancel-on-exit` when shutdown should try to cancel tracked
live placements.

Only one instance may use a given state file at a time: Rotor takes an exclusive
lock on `<state>.lock` at startup and refuses to start if another process holds
it (set `ROTOR_STATE_PATH` to run a second instance on different state). Before
signing, Rotor also checks each previewed order's owner, recipient, token pair,
and amount against what it intended, and refuses to sign a mismatch. With
`--telegram`, a throttled alert fires if quoting stalls (no successful placement)
for several cadences, so a silent stall is not masked by the hourly heartbeat.

On startup, Rotor reattaches existing open orders for the configured markets.
It cancels those reattached placements before the first quote tick by default.
If cancellation is blocked by Sera's cooldown, Rotor waits 5 minutes and retries
once before continuing with any remaining live placements still tracked. A
one-tick run with `--once --cancel-on-exit` can perform a supervised
clean-place-clean cycle, but it is not a pure cancel-only command.

## Scheduled (fire-once) runs

`--once` runs a single tick and exits, which suits an external scheduler (e.g. a
Manus scheduled task) firing on a cadence. In `--once` mode the boot cancel does
a single pass and never blocks on Sera's cancel cooldown, so an invocation always
returns promptly. Each fire reattaches open orders from Sera, cancels the stale
set, places a fresh quote set, and exits (`0` placed/ok; non-zero = this run could
not requote and the next fire should retry). Schedule the interval at or above
Sera's ~5-minute cancel cooldown, keep `--order-ttl` at or above the interval, and
do not pass `--cancel-on-exit` (quotes should rest between fires). See
[manus.md](manus.md) for a full Manus walkthrough.

## Unresolved State

Rotor writes a small SQLite file at `.rotor_state.sqlite` by default. This is
not trade history. It stores only unresolved active orders and pending
return-side signals, then prunes rows after cancellation or after the return
side is used. Set `ROTOR_STATE_PATH` to move the file.

Add `--telegram` to send lifecycle/error alerts through the optional Telegram
notifier. Install the optional dependency with:

```bash
poetry install --extras telegram
```

If `TELEGRAM_TOKEN` is empty or the extra is not installed, notifier sends
degrade to dry-run logs and never crash the trading loop.

## Logging

The default `--log-level WARNING` keeps output small for lightweight hosts.
Routine per-tick quote, route, placement, cancellation, and reattach details are
`DEBUG` only:

```bash
poetry run rotor mm vl --config rotor/vl.config.toml --dry-run --once --log-level DEBUG
```

## Autonomous Updates

```bash
poetry run rotor mm vl \
  --config rotor/vl.config.toml \
  --poll 305 \
  --auto-update
```

Rotor checks the checkout on the first tick and then once per day. It runs
`git pull --ff-only` only when the directory is a clean Git worktree on a branch.
Pull failures are logged and the next cadence tick continues normally.

After a successful pull, Rotor stops the current loop, runs normal shutdown
cleanup, and re-execs the same command so the replacement process loads the new
code. `--auto-update-interval-seconds` exists for testing/custom deployments.

## Real Testnet E2E

The default testnet suite is non-mutating unless the two-wallet E2E gates are
set. That E2E places the maker quote through Sera VL, places an opposite taker
order from a second wallet, polls fills, and then attempts best-effort cleanup:

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_RUN_TESTNET_E2E=1 \
ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 \
poetry run python -m pytest -q tests/testnet
```

See `tests/testnet/README.md` for the required maker/taker wallet and market
environment variables.

For full live runner behavior, enable the separate scenario harness:

```bash
ROTOR_RUN_TESTNET=1 \
ROTOR_RUN_TESTNET_SCENARIOS=1 \
ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1 \
poetry run python -m pytest -q tests/testnet/test_vl_runner_scenarios.py
```

Those scenarios run the actual tick loop with a deterministic test mid, fill a
maker quote from a taker wallet between ticks, and verify the return-side
requote. They default to a `305` second wait between ticks so live cancellation
cooldowns can clear.
