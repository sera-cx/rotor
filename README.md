# rotor

Small Sera Virtual Liquidity market maker.

Rotor runs one autonomous command: `rotor mm vl`. On each cadence tick it quotes
a bid and an ask on each configured market at Wise's after-fee price for that
order's size, cancels the previous tracked quote set when safe, and places the
fresh set through Sera VL where possible. Inventory a fill moves becomes a
position that rotor quotes back on the opposite side: the return order starts
`2 x fixed_bps` from the fill price and walks to it over 48 hours (a reverse
Dutch auction), then holds at the fill price until it fills. See [docs/vl-mm.md](docs/vl-mm.md#returning-fills).

## Install

Rotor needs Python 3.11 or newer and Poetry 2 or newer (Poetry 1.x cannot read
its `pyproject.toml`); check with `python3 --version` and `poetry --version`, and
upgrade Poetry with `pipx install 'poetry>=2'`.

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
export WISE_API_TOKEN="..."   # optional: only raises Wise's rate limits
```

`SERA_BASE_URL` must use `https://` (the maker signs orders against it);
`http://` is rejected outside `localhost`.

For file-based local config, copy `.env.example` to the gitignored root `.env`
and fill in real values. Real environment variables override `.env`; within
`.env`, a later line for the same name wins, as in a shell.

## VL Config

```toml
# Global settings go before the first [[...]] table: TOML puts any key that
# follows a table header inside that table.
fixed_bps = "50"
return_bps = "2"
fallback_bps = "50"
fallback_weekend_bps = "200"

[[budget]]
token = "USDC"
amount = "5000"

[[pair]]
market = "XSGD/USDC"
price_source = "wise"
qty_base = "2000"

[[pair]]
market = "JPYC/USDC"
price_source = ["ecb", "bnm"]
qty_base = "300000"
fixed_bps = "40"
```

Supported FX sources are `wise`, `ecb`, and `bnm`, and none needs a key. A
pair's source, or its list of sources tried in order, sets its mid, but every pair's normal quotes sit at Wise's
after-fee prices for each order's size: the mid and `fixed_bps` shape the return
orders, and the mid anchors a side Wise cannot price.
[docs/vl-mm.md](docs/vl-mm.md#config) describes every setting, and
[Price Freshness](docs/vl-mm.md#price-freshness) what happens on weekends and
when a source is down.

## Run

```bash
poetry run rotor mm vl --config rotor/vl.config.toml --dry-run --once
poetry run rotor mm vl --config rotor/vl.config.toml --poll 305
```

Run one rotor per wallet: rotor refuses to start if the wallet has open orders
on its markets that it did not place.

Rotor keeps open positions and other unresolved state in a small database
(`.rotor_state.sqlite`, or `ROTOR_STATE_PATH`). Deleting it forgets positions, and a file belongs to one wallet
and network, so use one file per wallet and network; see
[Unresolved State](docs/vl-mm.md#unresolved-state).

Add `--telegram` for lifecycle and error alerts, `--auto-update` to pull and
restart into new code once a day
([Autonomous Updates](docs/vl-mm.md#autonomous-updates)), and
`--log-level DEBUG` for per-tick traces (the default level is `WARNING`).

## Development

```bash
poetry run ruff check rotor tests
poetry run python -m pytest -q
```

Opt-in live FX source checks and real Sera testnet checks are skipped by
default; see [tests/README.md](tests/README.md) and
[tests/testnet/README.md](tests/testnet/README.md).

## Deploy on Manus

To run on [Manus](https://manus.im): register the secrets from
[`.env.manus.example`](.env.manus.example) (exact names) in the Manus secret
store, and schedule the fire-once command (`./scripts/manus_run_once.sh`, i.e.
`rotor mm vl --once`) on a ≥5-minute cadence. Each fire reattaches from Sera,
requotes, and exits without blocking on cancel cooldowns. See
[docs/manus.md](docs/manus.md).

See [docs/vl-mm.md](docs/vl-mm.md) for operator details.
