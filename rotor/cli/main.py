"""Command line entry point for Rotor's Sera market-maker commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_mm_vl(args) -> int:
    """Run the TOML-driven multi-pair VL market-maker loop."""
    if args.check_config:
        return _check_config(args.config)
    if not args.dry_run and not _unlock_wallet():
        return 1

    # Import lazily so config-only CLI operations stay lightweight.
    from rotor.mm.vl_runner import run as run_vl_mm

    return run_vl_mm(
        config_path=args.config,
        poll_seconds=args.poll,
        order_ttl_seconds=args.order_ttl,
        min_requote_seconds=args.min_requote_seconds,
        max_reference_age_s=args.max_reference_age,
        halt_on_stale_reference=args.halt_on_stale_reference,
        dry_run=args.dry_run,
        once=args.once,
        cancel_on_exit=args.cancel_on_exit,
        telegram=args.telegram,
        auto_update=args.auto_update,
        update_interval_seconds=args.auto_update_interval_seconds,
        log_level=args.log_level,
    )


def _check_config(config_path: str) -> int:
    """Load the runner and the config, and nothing else (auto-update's start check)."""
    try:
        from rotor.mm import vl_runner

        vl_runner.load_vl_config(config_path)
    except Exception as exc:
        print(f"config check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("config ok")
    return 0


def _unlock_wallet() -> bool:
    """Unlock the configured maker wallet and report operator-friendly errors."""
    # Import lazily to keep wallet dependencies out of parser construction.
    from rotor.security import key

    try:
        # unlock() is idempotent, so repeated checks are safe.
        key.unlock()
        print(
            f"wallet unlocked: address={key.wallet_address()}", file=sys.stderr)
        return True
    except key.WalletNotInitialized as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    except key.InvalidPrivateKey:
        # Avoid printing the bad private key value.
        print("error: invalid SERA_PRIVATE_KEY", file=sys.stderr)
        return False
    except Exception as exc:
        # Anything else: name the error type only; its message or traceback
        # could carry the key.
        print(f"error: could not unlock the wallet ({type(exc).__name__})", file=sys.stderr)
        return False


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level Rotor argparse command tree."""
    parser = argparse.ArgumentParser(
        prog="rotor",
        description="Basic Sera market-making tools",
    )
    top = parser.add_subparsers(dest="group", required=True)

    mm_g = top.add_parser("mm", help="Sera market maker")
    mm_sub = mm_g.add_subparsers(dest="cmd", required=True)

    p_mm_vl = mm_sub.add_parser(
        "vl",
        help="multi-pair MM loop with automatic Virtual Liquidity placement",
    )
    # Runtime controls stay in CLI flags; strategy details live in TOML config.
    p_mm_vl.add_argument("--config", default="rotor/vl.config.toml",
                         help="VL TOML config path (default rotor/vl.config.toml)")
    p_mm_vl.add_argument("--poll", type=float, default=305.0,
                         help="poll interval in seconds (default 305)")
    p_mm_vl.add_argument("--order-ttl", type=int, default=600,
                         help="order expiration seconds from placement (default 600)")
    p_mm_vl.add_argument("--min-requote-seconds", type=float, default=300.0,
                         help="local guard before cancelling fresh orders (default 300)")
    p_mm_vl.add_argument("--max-reference-age", type=float, default=None,
                         help="override per-source max reference age in seconds "
                              "(default: each source's own window — wise ~10m, "
                              "daily FX sources ~48h)")
    p_mm_vl.add_argument("--halt-on-stale-reference", action="store_true",
                         help="use Wise's live mid as soon as a pair's reference is past "
                              "its freshness window (default: carry a daily FX source's "
                              "last published rate for up to 7 days, with a warning)")
    p_mm_vl.add_argument("--dry-run", action="store_true",
                         help="print intended VL groups; do not sign or place orders")
    p_mm_vl.add_argument("--once", action="store_true",
                         help="run one tick and exit")
    p_mm_vl.add_argument("--cancel-on-exit", action="store_true",
                         help="try to cancel tracked live orders during shutdown")
    p_mm_vl.add_argument("--telegram", action="store_true",
                         help="send lifecycle/error alerts to Telegram (needs TELEGRAM_* config)")
    p_mm_vl.add_argument("--auto-update", action="store_true",
                         help="check git once per day and pull updates safely")
    p_mm_vl.add_argument("--auto-update-interval-seconds", type=float, default=86400.0,
                         help="seconds between auto-update checks (default 86400)")
    p_mm_vl.add_argument("--log-level", default="WARNING",
                         help="logging level (default WARNING)")
    p_mm_vl.add_argument("--check-config", action="store_true",
                         help="load the runner and the config, then exit 0 if both are "
                              "fine (no network, wallet, or state); auto-update runs this "
                              "on new code before restarting into it")
    p_mm_vl.set_defaults(func=cmd_mm_vl)

    return parser


LEGACY_SECRET_FILE = Path(__file__).resolve().parents[1] / "secret.py"


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args and run the selected command handler."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # rotor/secret.py was a config fallback and is no longer read. Stop rather
    # than run without the settings an operator kept there.
    if LEGACY_SECRET_FILE.exists():
        print(
            f"{LEGACY_SECRET_FILE} is no longer read: move its settings to the "
            "environment or the root .env (see .env.example), then delete it.",
            file=sys.stderr,
        )
        return 2
    # Subparsers attach `func`; required=True guarantees it exists.
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
