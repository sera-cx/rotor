# rotor

Small Sera Virtual Liquidity market maker.

Rotor runs one autonomous command: `rotor mm vl`. On each cadence tick it fetches
the latest source mid for each configured market, builds fixed-bps bid/ask quotes,
cancels the previous tracked quote set when safe, and places the fresh set through
Sera VL where possible. Inventory a fill moves becomes a position that rotor quotes
back on the opposite side: the return order walks from the normal quote to the
fill price over 48 hours (a reverse Dutch auction), then holds at the fill price
until it fills. See [docs/vl-mm.md](docs/vl-mm.md#returning-fills).

## Install

Rotor needs Poetry 2 or newer (Poetry 1.x cannot read its `pyproject.toml`);
check with `poetry --version`, and upgrade with `pipx install 'poetry>=2'`.

```bash
poetry install
```

## Configure

Environment variables are preferred:

```bash
export SERA_BASE_URL="https://api.testnet.sera.cx/api/v1"  # must be https
export SERA_API_KEY="..."
export SERA_API_SECRET="..."
export SERA_PRIVATE_KEY="0x..."
export WISE_API_TOKEN="..."   # optional: raises Wise rate limits for price_source = wise
export FRED_API_KEY="..."     # for price_source = fed (free FRED API key)
```

`SERA_BASE_URL` must use `https://` (the maker signs orders against it);
`http://` is rejected outside `localhost`.

For file-based local config, copy `.env.example` to the gitignored root `.env`
and fill in real values. `rotor/secret.example.py` remains available only as a
Python fallback template for `rotor/secret.py`. Real environment variables
override `.env`, and `.env` overrides `rotor/secret.py`.

## VL Config

```toml
# Global settings go before the first [[...]] table: TOML puts any key that
# follows a table header inside that table.
fixed_bps = "50"
return_bps = "2"

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

Supported FX sources are `wise`, `ecb`, `fed`, and `bnm`. `wise`, `ecb`, and
`bnm` work without a key (`WISE_API_TOKEN` is optional and only
raises Wise's rate limits); `fed` (the official FRED API) needs a free
`FRED_API_KEY`. A pair's source sets its mid: the central-bank sources publish
daily reference rates, and `wise` (for currencies none of them covers) uses the
midpoint of Wise's after-fee sell and buy rates for the pair's size.

Every pair's normal quotes, whatever its source, are placed at Wise's after-fee
prices for each order's own size: a bid at what converting that much of the base
token through Wise pays, an ask at what buying it through Wise costs, by the
cheapest enabled transfer method that does not involve a Wise balance. Wise's
rates are live, so quotes follow the live market. The mid and `fixed_bps` shape
the return orders that bring a position back after a fill.

## Run

```bash
poetry run rotor mm vl --config rotor/vl.config.toml --dry-run --once
poetry run rotor mm vl --config rotor/vl.config.toml --poll 305
```

Run one rotor per wallet: rotor refuses to start if the wallet has open orders
on its markets that it did not place. Live mode always cancels its own
reattached orders on boot. If Sera's cancel
cooldown is active, Rotor waits 5 minutes and retries before quoting.

Rotor keeps a small state database at `.rotor_state.sqlite` by default. It
stores active orders, placed orders whose fills are not yet counted, open
positions, and open stale-reference alerts, and prunes rows once they resolve.
Positions are the part that must survive: deleting the file forgets them, and
rotor goes back to quoting both sides normally. Set `ROTOR_STATE_PATH` to move it.
The file belongs to the wallet and network that first used it, and rotor refuses
to start on it with any other, so use one file per wallet and network (for
example `.rotor_state.testnet.sqlite` and `.rotor_state.mainnet.sqlite`).

Add `--telegram` to send lifecycle/error alerts to Telegram. It needs no extra
package; without a token and chat id, sends degrade to dry-run logs.

Daily FX sources (`ecb`, `fed`, `bnm`) publish nothing on weekends or
public holidays. Instead of going dark until they publish again, Rotor keeps
quoting on a source's last published rate for up to 7 days past that rate's
date. Past that limit, or whenever a source is down, the pair's mid is
replaced by Wise's live mid-market rate and it keeps quoting (quotes are at
Wise's prices either way; the mid only bounds return orders). With `--telegram`
it warns when pairs start quoting on carried rates, repeats the warning quietly
every 6 hours, says when a pair switches to Wise's mid, and reports when fresh
rates return. `wise` never carries a stale quote. Pass
`--halt-on-stale-reference` to use Wise's mid as soon as a rate is stale
instead of carrying it, or change a source's limit with
`<SOURCE>_MAX_CARRY_AGE_S`.

For autonomous source checkouts, add `--auto-update`. Rotor checks once per day
and runs `git pull --ff-only` unless tracked files were changed (untracked files
such as your config do not block it). It restarts into new code only after that
code passes `--check-config` with the same command; otherwise it keeps running
and alerts. A checkout from before this check needs one manual `git pull`.

Logs are intentionally small for lightweight servers. The default log level is
`WARNING`; per-tick quote and placement traces require `--log-level DEBUG`.

## Development

```bash
poetry run ruff check rotor tests
poetry run python -m pytest -q
```

Opt-in real Sera testnet checks are skipped by default:

```bash
ROTOR_RUN_TESTNET=1 poetry run python -m pytest -q tests/testnet
```

Opt-in live FX source checks (keyless `ecb`/`bnm`/`wise`; `fed` runs when its
key is set) are also skipped by default:

```bash
make live-prices   # or: ROTOR_RUN_LIVE_PRICES=1 poetry run python -m pytest -q tests/live
```

The two-wallet maker/taker E2E and the heavier live runner scenarios are
separately gated because they place real testnet orders. See
[tests/testnet/README.md](tests/testnet/README.md).

## Deploy on Manus

To run on [Manus](https://manus.im): register the secrets from
[`.env.manus.example`](.env.manus.example) (exact names) in the Manus secret
store, and schedule the fire-once command (`./scripts/manus_run_once.sh`, i.e.
`rotor mm vl --once`) on a ≥5-minute cadence. Each fire reattaches from Sera,
requotes, and exits without blocking on cancel cooldowns. See
[docs/manus.md](docs/manus.md).

See [docs/vl-mm.md](docs/vl-mm.md) for operator details.
