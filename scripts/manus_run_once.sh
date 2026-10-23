#!/usr/bin/env bash
# Fire-once Rotor invocation for a Manus scheduled task.
#
# Manus runs this in the sandbox on its schedule with the secret store injected
# as environment variables. Each run reattaches open orders from Sera, cancels
# the stale set, places a fresh quote set, and exits — so it is safe to fire
# repeatedly. It never blocks on Sera's cancel cooldown.
#
# Set the schedule interval >= Sera's cancel cooldown (~5 min) so each run can
# cancel-and-requote, and keep --order-ttl >= the interval so quotes do not
# expire between runs. Do NOT pass --cancel-on-exit: quotes should rest between
# fires and the next run replaces them.
#
# Exit code 0 = a fresh quote set was placed (or nothing needed placing);
# non-zero = this run could not requote (e.g. cooldown / transient error) and
# the next scheduled fire should retry.
set -euo pipefail

# Run from the repo root wherever the scheduler starts us: poetry finds the
# project there, and the default config and state paths are relative to it.
cd "$(dirname "$0")/.."

CONFIG="${ROTOR_CONFIG:-rotor/vl.config.toml}"
ORDER_TTL="${ROTOR_ORDER_TTL:-600}"
LOG_LEVEL="${ROTOR_LOG_LEVEL:-WARNING}"

exec poetry run rotor mm vl \
  --config "$CONFIG" \
  --once \
  --order-ttl "$ORDER_TTL" \
  --log-level "$LOG_LEVEL" \
  "$@"
