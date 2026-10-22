# Running Rotor on Manus

This guide covers deploying `rotor mm vl` on [Manus](https://manus.im) using its
secret store and scheduled tasks.

## Secrets — exact, no guessing

Manus stores the values you add to its secret store and injects them into the
sandbox as **environment variables under the exact name you register**. Rotor
reads config environment-first (`get_config`: env → `.env` → `rotor/secret.py` →
default), so it consumes them directly.

Register each secret using the **exact** name from
[`.env.manus.example`](../.env.manus.example) — copy the names verbatim. The
required set for a live deployment is:

| Secret name | Purpose |
| --- | --- |
| `SERA_BASE_URL` | Sera API base (must be `https://`) |
| `SERA_API_KEY` | Sera API key |
| `SERA_API_SECRET` | Sera API secret |
| `SERA_PRIVATE_KEY` | maker signing key (`0x…`) — most sensitive |
| `WISE_API_TOKEN` | (optional) Wise quotes API token; raises rate limits |
| `ROTOR_STATE_PATH` | (recommended) unresolved-state DB path; one per wallet and network |

Add `FRED_API_KEY` only if a pair uses `price_source = fed`, and `TELEGRAM_*`
only with `--telegram`.

**If Manus injects under a namespace prefix.** If your stored secrets arrive in
the sandbox as `<PREFIX>SERA_API_KEY` rather than bare `SERA_API_KEY`, set one
variable — `ROTOR_ENV_PREFIX=<PREFIX>` — and rotor resolves every name through
that prefix automatically. Leave it unset when names are injected bare (the
common case).

**Verify in the sandbox** once secrets are set:

```bash
poetry run python -c "from rotor.config import get_config as g; [print(n, 'OK' if g(n) else 'MISSING') for n in ['SERA_BASE_URL','SERA_API_KEY','SERA_API_SECRET','SERA_PRIVATE_KEY']]"
```

## Run mode — scheduled fire-once

Manus sandboxes are oriented around scheduled task sessions rather than a 24/7
foreground daemon, so the robust fit is **fire-once on a schedule**, not a
long-running loop. Each invocation:

1. reattaches open orders for the configured markets from Sera,
2. cancels the stale set (single pass — it never blocks on Sera's cancel
   cooldown in `--once` mode),
3. places a fresh quote set around the latest price,
4. exits (`0` placed/ok, non-zero = could not requote this run — the next fire
   retries).

Startup failures share an hourly budget across fires: at most 3 fires per clock
hour try to start, and after the third failure the rest of that hour's fires
exit 1 at once without contacting Sera. With `--telegram`, the hour's first and
third failed startup alert, and so does the first start that succeeds after them.
A fire that places nothing alerts once no fire has placed for 15 minutes (at
most once per 15 minutes), and the next fire that places reports that quoting
resumed.

Keep `.rotor_state.sqlite` on persistent storage. Open orders are recovered from
Sera on each fire, but the file is the only record of positions held for return
and of fills still to be counted (and of the startup budget); losing it forgets
them, and rotor goes back to quoting both sides normally.

### The scheduled command

Point the Manus scheduled task at:

```bash
./scripts/manus_run_once.sh          # = rotor mm vl --config rotor/vl.config.toml --once --order-ttl 600
# or directly:
poetry run rotor mm vl --config rotor/vl.config.toml --once --order-ttl 600
```

The wrapper honors `ROTOR_CONFIG`, `ROTOR_ORDER_TTL`, and `ROTOR_LOG_LEVEL`.

### Cadence guidance

- **Interval ≥ ~5 minutes.** Sera applies a per-order cancel cooldown (~5 min)
  and rotor's local requote guard defaults to 300 s, so firing more often than
  that just yields no-op runs that wait for the cooldown.
- **`--order-ttl` ≥ the interval (plus margin).** Quotes carry a signed
  expiration; with a 5-minute schedule, the default `600` keeps quotes alive
  across a tick (and tolerates one missed fire).
- **Do not pass `--cancel-on-exit`.** Quotes should rest between fires; the next
  run reattaches and replaces them.

## Other notes

- **One instance per wallet.** Rotor takes an advisory lock on
  `<state>.lock`; do not let scheduled runs overlap against the same wallet.
- **Egress.** The sandbox needs outbound HTTPS to Sera and your price source
  (Wise/ECB/BNM/FRED-API). The legacy FRED scrape endpoint is bot-walled,
  which is why `fed` uses the FRED API.
- **`SERA_BASE_URL` must be `https://`** (rotor refuses a plain-http endpoint
  outside `localhost`).
- **Auto-update.** `--auto-update` is independent of the schedule; leave it off
  unless you specifically want each sandbox to `git pull` before quoting.
