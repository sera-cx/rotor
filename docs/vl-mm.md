# Virtual Liquidity Market Maker

`rotor mm vl` is a cadence-based Sera VL runner that quotes at Wise's after-fee
prices. Each tick:

1. Reads the fills of its live quotes and of every placed order not yet settled.
   Fully filled orders stop being tracked, and positions are brought up to date.
2. Fetches the latest source mid for every configured pair.
3. Places one bid at Wise's after-fee rate for converting that order's size of
   the base token into the quote token, and one ask at Wise's after-fee cost of
   buying that size. A market with an open position quotes its return order on
   that side instead.
4. Cancels tracked old placements when the local cancel guard permits.
5. Places the fresh quote set, using VL for eligible same-spent-token groups,
   recording every order for settlement before it is sent.

## Returning fills

A fill moves inventory: a filled bid leaves rotor holding extra base tokens, a
filled ask leaves it short of them. Rotor records that as a per-market
**position** (net base quantity, size-weighted fill price, size-weighted fill
time) in `rotor/mm/positions.py` and quotes it back on the opposite side:

- The return order replaces the normal order on that side. It starts
  `2 x fixed_bps` from the fill price and
  walks linearly to the fill price over `return_window_hours` (default 48): a
  reverse Dutch auction that asks for less profit the longer it waits.
- After the window it **holds at the fill price indefinitely**. Rotor never
  returns a position at a loss against its fill price. Breakeven is measured on
  price alone; any fees or gas a deployment charges are not included.
- Unlike normal quotes, it is not priced at Wise: it can start outside Wise's
  after-fee price and walks back toward the fill price, so the position can
  return to neutral.
- It never quotes closer to the latest mid than `return_bps`. If the market moved
  in rotor's favour, the order sits `return_bps` from mid instead of overpaying.
- It never spends more than the normal order it replaces, so a larger position is
  returned over several fills and budgets see no new spend. A position below the
  market's minimum order, or under a tenth of the normal order on that side
  (`RETURN_MIN_SHARE`), keeps the full normal order there instead of shrinking
  the side to a dust-sized return order; the next fill there
  absorbs it.
- The other side keeps quoting normally. Fills there grow the position, and its
  fill price and clock become size-weighted averages. If the market moves away
  from a daily mid far enough that the other side's normal order would cross
  the return order, that normal order is left out for the tick: Sera would
  refuse the self-trade, and with it any VL batch the order belongs to.

Fills are counted from a ledger, not from the resting quote set. Every order
is recorded with its side, limit price, and signed expiry once rotor has signed
it and before it is sent, and each tick rotor reads the fills of every order in
the ledger. Rotor picks and signs the order ids itself, so Sera books an order
under the recorded id or not at all. If Sera refuses the order (a 4xx answer),
its row is removed. If the answer is lost (a timeout, a dropped connection, a
server error, a reply rotor cannot read), the row stays, so a fill on an order
Sera did book is still counted; an order Sera never booked has no fills and
settles at zero. Rotor also tracks such a placement as live, so the next cycle
cancels it before quoting; a cancel Sera answers with "not found" drops it. Once an order has
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
orders, or orders placed by an older rotor version. A position on a market that is
no longer configured is kept but not quoted. Deleting the state file forgets
every position.

### Stablecoin pegs

