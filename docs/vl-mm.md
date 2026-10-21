# Virtual Liquidity Market Maker

`rotor mm vl` is a fixed-spread, cadence-based Sera VL runner. Each tick:

1. Reads the fills of its live quotes and of every placed order not yet settled.
   Fully filled orders stop being tracked, and positions are brought up to date.
2. Fetches the latest source mid for every configured pair.
3. Builds one bid and one ask at `fixed_bps` from the mid. A market with an open
   position quotes its return order on that side instead.
4. Cancels tracked old placements when the local cancel guard permits.
5. Places the fresh quote set, using VL for eligible same-spent-token groups, and
   records every placed order for settlement.

## Returning fills

A fill moves inventory: a filled bid leaves rotor holding extra base tokens, a
filled ask leaves it short of them. Rotor records that as a per-market
**position** (net base quantity, size-weighted fill price, size-weighted fill
time) in `rotor/mm/positions.py` and quotes it back on the opposite side:

- The return order replaces the normal order on that side. It starts at the
  normal opposite quote at fill time (`2 x fixed_bps` from the fill price) and
  walks linearly to the fill price over `return_window_hours` (default 48): a
  reverse Dutch auction that asks for less profit the longer it waits.
- After the window it **holds at the fill price indefinitely**. Rotor never
  returns a position at a loss against its fill price. Breakeven is measured on
  price alone; any fees or gas a deployment charges are not included.
- It never quotes closer to the latest mid than `return_bps`. If the market moved
  in rotor's favour, the order sits `return_bps` from mid instead of overpaying.
- It never spends more than the normal order it replaces, so a larger position is
  returned over several fills and budgets see no new spend. A position below the
  market's minimum order keeps the normal order on that side; the next fill there
  absorbs it.
- The other side keeps quoting normally. Fills there grow the position, and its
  fill price and clock become size-weighted averages.

Fills are counted from a ledger, not from the resting quote set. Every order
rotor places is recorded with its side, limit price, and signed expiry, and each
tick rotor reads the fills of every order in the ledger. Once an order has
expired (plus 60 seconds) and none of its fills is still `pending` or
`confirming`, its `settled` quantity is added to the stored position in the same
database transaction that removes it from the ledger. Each order is therefore
counted exactly once, however it left the book: cancelled at boot, left behind
by a previous `--once` run, or cancelled by a failed tick. Fills on orders that
are not final yet (still resting, cancelled but unexpired, or settling) are added
on top when quoting, so a fill holds its return side from the first tick that
sees it; only settled fills are stored. `failed` and `reverted` fills count as
nothing. The order's limit price stands in for the fill price: fills happen at
that price or better (on Sera a maker receives exactly its limit), so the
breakeven is never optimistic. A fill that lands between a tick's fill read and
its cancel is seen one tick later. If an order's fills cannot be read in a tick,
that order is not settled and its market sits the tick out while the others
quote; rotor reports it as an error if that lasts 12 ticks.

Requests to Sera are paced to its documented per-wallet limits (10 reads, 5
order placements and 2 cancels per second), and rate-limited or briefly failing
requests are retried, so a tick for 8 pairs takes about 10 seconds.

Positions do not see deposits, withdrawals, or trades outside this rotor's own
orders, orders placed by an older rotor version, or fills on a VL batch whose
returned order ids did not line up with its legs. A position on a market that is
no longer configured is kept but not quoted. Deleting the state file forgets
every position.

## Config

```toml
# Global settings go before the first [[...]] table: TOML puts any key that
# follows a table header inside that table.
fixed_bps = "50"
return_bps = "2"
return_window_hours = "48"

[[budget]]
token = "USDC"
amount = "5000"

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

- Global settings (`fixed_bps`, `return_bps`, `return_window_hours`) go above
  the first table; a `[[budget]]` holds only `token` and `amount`, and any other
  key there is rejected.
- At least one `[[budget]]` is required. A budget caps what one token's orders
  can spend, counted the way Sera locks it: a VL batch counts as its largest leg,
  because its legs share one budget and only one can fill at a time; each
  standalone order counts in full. So every pair in a VL batch can be sized to
  the whole budget. Sizes are fixed in base tokens, so a rising price raises a
  bid's spend; an order that would spend more than its budget is trimmed to fit
  (and dropped if trimming takes it below the market minimum).
- Each tick also reads the wallet's vault balances (one `GET /balances`), and no
  order may spend more of a token than the vault holds. An order on a token the
  vault does not hold is not sent at all: with only USDC funded, rotor quotes
  bids on `X/USDC` and no asks until a fill leaves it holding `X`. If the
  balances cannot be read, budgets alone apply.
- Markets cannot be duplicated, including inverse duplicates.
- Sera lists each pair in one orientation (e.g. `USDC/EURC`, not `EURC/USDC`);
  use the symbol `GET /markets` returns. A configured market Sera does not list
  is skipped with a warning (and a Telegram alert with `--telegram`) while the
  other pairs quote; rotor refuses to start only if Sera lists none of them.
- `price_source` is `wise`, `ecb`, `fed`, `bnm`, or `mas`. `wise` needs
  `WISE_API_TOKEN` and `fed` (the official FRED API) needs a free `FRED_API_KEY`;
  `ecb`, `bnm`, and `mas` are keyless.
- `wise` is amount-aware: it prices a Wise quote for the pair's `qty_base` and
  uses the **fee-inclusive effective rate** for the cheapest enabled payment
  method, so its benchmark already reflects Wise's conversion cost at that size.
  The other sources are pure mid/reference rates. Pin a Wise method with
  `WISE_PAY_IN`/`WISE_PAY_OUT` if you do not want cheapest-enabled.
- `fixed_bps` can be global or overridden per pair.
- `return_bps` must be positive. It is the closest a return order quotes to mid.
- `return_window_hours` must be positive (default 48).

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
produces a quote. Placement is isolated the same way: an order or VL batch that
Sera rejects (for example an ask whose base token the wallet does not hold) is
logged and skipped, and the tick's other orders still go out.

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
signing, Rotor builds every order itself and signs only that, never a struct a
server returns, and it refuses to start if `GET /config` names a contract other
than Sera's for mainnet or Sepolia. With
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
not trade history. It stores unresolved active orders, placed orders whose fills
are not yet counted (`unsettled_orders`), open positions (`positions`), and open
stale-reference alerts, and prunes rows once they resolve. Positions must survive
restarts: deleting the file forgets them. Set `ROTOR_STATE_PATH` to move the
file.

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
maker quote from a taker wallet between ticks, and verify that the fill settles
into a position and is quoted back as a return order sized to the fill. They
default to a `305` second wait between ticks so live cancellation cooldowns can
clear, and run four ticks so the filled order expires and settles.