Sera treats every stablecoin as worth exactly one unit of its currency, and
rotor prices it the same way: each token is quoted at its currency's reference
rate (AUDD at AUD, USDT at USD), never at its own market price, which rotor
does not read. If a stablecoin loses its peg, rotor keeps bidding for it at the
currency's rate, so sellers can sell it to rotor above its market price; the
position that leaves is held at its fill price until the peg returns, and is
not recovered if the token never re-pegs. Budgets cap what each tick can spend.

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
market = "JPYC/USDC"
price_source = "ecb"
qty_base = "300000"
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
- Sera lists each pair in one orientation, and it can differ between networks
  (Sepolia lists `USDC/EURC` and `CADC/USDC`; mainnet lists `EURC/USDC` and
  `USDC/CADC`); use the symbol that network's `GET /markets` returns. A configured market Sera does not list
  is skipped with a warning (and a Telegram alert with `--telegram`) while the
  other pairs quote; rotor refuses to start only if Sera lists none of them.
  A running rotor re-checks this hourly (see [Sera Changes While
  Running](#sera-changes-while-running)).
- `price_source` is `wise` (the default), `ecb`, `fed`, or `bnm` (`mas` was removed: MAS
  publishes no public daily rate API; ECB and BNM both publish SGD). `fed` (the official
  FRED API) needs a free `FRED_API_KEY`; the others work without a key.
  `WISE_API_TOKEN` is optional: Wise prices anonymous quotes too, and a token
  only raises its rate limits.
- A pair's source sets its mid. `ecb`, `fed`, and `bnm` publish daily
  reference rates; `wise` (for currencies none of them covers) uses Wise's
  mid-market rate. When Wise cannot convert from a currency (it sends to CNH,
  COP, NGN, ZAR and AED but not from them), that rate is read from the other
  direction's reply. ARS and PEN have no Wise route at all and no central-bank
  source in rotor, so they cannot be quoted.
- Every pair's normal quotes are placed at Wise's after-fee prices for each
  order's own final size (after budget and balance trimming): a bid at what
  converting that much of the base token through Wise pays, an ask at what
  buying it through Wise costs, by the cheapest enabled transfer method that
  does not involve a Wise balance. A taker on Sera gets what converting through
  Wise would give. Wise's fees fall with size: about 25-80 bps each way on majors
  at a few thousand dollars. Wise's rates are live, so quotes follow the live
  market whatever the daily mid says.
- Where Wise cannot price one side (it offers no such route, it does not
  answer, or its rate is older than 10 minutes; a reply up to that old is
  reused), that side sits around the mid instead: `fallback_bps` (default 50)
  below it for a bid or above it for an ask on weekdays, and the wider
  `fallback_weekend_bps` (default 200) during the FX weekend (Friday 21:00 to
  Sunday 21:00 UTC), on a carried mid, or while Wise is unreachable. Both can be
  set globally or per pair, and `fallback_weekend_bps` must be at least
  `fallback_bps`. The other side keeps Wise's price if Wise can give it.
- The mid sets how close a return order may come (`return_bps`) and how far its
  auction starts; normal quotes do not use it.
- `fixed_bps` (global or per pair) sets where a return order starts after a
  fill: `2 x fixed_bps` from the fill price. It does not move normal quotes.
- `return_bps` is the closest a return order quotes to mid. Every spread must
  be more than 0 and less than 10000.
- Unknown keys are refused, at the top level and in `[[pair]]`: a typo, or a
  global setting written after a table header, would otherwise be ignored.
- Every `[[budget]]` token must be traded by a configured market; a typo there
  is rejected instead of silently leaving that token uncapped.
- `--order-ttl` must be at least 120 seconds: Sera sweeps orders due to expire
  within 60 seconds every 30 seconds.
- Price-source URLs (`WISE_BASE_URL`, `ECB_RATES_URL`, `FRED_API_BASE_URL`,
  `BNM_EXCHANGE_RATE_BASE_URL`) must be `https://`, like `SERA_BASE_URL`.
- `return_window_hours` must be positive (default 48).

See `rotor/price_reference/README.md` for provider-specific endpoint and
cross-rate details.

## Price Freshness

Each source observation is stamped with the provider's published timestamp and
checked against a per-source freshness window: 10 minutes for `wise` (intraday)
and 48 hours for the daily references (`ecb`, `fed`, `bnm`). Override a
single daily source with `<SOURCE>_MAX_AGE_S`, all of them with
`PRICE_REFERENCE_MAX_AGE_S`, or at runtime with `--max-reference-age`. Wise's
window is fixed: it prices every normal order, so no override loosens it.

Daily references publish nothing on weekends or public holidays. When a daily
rate goes stale, the pair uses Wise's live mid instead, with no alert: that is
the normal weekend. Only if Wise has no mid either is the last published rate
carried forward, until it is 7 days old (`<SOURCE>_MAX_CARRY_AGE_S` or
`PRICE_REFERENCE_MAX_CARRY_AGE_S` to change; neither applies to `wise`). With
`--telegram`, Rotor warns once on the important channel when pairs start
quoting on carried rates, repeats the warning on the normal channel every 6
hours, and reports when fresh rates return. A tick where Wise's mid covers a
carried rate leaves that episode open, so a Wise blip on a weekend does not
repeat the warning. These alert episodes are kept in the state database, so
`--once` runs do not repeat them. A stale `wise` quote is never carried.

A pair whose own source fails outright (down, or a rate past its carry limit)
keeps quoting with its mid replaced by Wise's live mid-market rate, read from the same
Wise reply as its quotes, with one important alert when it switches. Quotes are
at Wise's prices either way; the mid only bounds return orders. Pass
`--halt-on-stale-reference` to switch to Wise's mid as soon as a rate is stale
instead of carrying it.

Pairs are isolated within a tick: if a pair has no mid at all (its source and
Wise both fail), or its quote fails market minimums, only that pair is skipped —
the other configured pairs still quote. A tick is fully skipped only when no pair
produces a quote. With `--telegram`, a pair that has produced no quote for 15
minutes in a row is alerted once with the reason (for example its price source
failing), and again once when it quotes again. Wise being unreachable for 15
minutes, so normal orders sit at the mid ± `fallback_weekend_bps`, is alerted
once the same way, with a notice when it answers again. These episodes are kept
in the state file, so `--once` fires share them. Placement is isolated the same way: an order or VL batch that
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

## Startup

Startup loads the config, bootstraps Sera, and builds the pairs. A temporary
failure (a network error, a Sera server error or rate limit, or Sera reporting
it is not ready) is retried after 30 and then 60 seconds. At most 3 startup
attempts run per clock hour; after the third failure rotor waits for the next
hour (a `--once` fire just exits). The count is kept in `<state>.boot.json`, so
restarts and scheduled fires share it. Any other failure (a bad config, a wrong
contract or token, no quotable pair, a locked state file) stops rotor at once.
With `--telegram`, the hour's first and third failure and the start that
follows them each send an alert.

## Sera Changes While Running

A long-running rotor re-reads Sera's metadata (`GET /config`, `/health`,
`/markets`) every hour, with the same contract and token checks as startup, and
rebuilds its pairs and VL batch limits from it. A pair Sera no longer lists stops
quoting (with an alert); a newly listed configured pair starts. A refresh is all
or nothing: if any read or check fails, rotor keeps quoting on the last good
metadata, retries every 10 minutes, and with `--telegram` alerts once when
refreshes start failing and once when they recover.

Between refreshes:

- When Sera rejects an order or a VL batch leg because its pair is not active,
  rotor re-sends the rest of the batch without that leg in the same cycle
  (standalone if fewer legs than Sera's batch minimum remain), then stops
  quoting that pair until the next refresh. With `--telegram` it alerts once
  per pair.
- A rejection that suggests rotor's metadata is out of date (HTTP 422, or an
  amount below Sera's minimum) brings the next refresh forward to 10 minutes
  after the last one.

`--once` runs bootstrap fresh every fire, so they need none of this.

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
against it. Use `--cancel-on-exit` when shutdown should cancel tracked live
placements. Sera refuses to cancel an order in its first 5 minutes, so shutdown
then waits until the youngest order is old enough and cancels it (up to about 5
minutes; stop a second time to skip the wait). A stop that arrives mid-tick
places no fresh set. Anything still live is logged and alerted with when it
expires, and the loop exits with code 1.

Only one instance may use a given state file at a time: Rotor takes an exclusive
lock on `<state>.lock` at startup and refuses to start if another process holds
it (set `ROTOR_STATE_PATH` to run a second instance on different state). Before
signing, Rotor builds every order itself and signs only that, never a struct a
server returns, and it refuses to start if `GET /config` names a contract other
than Sera's for mainnet or Sepolia, or if `GET /markets` gives a mainnet token
an address or decimals other than the ones rotor pins. With
`--telegram`, an alert fires if quoting stalls (no successful placement for 3
cadences, at least 15 minutes) and repeats at most once per that window, so a
silent stall is not masked by the hourly heartbeat; the first placement after it
reports that quoting resumed. The stall clock is kept in the state file, so
`--once` fires share it: a fire that places nothing alerts once the stall has
lasted that long across fires.

On startup, Rotor reattaches the open orders on its configured markets that it
placed (those in its state file or settlement ledger). Run one rotor per wallet:
an open order on those markets that rotor did not place means another rotor, or
someone else, is trading the wallet, so rotor refuses to start (with an
important alert) and never touches that order. If one appears while rotor runs,
it alerts once and leaves it alone.
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

The file belongs to one wallet on one Sera deployment. The first live run
records its wallet address, chain id, and Sera contract in it, and a later run
with any other refuses to start (with a Telegram alert under `--telegram`)
rather than quote one network's positions on another. Keep one file per wallet
and network, for example `ROTOR_STATE_PATH=.rotor_state.testnet.sqlite` while
testing and `.rotor_state.mainnet.sqlite` for real funds. A file written before
rotor recorded this belongs to the next live run that opens it.

Add `--telegram` to send lifecycle/error alerts to Telegram. Messages go to the
Bot API over httpx, so no extra package is needed. If `TELEGRAM_TOKEN` or both
chat ids are empty, sends degrade to dry-run logs (with a warning), and a failed
send never crashes the trading loop. Alerts longer than Telegram's limit are
cut, not dropped.

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
`git pull --ff-only` only on a branch with no changed tracked files; untracked
files (your config, state files, `.env`) do not block it. A skipped or failed
check is logged as a warning and, with `--telegram`, sent as an important alert.

Before restarting into new code, Rotor runs it once with the same command plus
`--check-config`, which loads the runner and your config and touches no network,
wallet, or state. Only if that passes does Rotor stop the loop, run normal
shutdown cleanup, and re-exec the same command (with a Telegram notice). If it
fails (a new dependency needing `poetry install`, or a config the new version
rejects), Rotor keeps running the old version, alerts with the reason, and
checks again at the next interval. `--auto-update-interval-seconds` exists for
testing/custom deployments.

A deployment still on a version from before this check (GitHub `public` at
517d80f or earlier) sees its own untracked config as local changes and never
pulls: update it once by hand with `git pull --ff-only`.

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

Those scenarios run the actual tick loop with a deterministic test mid and a
stand-in Wise fee (`ROTOR_TESTNET_WISE_FEE_BPS`, default 20), fill a maker quote
from a taker wallet between ticks, and verify that the fill settles into a
position and that every later order on the return side is the return order:
sized to the position, at the return auction's price for when it was placed
(within one price step), never through the fill price. They default to a `305`
second wait between ticks so live cancellation cooldowns can clear, and run
three ticks so the filled order expires and settles. With `--cancel-on-exit`
the run then waits out Sera's cancel cooldown before exiting.
