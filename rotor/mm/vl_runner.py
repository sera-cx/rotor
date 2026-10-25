"""Multi-pair VL placement runner for Rotor market making.

Each tick, every pair builds a bid and an ask priced at Wise's after-fee rates;
on the side that returns an open position, a return order takes the normal
one's place. The order intents are then grouped by spent token: a group with at
least Sera's VL batch minimum of legs goes out as Virtual Liquidity batches, a
smaller one as standalone orders.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import httpx

try:  # pragma: no cover - POSIX-only single-instance lock.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback (no flock).
    fcntl = None

from rotor.config import get_config
from rotor.mm.exchange import errors
from rotor.mm.exchange.client import (
    OrderRecord,
    SeraClient,
    SignedOrder,
    VLBatchResult,
    VLLeg,
)
from rotor.mm.grid import BPS, quantize_price, quantize_qty, validate_minimums
from rotor.mm.positions import Position, apply_fill, return_price
from rotor.mm.quote_engine import (
    LiveOrder,
    StopFlag,
    all_requote_safe,
    build_client,
    fmt,
    sleep_remaining,
)
from rotor.mm.vl_config import PairConfig, VLConfig, load_vl_config
from rotor.notifications.telegram import TelegramNotifier
from rotor.price_reference import (
    ReferencePrice,
    ReferencePriceOracle,
    ReferenceUnavailable,
    load_price_reference_source,
    load_token_to_iso,
    market_to_iso_pair,
)
from rotor.price_reference.sources import WiseNoRoute, parse_ts
from rotor.updater import (
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DailyGitUpdater,
    restart_current_process,
)

# Sera refuses to cancel an order younger than this (HTTP 429 cancel cooldown).
SERA_CANCEL_COOLDOWN_SECONDS = 300.0
# Sera sweeps, every 30 s, orders due to expire within 60 s, so a shorter TTL
# leaves an order almost no time on the book.
MIN_ORDER_TTL_SECONDS = 120
# An order whose fills are still pending/confirming this long after it expired
# is reported once: settlement looks stuck, and its fill is not yet stored.
STUCK_SETTLEMENT_SECONDS = 3600.0
# After cancelling, how often (and how long apart) rotor checks that the old
# orders are gone before placing new ones: about 5 s at most.
CANCEL_SETTLE_POLLS = 10
CANCEL_SETTLE_INTERVAL_SECONDS = 0.5
# A position smaller than this share of the normal order on its return side
# keeps that full normal quote rather than shrinking the side to a dust-sized
# return order (which can rest for days at its fill price).
RETURN_MIN_SHARE = Decimal("0.1")
DEFAULT_STATE_PATH = ".rotor_state.sqlite"
# While pairs quote on carried-forward reference rates, repeat the warning at
# most this often, so a weekend produces a handful of messages, not one per tick.
STALE_REMINDER_SECONDS = 6 * 60 * 60.0
# A pair that produces no quote this long while others may still quote (its
# price source failing, say) is alerted once; quoting again is reported.
DARK_PAIR_ALERT_SECONDS = 15 * 60.0
# DarkPairAlerts' episode key for Wise being unreachable (not a market name).
WISE_OUTAGE = "*wise*"
# An order's fills are counted this long after it expires, so a match made just
# before expiry has time to show up in its fill list.
SETTLE_GRACE_SECONDS = 60.0
# An order whose fills cannot be read this many ticks in a row (about an hour at
# the default cadence) is reported as an error instead of only a warning.
READ_FAILURE_ALERT_TICKS = 12
# When the cancel-cooldown guard clears within this many seconds, the tick waits
# for it instead of being skipped.
MAX_GUARD_WAIT_SECONDS = 60.0
# Startup attempts allowed per clock hour; after that rotor waits for the next
# hour. The count is kept beside the state file so restarts share it.
BOOT_ATTEMPTS_PER_HOUR = 3
# Seconds to wait before the 2nd and 3rd startup attempt within an hour.
BOOT_RETRY_WAITS = (30.0, 60.0)
# A running rotor re-reads Sera's metadata (config, health, markets) this often,
# and no sooner than EARLY_REFRESH_SECONDS after the last read when a rejection
# suggests it went stale. Markets Sera rejects as inactive sit out until then.
METADATA_REFRESH_SECONDS = 3600.0
# What client.bootstrap() replaces; put back if a refresh leaves no pair to quote.
CLIENT_METADATA = (
    "eip712_domain", "vl_batch_min", "vl_batch_max", "executor_id", "markets_by_symbol")
EARLY_REFRESH_SECONDS = 600.0
# How Sera names an inactive pair when it rejects a VL batch.
_INACTIVE_PAIR = re.compile(r"Pair (\S+) is not an active trading pair")


@dataclass(frozen=True)
class PairRuntime:
    """Runtime state for one configured market."""

    config: PairConfig
    # Sera market metadata from the bootstrapped client.
    info: object
    oracle: ReferencePriceOracle
    # ISO currency symbols used by the reference oracle.
    iso_base: str
    iso_quote: str
    # Wise, at whose after-fee rates every pair's normal orders sit (return
    # orders are exempt).
    settlement: object = None


@dataclass(frozen=True)
class OrderIntent:
    """One order rotor means to place this tick."""

    market: str
    side: str
    # Base-token order size.
    qty_base: Decimal
    # Limit price in quote-per-base terms.
    price: Decimal
    # Unix-seconds order expiry.
    expiration: int
    # Token spent by this side; used for VL grouping and budget checks.
    spent_token: str
    # A return order (priced by the return auction) rather than a normal quote.
    is_return: bool = False


@dataclass
class LivePlacement:
    """Local state for one standalone order or VL batch."""

    # "standalone" or "vl"; controls cancellation path.
    kind: str
    spent_token: str
    orders: list[LiveOrder]
    # Local placement time for cooldown guards.
    placed_at: float
    # VL batch id is present only for VL placements.
    vl_batch_id: str | None = None


@dataclass(frozen=True)
class UnsettledOrder:
    """An order rotor placed whose fills are not yet in a position."""

    order_id: str
    market: str
    side: str
    # Limit price. Fills happen at this price or better, so it is a safe breakeven.
    price: Decimal
    # Signed expiry in Sera server time; the order cannot fill after it.
    expiration: int
    # Local placement time; the position's return window starts here.
    placed_at: float


@dataclass
class TickReferences:
    """How each pair's reference fared while one tick's quotes were built."""

    # market -> the reference price a pair actually quoted on this tick.
    quoted: dict[str, ReferencePrice] = field(default_factory=dict)
    # market -> why a pair produced no quote this tick.
    skipped: dict[str, Exception] = field(default_factory=dict)
    # market -> why its own source failed, so it quoted on Wise's mid instead.
    fallback: dict[str, Exception] = field(default_factory=dict)
    # Markets whose carried daily rate was set aside for Wise's live mid.
    covered: set[str] = field(default_factory=set)
    # market -> why Wise could not price a normal order while unreachable, so
    # that order sat at the mid +/- the pair's weekend/outage spread.
    wise_outage: dict[str, Exception] = field(default_factory=dict)


@dataclass
class StaleAlertState:
    """One pair's open stale-reference alert episode."""

    # "carrying" while the pair quotes on a carried-forward rate; "wise" while
    # its own source is unavailable and it uses Wise's mid instead.
    status: str
    # Published timestamp of the carried rate; None for "wise".
    published_ts: float | None
    # When the operator was last told about this episode.
    alerted_at: float


class StateStore:
    """Tiny unresolved-state store; this is not a trade-history database."""

    def __init__(self, path: str | Path) -> None:
        self.path = ":memory:" if str(path) == ":memory:" else str(Path(path).expanduser())
        self._lock_fp = None
        if self.path != ":memory:":
            parent = Path(self.path).parent
            if str(parent) not in {"", "."}:
                parent.mkdir(parents=True, exist_ok=True)
            # Single-instance guard: two rotors on the same wallet/state would
            # fight (cross-cancel, STP self-matches, clobbered tracking). Hold an
            # exclusive advisory lock on a sibling .lock file for our lifetime.
            self._acquire_single_instance_lock()
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS active_orders (
                order_id TEXT PRIMARY KEY,
                uuid_int TEXT NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                kind TEXT NOT NULL,
                spent_token TEXT NOT NULL,
                placed_at REAL NOT NULL,
                vl_batch_id TEXT,
                qty_base TEXT
            )
        """)
        # Superseded by positions; its rows were one-tick signals, safe to drop.
        self._db.execute("DROP TABLE IF EXISTS pending_returns")
        # Every order rotor placed, until its fills are final and counted.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS unsettled_orders (
                order_id TEXT PRIMARY KEY,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                expiration INTEGER NOT NULL,
                placed_at REAL NOT NULL
            )
        """)
        # One row per market whose fills have moved inventory; gone once flat.
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                market TEXT PRIMARY KEY,
                net_base TEXT NOT NULL,
                price TEXT NOT NULL,
                opened_at REAL NOT NULL
            )
        """)
        # Pairs not quoting, so --once fires share the episode (see DarkPairAlerts).
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS dark_pairs (
                market TEXT PRIMARY KEY,
                since REAL NOT NULL,
                alerted INTEGER NOT NULL
            )
        """)
        # The stall watchdog's clock, so --once fires share it (see StallWatchdog).
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS watchdog (
                key TEXT PRIMARY KEY,
                value REAL NOT NULL
            )
        """)
        # The wallet and Sera deployment this file belongs to (see claim()).
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS identity (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS reference_alerts (
                market TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                published_ts REAL,
                alerted_at REAL NOT NULL
            )
        """)
        self._db.commit()

    def claim(self, identity: dict[str, str]) -> None:
        """Tie this file to one wallet on one Sera deployment, or refuse another.

        The first live run writes its identity; every later run must match it,
        so one network's or wallet's positions never drive another's orders.
        A file from before identities were stored is tied to its next run.
        """
        stored = dict(self._db.execute("SELECT key, value FROM identity").fetchall())
        if not stored:
            with self._db:
                self._db.executemany(
                    "INSERT INTO identity (key, value) VALUES (?, ?)", identity.items())
            return
        if stored != identity:
            raise RuntimeError(
                f"state file {self.path} belongs to {_describe_identity(stored)}; "
                f"this run is {_describe_identity(identity)}. Set ROTOR_STATE_PATH to "
                "a separate state file for each wallet and network"
            )

    def _acquire_single_instance_lock(self) -> None:
        """Take an exclusive advisory lock so only one rotor uses this state."""
        if fcntl is None:  # pragma: no cover - non-POSIX platforms.
            return
        lock_path = self.path + ".lock"
        lock_fp = open(lock_path, "w")  # noqa: SIM115 - held for process lifetime.
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_fp.close()
            raise RuntimeError(
                f"another rotor instance is using {self.path}; refusing to start "
                "(set ROTOR_STATE_PATH to run a second instance on different state)"
            ) from exc
        self._lock_fp = lock_fp

    def close(self) -> None:
        self._db.close()
        # Release and close the advisory lock so a clean restart can re-acquire it.
        if self._lock_fp is not None:
            if fcntl is not None:  # pragma: no branch - POSIX path.
                with contextlib.suppress(OSError):
                    fcntl.flock(self._lock_fp, fcntl.LOCK_UN)
            self._lock_fp.close()
            self._lock_fp = None

    def replace_active(self, placements: list[LivePlacement]) -> None:
        """Replace active unresolved orders, pruning completed/cancelled rows."""
        rows = []
        for placement in placements:
            for order in placement.orders:
                rows.append((
                    order.order_id,
                    str(order.uuid_int),
                    order.market,
                    order.side,
                    placement.kind,
                    placement.spent_token,
                    float(order.placed_at),
                    placement.vl_batch_id,
                    str(order.qty_base) if order.qty_base is not None else None,
                ))
        with self._db:
            self._db.execute("DELETE FROM active_orders")
            self._db.executemany(
                """
                INSERT OR REPLACE INTO active_orders (
                    order_id, uuid_int, market, side, kind, spent_token,
                    placed_at, vl_batch_id, qty_base
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def load_active(self, markets: set[str]) -> list[LivePlacement]:
        """Load unresolved active orders for configured markets."""
        rows = self._db.execute(
            """
            SELECT order_id, uuid_int, market, side, kind, spent_token,
                   placed_at, vl_batch_id, qty_base
            FROM active_orders
            ORDER BY COALESCE(vl_batch_id, ''), placed_at, order_id
            """
        ).fetchall()
        standalone: list[LivePlacement] = []
        batches: dict[str, tuple[str, list[LiveOrder]]] = {}
        for (
            order_id,
            uuid_int,
            market,
            side,
            kind,
            spent_token,
            placed_at,
            batch_id,
            qty_base,
        ) in rows:
            if market not in markets:
                continue
            order = LiveOrder(
                side=side,
                order_id=order_id,
                uuid_int=int(uuid_int),
                placed_at=float(placed_at),
                market=market,
                qty_base=Decimal(str(qty_base)) if qty_base is not None else None,
            )
            if kind == "vl" and batch_id:
                token, orders = batches.setdefault(batch_id, (spent_token, []))
                orders.append(order)
                batches[batch_id] = (token, orders)
            else:
                standalone.append(
                    LivePlacement(
                        kind="standalone",
                        spent_token=spent_token,
                        orders=[order],
                        placed_at=order.placed_at,
                    )
                )
        out = list(standalone)
        for batch_id, (spent_token, orders) in batches.items():
            out.append(
                LivePlacement(
                    kind="vl",
                    spent_token=spent_token,
                    orders=orders,
                    placed_at=min(order.placed_at for order in orders),
                    vl_batch_id=batch_id,
                )
            )
        return out

    def add_unsettled(self, orders: list[UnsettledOrder]) -> None:
        """Record placed orders so their fills are counted once they are final."""
        rows = [
            (
                order.order_id,
                order.market,
                order.side,
                str(order.price),
                int(order.expiration),
                float(order.placed_at),
            )
            for order in orders
        ]
        with self._db:
            self._db.executemany(
                """
                INSERT OR IGNORE INTO unsettled_orders (
                    order_id, market, side, price, expiration, placed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def remove_unsettled(self, order_ids: list[str]) -> None:
        """Drop orders Sera refused: it never booked them, so they cannot fill."""
        with self._db:
            self._db.executemany(
                "DELETE FROM unsettled_orders WHERE order_id = ?",
                [(order_id,) for order_id in order_ids],
            )

    def load_unsettled(self) -> list[UnsettledOrder]:
        """Load orders whose fills are not yet counted, oldest first."""
        return [
            UnsettledOrder(
                order_id=order_id,
                market=market,
                side=side,
                price=Decimal(price),
                expiration=int(expiration),
                placed_at=float(placed_at),
            )
            for order_id, market, side, price, expiration, placed_at in self._db.execute(
                "SELECT order_id, market, side, price, expiration, placed_at "
                "FROM unsettled_orders ORDER BY placed_at, order_id"
            )
        ]

    def load_positions(self) -> dict[str, Position]:
        """Load every open position by market."""
        return {
            market: Position(Decimal(net_base), Decimal(price), float(opened_at))
            for market, net_base, price, opened_at in self._db.execute(
                "SELECT market, net_base, price, opened_at FROM positions ORDER BY market"
            )
        }

    def settle_order(
        self, order: UnsettledOrder, filled_base: Decimal
    ) -> tuple[Position | None, Position | None]:
        """Count one order's final fills into its market's position.

        The position update and the ledger delete commit together, so an order is
        counted exactly once: a crash before the commit leaves it for next tick.
        Returns the position before and after.
        """
        with self._db:
            row = self._db.execute(
                "SELECT net_base, price, opened_at FROM positions WHERE market = ?",
                (order.market,),
            ).fetchone()
            before = (
                None if row is None
                else Position(Decimal(row[0]), Decimal(row[1]), float(row[2]))
            )
            after = before
            if filled_base > 0:
                after = apply_fill(
                    before, order.side, filled_base, order.price, order.placed_at)
                if after is None:
                    self._db.execute(
                        "DELETE FROM positions WHERE market = ?", (order.market,))
                else:
                    self._db.execute(
                        """
                        INSERT OR REPLACE INTO positions (
                            market, net_base, price, opened_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (order.market, str(after.net_base), str(after.price), after.opened_at),
                    )
            self._db.execute(
                "DELETE FROM unsettled_orders WHERE order_id = ?", (order.order_id,))
        return before, after

    def replace_reference_alerts(self, episodes: dict[str, StaleAlertState]) -> None:
        """Replace open stale-reference alert episodes, pruning resolved ones."""
        rows = [
            (market, episode.status, episode.published_ts, float(episode.alerted_at))
            for market, episode in episodes.items()
        ]
        with self._db:
            self._db.execute("DELETE FROM reference_alerts")
            self._db.executemany(
                """
                INSERT OR REPLACE INTO reference_alerts (
                    market, status, published_ts, alerted_at
                ) VALUES (?, ?, ?, ?)
                """,
                rows,
            )

    def replace_dark_pairs(self, episodes: dict[str, tuple[float, bool]]) -> None:
        """Replace the open not-quoting episodes (market -> since, alerted)."""
        with self._db:
            self._db.execute("DELETE FROM dark_pairs")
            self._db.executemany(
                "INSERT INTO dark_pairs (market, since, alerted) VALUES (?, ?, ?)",
                [(market, since, int(alerted)) for market, (since, alerted) in episodes.items()],
            )

    def load_dark_pairs(self) -> dict[str, tuple[float, bool]]:
        """Load the open not-quoting episodes."""
        return {
            market: (float(since), bool(alerted))
            for market, since, alerted in self._db.execute(
                "SELECT market, since, alerted FROM dark_pairs")
        }

    def save_watchdog(self, values: dict[str, float]) -> None:
        """Store the stall watchdog's timestamps."""
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO watchdog (key, value) VALUES (?, ?)", values.items())

    def load_watchdog(self) -> dict[str, float]:
        """Load the stall watchdog's timestamps."""
        return {
            key: float(value)
            for key, value in self._db.execute("SELECT key, value FROM watchdog")
        }

    def load_reference_alerts(self) -> dict[str, StaleAlertState]:
        """Load open stale-reference alert episodes."""
        return {
            market: StaleAlertState(
                status=status,
                published_ts=None if published_ts is None else float(published_ts),
                alerted_at=float(alerted_at),
            )
            for market, status, published_ts, alerted_at in self._db.execute(
                "SELECT market, status, published_ts, alerted_at "
                "FROM reference_alerts ORDER BY market"
            )
        }


class StallWatchdog:
    """Alert when no fresh quote set has gone out for `window` seconds.

    Every skipped tick (unreadable prices or fills, a stuck cancel, a budget
    rejection) leaves quoting stalled while the heartbeat still says "alive".
    A continuous run checks at each tick and starts the clock at boot. A
    `--once` fire checks only after placing nothing, and its clock is kept in
    the state file, so fires share it as one long run would. Placing again
    after an alert sends one "resumed" message.
    """

    def __init__(
        self,
        window: float,
        *,
        state: StateStore | None,
        notifier: TelegramNotifier | None,
        log: logging.Logger,
        carry_over: bool,
    ) -> None:
        now = time.time()
        stored = state.load_watchdog() if state is not None else {}
        self.window = window
        self.last_quote_ok = stored.get("last_quote_ok", now) if carry_over else now
        self.last_alert = stored.get("last_alert", 0.0)
        self.quoted_this_run = False
        self._state = state
        self._notifier = notifier
        self._log = log
        self._save()

    def quoted(self) -> None:
        """A fresh quote set went out."""
        now = time.time()
        if self.last_alert >= self.last_quote_ok:
            self._log.warning(
                "quoting resumed after %s without a placement",
                _fmt_duration(now - self.last_quote_ok))
            if self._notifier is not None:
                self._notifier.send_raw(
                    "✅ VL quoting resumed after "
                    f"{_fmt_duration(now - self.last_quote_ok)} without a placement.",
                    important=True,
                )
        self.last_quote_ok = now
        self.quoted_this_run = True
        self._save()

    def check(self) -> None:
        """Alert, at most once per window, if quoting has stalled for a window."""
        now = time.time()
        stalled_for = now - self.last_quote_ok
        if stalled_for < self.window or now - self.last_alert < self.window:
            return
        self._log.error("VL quoting stalled: no successful placement for %.0fs", stalled_for)
        if self._notifier is not None:
            self._notifier.notify_error(
                "VL quoting stalled",
                RuntimeError(f"no successful placement for {stalled_for:.0f}s"),
            )
        self.last_alert = now
        self._save()

    def _save(self) -> None:
        if self._state is not None:
            self._state.save_watchdog(
                {"last_quote_ok": self.last_quote_ok, "last_alert": self.last_alert})


class DarkPairAlerts:
    """Alert when one pair stops quoting while the others may carry on.

    The stall watchdog sees only whether anything was placed, so a single pair
    whose price source keeps failing would otherwise show up only as a log
    line per tick. A pair skipped on every tick for DARK_PAIR_ALERT_SECONDS
    gets one alert with the reason; its next quote sends one "quoting again".
    Wise being unreachable that long, so normal orders sit at the mid +/- the
    weekend/outage spread, is one more episode (under WISE_OUTAGE). Episodes
    are kept in the state file, so --once fires share them.
    """

    def __init__(
        self,
        markets: set[str],
        *,
        state: StateStore | None = None,
        window: float = DARK_PAIR_ALERT_SECONDS,
    ) -> None:
        self._state = state
        self.window = window
        loaded = {} if state is None else state.load_dark_pairs()
        # market (or WISE_OUTAGE) -> (first seen at, alerted yet)
        self._episodes = {
            m: e for m, e in loaded.items() if m in markets or m == WISE_OUTAGE}

    def update(
        self,
        refs: TickReferences,
        *,
        notifier: TelegramNotifier | None,
        log: logging.Logger,
        now: float | None = None,
    ) -> None:
        """Advance each pair's episode from this tick and alert on changes."""
        now = time.time() if now is None else now
        dark: list[str] = []
        back: list[str] = []
        outage: list[str] = []
        recovered: list[str] = []
        before = dict(self._episodes)
        for market, error in sorted(refs.skipped.items(), key=lambda item: item[0]):
            since, alerted = self._episodes.get(market, (now, False))
            if not alerted and now - since >= self.window:
                alerted = True
                dark.append(f"{market} for {_fmt_duration(now - since)}: {_first_line(error)}")
            self._episodes[market] = (since, alerted)
        for market in sorted(refs.quoted):
            episode = self._episodes.pop(market, None)
            if episode is not None and episode[1]:
                back.append(f"{market} after {_fmt_duration(now - episode[0])}")
        if refs.wise_outage:
            since, alerted = self._episodes.get(WISE_OUTAGE, (now, False))
            if not alerted and now - since >= self.window:
                alerted = True
                first = next(iter(refs.wise_outage.values()))
                outage.append(
                    f"for {_fmt_duration(now - since)} ({', '.join(sorted(refs.wise_outage))}): "
                    f"{_first_line(first)}")
            self._episodes[WISE_OUTAGE] = (since, alerted)
        else:
            episode = self._episodes.pop(WISE_OUTAGE, None)
            if episode is not None and episode[1]:
                recovered.append(f"after {_fmt_duration(now - episode[0])}")
        if self._episodes != before and self._state is not None:
            self._state.replace_dark_pairs(self._episodes)
        for lines, icon, heading in (
            (dark, "⚠️", "Not quoting these pairs; the other pairs are quoting:"),
            (back, "✅", "Quoting again:"),
            (outage, "⚠️", "Wise is not answering, so normal orders sit at the mid "
                           "+/- each pair's fallback_weekend_bps:"),
            (recovered, "✅", "Wise is answering again; normal orders are back at its prices:"),
        ):
            if not lines:
                continue
            body = "\n".join([heading, *lines])
            log.warning("%s", body.replace("\n", " | "))
            if notifier is not None:
                notifier.send_raw(f"{icon} {body}", important=True)


class StaleReferenceAlerts:
    """Tell the operator when pairs quote on reference rates that stopped updating.

    Official daily FX sources (BNM, ECB, the Fed) publish nothing on
    weekends or public holidays, so the oracle carries their last rate forward.
    While Wise answers, its live mid is used instead and nothing is announced;
    a carried rate is quoted on only when Wise has no mid either. Each such
    episode is announced once on the important channel, repeated quietly every
    `reminder_seconds`, and closed with a notice when fresh rates return. A pair
    whose source is unavailable altogether, and so uses Wise's mid, is announced
    once too. Episodes live in the state store so fire-once runs do not
    re-announce the same weekend every run. Every message also reaches the
    log, with or without --telegram.
    """

    def __init__(
        self,
        runtimes: list[PairRuntime],
        *,
        state: StateStore | None = None,
        reminder_seconds: float = STALE_REMINDER_SECONDS,
    ) -> None:
        # Each pair's carry limit, so a warning can say how long it may last.
        self._carry_limit = {
            runtime.config.market: runtime.oracle.max_carry_age_s for runtime in runtimes
        }
        self._state = state
        self.reminder_seconds = reminder_seconds
        loaded = {} if state is None else state.load_reference_alerts()
        # Drop episodes for markets that are no longer configured.
        self._episodes = {
            market: episode
            for market, episode in loaded.items()
            if market in self._carry_limit
        }

    def update(
        self,
        refs: TickReferences,
        *,
        notifier: TelegramNotifier,
        log: logging.Logger,
        now: float | None = None,
    ) -> None:
        """Advance each pair's episode from this tick's references and alert."""
        now = time.time() if now is None else now
        started: list[str] = []
        reminded: list[str] = []
        recovered: list[str] = []
        changed = False
        switched: list[str] = []
        for market, reason in sorted(refs.fallback.items(), key=lambda item: item[0]):
            episode = self._episodes.get(market)
            if episode is None or episode.status != "wise":
                self._episodes[market] = StaleAlertState("wise", None, now)
                switched.append(f"{market}: {_first_line(reason)}")
                changed = True
        for market, ref in sorted(refs.quoted.items(), key=lambda item: item[0]):
            # A carried rate set aside for Wise's mid neither opens nor closes
            # an episode, so a Wise blip on a weekend does not flap the alert.
            if market in refs.fallback or market in refs.covered:
                continue
            episode = self._episodes.get(market)
            if not ref.stale:
                if episode is not None:
                    # A fresh rate closes the episode.
                    del self._episodes[market]
                    recovered.append(f"{market}: {ref.source} rate {_rate_date(ref.ts)}")
                    changed = True
                continue
            line = self._carry_line(market, ref)
            if episode is None or episode.status != "carrying":
                self._episodes[market] = StaleAlertState("carrying", ref.ts, now)
                started.append(line)
                changed = True
            elif now - episode.alerted_at >= self.reminder_seconds:
                episode.published_ts = ref.ts
                episode.alerted_at = now
                reminded.append(line)
                changed = True
        if changed and self._state is not None:
            self._state.replace_reference_alerts(self._episodes)
        for lines, icon, heading, footer, important in (
            (
                started,
                "🟡",
                "Reference rates are not updating. These pairs keep quoting on the "
                "last published rate:",
                "This is expected on weekends and public holidays, when daily FX "
                "sources do not publish.",
                True,
            ),
            (
                reminded,
                "🟡",
                "Still quoting on reference rates that have not updated:",
                None,
                False,
            ),
            (
                switched,
                "🟡",
                "These pairs' reference rates are unavailable, so they use Wise's "
                "mid-market rate instead (it bounds return orders; quotes are at "
                "Wise's prices either way):",
                None,
                True,
            ),
            (recovered, "✅", "Reference rates updated again:", None, False),
        ):
            if not lines:
                continue
            body = "\n".join([heading, *lines, *([footer] if footer else [])])
            # One compact log line per state change, never one per tick.
            log.warning("%s", body.replace("\n", " | "))
            notifier.send_raw(f"{icon} {body}", important=important)

    def _carry_line(self, market: str, ref: ReferencePrice) -> str:
        """Describe one pair quoting on a carried-forward rate."""
        limit = self._carry_limit.get(market)
        until = (
            ""
            if limit is None
            else f"; carried until {_fmt_utc(ref.ts + limit)} at most"
        )
        return (
            f"{market}: {ref.source} rate {_rate_date(ref.ts)}, "
            f"{_fmt_duration(ref.age_s)} old{until}"
        )


def _first_line(error: Exception) -> str:
    """An error's first line, for a compact alert."""
    return (str(error).splitlines() or [type(error).__name__])[0]


def _rate_date(ts: float) -> str:
    """Label a published timestamp, as a plain date for date-only sources."""
    stamp = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
    # Daily sources publish a date, which the adapters stamp as UTC midnight.
    if (stamp.hour, stamp.minute, stamp.second) == (0, 0, 0):
        return f"dated {stamp:%Y-%m-%d}"
    return f"from {stamp:%Y-%m-%d %H:%M} UTC"


def _fmt_utc(ts: float) -> str:
    """Format POSIX seconds as a UTC minute."""
    return f"{dt.datetime.fromtimestamp(ts, tz=dt.UTC):%Y-%m-%d %H:%M} UTC"


def _fmt_duration(seconds: float) -> str:
    """Format an age compactly: 2d 3h, 5h 12m, or 40m."""
    minutes = int(max(0.0, seconds) // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def run(
    *,
    config_path: str | Path = "rotor/vl.config.toml",
    poll_seconds: float = 305.0,
    order_ttl_seconds: int = 600,
    min_requote_seconds: float = 300.0,
    max_reference_age_s: float | None = None,
    halt_on_stale_reference: bool = False,
    dry_run: bool = False,
    once: bool = False,
    cancel_on_exit: bool = False,
    telegram: bool = False,
    auto_update: bool = False,
    update_interval_seconds: float = DEFAULT_UPDATE_INTERVAL_SECONDS,
    log_level: str = "WARNING",
    state_path: str | Path | None = None,
) -> int:
    """Run the TOML-driven VL market maker."""
    # Configure logging before boot so config/network failures are visible.
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.WARNING),
        format="%(levelname).1s %(message)s",
    )
    # The HTTP libraries log request URLs at INFO/DEBUG, and every Telegram URL
    # carries the bot token. Rotor's own --log-level lines are unaffected.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    log = logging.getLogger("rotor.mm.vl")
    if order_ttl_seconds < MIN_ORDER_TTL_SECONDS:
        log.error(
            "--order-ttl %ds is too short: Sera sweeps orders within 60 s of expiry "
            "every 30 s; use at least %ds", order_ttl_seconds, MIN_ORDER_TTL_SECONDS,
        )
        return 1
    # Created first so a failed startup can alert and its waits can be stopped.
    notifier = TelegramNotifier(log=log, dry_run=not telegram)
    stop = StopFlag()
    stop.install()
    budget = BootBudget(
        None if dry_run else _boot_budget_path(state_path or _state_path()))
    # Startup: load the config, bootstrap Sera, and build the pairs. Temporary
    # failures (network, Sera 5xx/429, a degraded /health) are retried, at most
    # BOOT_ATTEMPTS_PER_HOUR attempts per clock hour; then rotor waits for the
    # next hour. Anything else (config, a wrong contract or token, no quotable
    # pair) stops rotor at once. Every attempt counts toward the hour.
    while True:
        if budget.exhausted():
            resume = _fmt_utc(budget.next_hour())
            log.error(
                "startup failed %d times this hour; next attempt at %s",
                budget.failures_this_hour(), resume,
            )
            if once:
                return 1
            if stop.wait(budget.next_hour() - time.time()):
                return 1
            budget.start_new_hour()
            continue
        try:
            config, client, runtimes, skipped = _start(
                config_path,
                dry_run=dry_run,
                max_reference_age_s=max_reference_age_s,
                carry_stale=not halt_on_stale_reference,
                log=log,
            )
            break
        except Exception as exc:
            failures = budget.record_failure()
            temporary = _is_temporary(exc)
            retrying = temporary and not once and failures < BOOT_ATTEMPTS_PER_HOUR
            log.error(
                "startup failed (attempt %d of %d this hour%s): %s",
                failures, BOOT_ATTEMPTS_PER_HOUR,
                f"; retrying in {BOOT_RETRY_WAITS[failures - 1]:.0f}s" if retrying else "",
                exc,
            )
            # Alert on the hour's first failure and when the hour's attempts run
            # out, not on every attempt.
            if failures == 1 or failures >= BOOT_ATTEMPTS_PER_HOUR:
                notifier.notify_error(
                    f"rotor could not start (attempt {failures} of "
                    f"{BOOT_ATTEMPTS_PER_HOUR} this hour"
                    + (f"; next attempt at {_fmt_utc(budget.next_hour())})"
                       if failures >= BOOT_ATTEMPTS_PER_HOUR and temporary else ")"),
                    exc,
                )
            if not temporary or once:
                return 1
            if retrying and stop.wait(BOOT_RETRY_WAITS[failures - 1]):
                return 1
    markets = {rt.config.market for rt in runtimes}

    updater = (
        DailyGitUpdater(interval_seconds=update_interval_seconds)
        if auto_update
        else None
    )
    state: StateStore | None = None
    try:
        if not dry_run:
            state = StateStore(state_path or _state_path())
            state.claim(_identity(client))
    except Exception as exc:
        budget.record_failure()
        log.error("state store failed: %s", exc)
        notifier.notify_error("rotor could not use its state file", exc)
        if state is not None:
            state.close()
        return 1
    if budget.failures_this_hour():
        notifier.send_raw(
            f"✅ rotor started after {budget.failures_this_hour()} failed attempt(s)",
            important=True,
        )
    budget.clear()
    # Say which network and wallet this run trades on: a testnet run with
    # mainnet expectations (or the reverse) should be obvious at a glance.
    started = (
        f"VL mm started on {_network_name(client)}"
        f"{'' if dry_run else ' as ' + client.owner_address} pairs={len(runtimes)} "
        f"budgets={','.join(sorted(config.budget_tokens))}"
        f"{' (dry-run)' if dry_run else ''}"
    )
    log.warning("%s", started)
    notifier.notify_startup(started)
    if skipped:
        notifier.send_raw(
            "⚠️ Not quoting "
            + "; ".join(f"{market} ({why})" for market, why in skipped.items())
            + ". The other pairs are quoting.",
            important=True,
        )
    # Throttled warnings while pairs quote on carried-forward daily rates.
    stale_alerts = StaleReferenceAlerts(runtimes, state=state)
    # One alert when a single pair stops quoting for a while, one when it is back.
    dark_pairs = DarkPairAlerts(markets, state=state)
    # Reattach to any orders already resting on Sera for the configured markets
    # so a restart does not orphan or duplicate live quotes. In dry-run we never
    # touch live state.
    stored_live = [] if state is None else state.load_active(markets)
    foreign: list[str] = []
    live: list[LivePlacement] = [] if dry_run else _load_live_placements(
        client,
        markets,
        log,
        fallback=stored_live,
        stop=stop,
        known=_known_order_ids(state, stored_live),
        foreign=foreign,
    )
    if foreign:
        # One rotor per wallet: a second one would fight this wallet's other
        # trader over budgets and self-matching, so refuse to start.
        message = _foreign_orders_message(client, foreign)
        log.error("%s", message)
        notifier.send_raw(f"⛔ rotor did not start: {message}", important=True)
        if state is not None:
            state.close()
        return 1
    if live and not dry_run:
        # One cancel pass, never blocking on Sera's 5-minute cancel cooldown.
        # Orders still on cooldown stay tracked, and the tick loop's cancel
        # guard waits or skips until they can be cancelled before it places.
        live = _cancel_placements(client, live, log)
    if state is not None:
        state.replace_active(live)

    restart_after_update = False
    # Whether open orders someone else placed on this wallet were reported.
    foreign_alerted = False
    # Orders already reported as stuck in settlement.
    stuck_settlements: set[str] = set()
    # Whether shutdown cleanup left orders live (see _cancel_before_exit).
    left_live = False
    # order id -> consecutive ticks its fills could not be read.
    read_failures: dict[str, int] = {}
    # Markets Sera rejected as inactive: not quoted until the next metadata
    # refresh. Each is alerted once, until it places again.
    inactive: set[str] = set()
    inactive_alerted: set[str] = set()
    last_refresh = time.time()
    next_refresh = last_refresh + METADATA_REFRESH_SECONDS
    # True while metadata refreshes fail; alerted when it starts and ends.
    refresh_failing = False
    # Stall watchdog: alert (once per window) when no fresh quote set has gone
    # out for a while. Dry-run never places, so it is not watched.
    watchdog = StallWatchdog(
        max(poll_seconds * 3, 900.0),
        state=state,
        notifier=notifier,
        log=log,
        carry_over=once,
    )
    try:
        while not stop.requested:
            tick_started = time.time()
            notifier.heartbeat()
            if not dry_run and not once:
                watchdog.check()
            if updater is not None:
                update_result = updater.run_if_due(log)
                if update_result is not None and update_result.updated:
                    restart_after_update = True
                    log.warning("auto-update stopping VL loop before restart")
                    notifier.send_raw(f"🔄 rotor auto-update: {update_result.message}; restarting")
                    break
                if update_result is not None and update_result.status in ("failed", "skipped"):
                    # --auto-update was asked for; say so when it is not working.
                    notifier.send_raw(
                        f"⚠️ rotor auto-update {update_result.status}: {update_result.message}",
                        important=True,
                    )
            # Re-read Sera's metadata hourly (sooner after a rejection that
            # suggests it went stale). A failed refresh keeps the last good
            # metadata and is tried again later; --once runs read it at boot.
            if not dry_run and not once and time.time() >= next_refresh:
                try:
                    runtimes = _refresh_runtimes(
                        config,
                        client,
                        runtimes,
                        max_reference_age_s=max_reference_age_s,
                        carry_stale=not halt_on_stale_reference,
                        log=log,
                        notifier=notifier,
                    )
                    markets = {rt.config.market for rt in runtimes}
                    inactive.clear()
                    last_refresh = time.time()
                    next_refresh = last_refresh + METADATA_REFRESH_SECONDS
                    if refresh_failing:
                        refresh_failing = False
                        log.warning("Sera metadata refresh recovered")
                        notifier.send_raw("✅ Sera market metadata refreshed again.")
                except Exception as exc:
                    log.warning("could not refresh Sera metadata; keeping the last: %s", exc)
                    next_refresh = time.time() + EARLY_REFRESH_SECONDS
                    if not refresh_failing:
                        refresh_failing = True
                        notifier.notify_error(
                            "could not refresh Sera market metadata; quoting on the "
                            "last good metadata and retrying every 10 minutes",
                            exc,
                        )
            # Phase 0: if we have no tracked state, try to recover open orders
            # from Sera before quoting again (falls back to prior local state).
            if not dry_run and not live:
                stored = state.load_active(markets) if state is not None else live
                foreign = []
                live = _load_live_placements(
                    client,
                    markets,
                    log,
                    announce=False,
                    fallback=stored,
                    stop=stop,
                    known=_known_order_ids(state, stored),
                    foreign=foreign,
                )
                if state is not None:
                    state.replace_active(live)
                if foreign and not foreign_alerted:
                    message = _foreign_orders_message(client, foreign)
                    log.error("%s", message)
                    notifier.send_raw(f"⚠️ {message}", important=True)
                foreign_alerted = bool(foreign)
            if dry_run:
                tick_expiration = int(time.time()) + order_ttl_seconds
            else:
                try:
                    tick_expiration = client.expiration_from_server_time(order_ttl_seconds)
                except Exception as exc:
                    log.warning("skip tick: server time unavailable: %s", exc)
                    if once:
                        return 1
                    sleep_remaining(tick_started, poll_seconds, stop)
                    continue
            # Phase 1: read the fills of every order rotor accounts for: its live
            # quotes and every placed order not yet settled. Fully filled live
            # orders stop being tracked, and positions are brought up to date.
            # A market with an order whose fills could not be read is not quoted
            # this tick, rather than quoted on missing fills; other markets are.
            positions: dict[str, Position] = {}
            unreadable: set[str] = set()
            funds: dict[str, Decimal] | None = None
            if not dry_run:
                funds = _read_funds(client, log)
                try:
                    unsettled = [] if state is None else state.load_unsettled()
                    fills, failed, unknown_orders = _read_fills(client, [
                        *(order.order_id for order in unsettled),
                        *(order.order_id for placement in live for order in placement.orders),
                    ])
                    unreadable = _markets_of(failed, unsettled, live)
                    read_failures = _report_read_failures(
                        failed, unsettled, live, read_failures, log=log, notifier=notifier)
                    live = _prune_fully_filled_placements(
                        live, _filled_quantities(fills), log)
                    if state is not None:
                        state.replace_active(live)
                        positions = _positions_for_quoting(
                            state,
                            unsettled,
                            fills,
                            server_now=tick_expiration - order_ttl_seconds,
                            log=log,
                            unknown=unknown_orders,
                            stuck=stuck_settlements,
                        )
                except Exception as exc:
                    log.warning("skip tick: fill polling failed: %s", exc)
                    if once:
                        return 1
                    sleep_remaining(tick_started, poll_seconds, stop)
                    continue

            # Phase 2: build each pair's fresh quotes as per-leg intents. A market
            # with an open position quotes its return order on that side instead
            # of the normal one.
            tick_refs = TickReferences()
            try:
                intents = _build_tick_intents(
                    config,
                    runtimes,
                    positions,
                    expiration=tick_expiration,
                    log=log,
                    refs=tick_refs,
                    skip=inactive,
                    unreadable=unreadable,
                    funds=funds,
                )
            except Exception as exc:
                log.warning("skip tick: %s", exc)
                if once:
                    return 1
                sleep_remaining(tick_started, poll_seconds, stop)
                continue

            # Warn (throttled) while pairs quote on carried-forward daily rates.
            # An alert failure must never cost a tick, so it is only logged.
            try:
                stale_alerts.update(tick_refs, notifier=notifier, log=log)
            except Exception as exc:
                log.warning("stale-reference alert failed: %s", exc)

            if not intents:
                # No emitted sides means nothing can be safely placed this tick.
                if not dry_run:
                    _update_dark_pairs(dark_pairs, tick_refs, set(), PlacementIssues(),
                                       notifier=notifier, log=log)
                log.warning("skip tick: no emitted order intents")
                if once:
                    return 1
                sleep_remaining(tick_started, poll_seconds, stop)
                continue

            # Phase 3: group legs by spent token; a group with >= vl_batch_min
            # legs goes through VL, otherwise each leg is placed standalone.
            groups = group_intents(intents, client.vl_batch_min, client.vl_batch_max)

            # Phase 4: reject the whole tick if any budgeted token could spend
            # more than its configured budget, counted the way Sera locks it (a
            # local guard; Sera's shared vl_group budget is the authoritative cap).
            # Each order is already trimmed to its budget, so this only trips
            # when one token's orders are split across batches (over 50 pairs).
            try:
                _validate_budget_caps(groups, config)
            except Exception as exc:
                log.warning("skip tick: %s", exc)
                if once:
                    return 1
                sleep_remaining(tick_started, poll_seconds, stop)
                continue
            _log_groups(groups, log, dry_run=dry_run)

            if dry_run:
                # Dry-run logs grouping only; no signatures or cancellations.
                if once:
                    return 0
                sleep_remaining(tick_started, poll_seconds, stop)
                continue

            # Phase 5: local cancel-cooldown guard. Orders are placed one after
            # another, so at the default 305s cadence the newest can still be a
            # few seconds short of the guard; wait for it then. A longer wait
            # (restarts, shortened poll intervals) skips the tick instead.
            live_orders = [
                order for placement in live for order in placement.orders]
            if live_orders and not all_requote_safe(live_orders, min_requote_seconds):
                wait_s = (
                    max(order.placed_at for order in live_orders)
                    + min_requote_seconds - time.time()
                )
                if wait_s > MAX_GUARD_WAIT_SECONDS:
                    log.debug("skip tick: cancel cooldown guard clears in %.1fs", wait_s)
                    if once:
                        return 0
                    sleep_remaining(tick_started, poll_seconds, stop)
                    continue
                log.debug("waiting %.1fs for the cancel cooldown guard", wait_s)
                if stop.wait(wait_s):
                    continue

            # Phase 6: cancel-first. Clear all resting placements before placing
            # fresh ones so the new quotes cannot self-match (STP_BLOCKED) the
            # old ones. If any cancel is still on cooldown, skip this tick.
            if live:
                cancelled = {order.order_id for placement in live for order in placement.orders}
                live = _cancel_placements(client, live, log)
                if state is not None:
                    state.replace_active(live)
                if live:
                    if once:
                        return 1
                    sleep_remaining(tick_started, poll_seconds, stop)
                    continue
                # Sera answers a cancel before applying it; until it does, the
                # old batch still holds its budget and could block the new one.
                _wait_until_gone(client, cancelled, log, stop=stop)
            if stop.requested and cancel_on_exit:
                # Shutting down to leave nothing resting: placing a fresh set now
                # would only leave orders Sera will not let us cancel yet.
                break

            # Phase 7: place exactly the fresh quote set. Every order is recorded
            # for settlement before it is sent; an order or VL batch Sera rejects
            # is skipped and the rest still go out.
            issues = PlacementIssues()
            placed = _place_groups(client, groups, log, state=state, issues=issues)
            for market in sorted(issues.inactive - inactive_alerted):
                log.error("%s is not active on Sera; not quoting it until the next refresh", market)
                notifier.send_raw(
                    f"⚠️ Sera says {market} is not active. Not quoting it until "
                    "the next market refresh; the other pairs are quoting.",
                    important=True,
                )
            inactive |= issues.inactive
            inactive_alerted |= issues.inactive
            inactive_alerted -= {o.market for p in placed for o in p.orders}
            if not dry_run:
                sent = {o.market for p in placed + issues.unanswered for o in p.orders}
                _update_dark_pairs(dark_pairs, tick_refs, sent, issues, notifier=notifier, log=log)
            if issues.stale_metadata:
                next_refresh = min(next_refresh, last_refresh + EARLY_REFRESH_SECONDS)
            # Placements Sera may have booked without answering are tracked too,
            # so the next cycle cancels them with the rest. (An order filled at
            # placement is pruned by the next tick's fill read.)
            live = placed + issues.unanswered
            if state is not None:
                state.replace_active(live)
            if placed:
                # A fresh quote set went out: reset the stall watchdog. (No
                # message per tick: the stall alert reports a run of failures.)
                watchdog.quoted()
            if once:
                return 0 if placed else 1
            sleep_remaining(tick_started, poll_seconds, stop)
    except Exception as exc:
        # Anything that ends the loop unexpectedly (a state file that cannot be
        # written, say) stops rotor: say so loudly, not only "VL mm stopped".
        log.exception("rotor stopped on an unexpected error")
        notifier.notify_error("rotor stopped on an unexpected error", exc)
        raise
    finally:
        # A --once fire that placed nothing checks for a stall shared across fires.
        if once and not dry_run and not watchdog.quoted_this_run:
            watchdog.check()
        # Shutdown cleanup cancels tracked placements in live mode. An
        # auto-update restart always cancels first (even without --cancel-on-exit)
        # so the next process does not boot into stale-priced resting quotes that
        # could be picked off during the restart gap.
        if (cancel_on_exit or restart_after_update) and live and not dry_run:
            # A restart cancels once: the next process cancels leftovers at boot.
            live = _cancel_before_exit(
                client, live, log, stop=stop, notifier=notifier,
                wait=cancel_on_exit and not restart_after_update,
                order_ttl_seconds=order_ttl_seconds,
            )
            if state is not None:
                state.replace_active(live)
            left_live = bool(live)
        notifier.notify_shutdown("VL mm stopped")
        if state is not None:
            state.close()
        # Release the HTTP connection pool (best-effort; fakes may lack close()).
        with contextlib.suppress(Exception):
            client.close()
    if restart_after_update and stop.requested:
        # A stop that arrived while the update was being checked wins: the
        # operator asked rotor to exit, not to come back as a new version.
        log.warning("stop requested during auto-update; exiting instead of restarting")
    elif restart_after_update:
        try:
            restart_current_process()
        except Exception as exc:
            log.error("auto-update restart failed: %s", exc)
            return 1
    # --cancel-on-exit could not clear everything.
    return 1 if left_live else 0


def _cancel_before_exit(
    client: SeraClient,
    live: list[LivePlacement],
    log: logging.Logger,
    *,
    stop: StopFlag,
    notifier: TelegramNotifier,
    wait: bool,
    order_ttl_seconds: int,
) -> list[LivePlacement]:
    """Cancel tracked placements on shutdown; report any that stay live.

    Sera refuses to cancel an order younger than its cancel cooldown, so with
    `wait` rotor sleeps until the youngest remaining order is old enough and
    tries once more (a second Ctrl-C or SIGTERM skips the wait). Anything still
    live is logged and alerted with when it expires.
    """
    live = _cancel_placements(client, live, log)
    youngest = max(
        (order.placed_at for placement in live for order in placement.orders),
        default=time.time(),
    )
    if live and wait and not stop.forced:
        delay = youngest + SERA_CANCEL_COOLDOWN_SECONDS + 5.0 - time.time()
        if delay > 0:
            log.warning(
                "waiting %.0fs for Sera's cancel cooldown before cancelling the last "
                "orders (stop again to skip)", delay,
            )
            stop.wait_forced(delay)
        if not stop.forced:
            live = _cancel_placements(client, live, log)
    if live:
        count = sum(len(placement.orders) or 1 for placement in live)
        message = (
            f"{count} order(s) could not be cancelled before exit (Sera's cancel "
            f"cooldown); they expire by about {_fmt_utc(youngest + order_ttl_seconds)}"
        )
        log.warning("%s", message)
        notifier.send_raw(f"⚠️ {message}", important=True)
    return live


def _refresh_runtimes(
    config: VLConfig,
    client: SeraClient,
    runtimes: list[PairRuntime],
    *,
    max_reference_age_s: float | None,
    carry_stale: bool,
    log: logging.Logger,
    notifier: TelegramNotifier,
) -> list[PairRuntime]:
    """Re-read Sera's metadata and rebuild the pairs from it.

    Bootstrap is all-or-nothing and re-applies the contract and token checks;
    on any failure this raises and the caller keeps the current pairs. Pairs
    that drop out (unlisted, unmapped) are logged and alerted once here.
    """
    saved = {name: getattr(client, name) for name in CLIENT_METADATA}
    client.bootstrap()
    skipped: dict[str, str] = {}
    try:
        fresh = _build_runtimes(
            config,
            client,
            max_reference_age_s=max_reference_age_s,
            carry_stale=carry_stale,
            log=log,
            skipped=skipped,
        )
    except Exception:
        # Bootstrap already applied metadata with no pair to quote (Sera
        # briefly serving an empty market list, say); keep the current pairs
        # quoting on the metadata they were built from.
        for name, value in saved.items():
            setattr(client, name, value)
        raise
    dropped = {rt.config.market for rt in runtimes} & set(skipped)
    if dropped:
        notifier.send_raw(
            "⚠️ No longer quoting "
            + "; ".join(f"{market} ({skipped[market]})" for market in sorted(dropped))
            + ". The other pairs are quoting.",
            important=True,
        )
    return fresh


def _start(
    config_path: str | Path,
    *,
    dry_run: bool,
    max_reference_age_s: float | None,
    carry_stale: bool,
    log: logging.Logger,
) -> tuple[VLConfig, SeraClient, list[PairRuntime], dict[str, str]]:
    """One startup attempt: load the config, bootstrap Sera, and build the pairs."""
    config = load_vl_config(config_path)
    # Construction rejects a non-TLS base URL; bootstrap loads the signing
    # domain, VL batch limits, executor id, and markets.
    client = build_client(dry_run=dry_run, log=log)
    try:
        client.bootstrap()
        # market -> why that configured pair is not quoted.
        skipped: dict[str, str] = {}
        runtimes = _build_runtimes(
            config,
            client,
            max_reference_age_s=max_reference_age_s,
            carry_stale=carry_stale,
            log=log,
            skipped=skipped,
        )
    except Exception:
        with contextlib.suppress(Exception):
            client.close()
        raise
    return config, client, runtimes, skipped


def _is_temporary(exc: Exception) -> bool:
    """Return whether a startup failure may clear on its own (worth retrying)."""
    if isinstance(exc, httpx.TransportError | errors.SeraUnavailable):
        return True
    return isinstance(exc, errors.SeraError) and (
        exc.status_code == 429 or exc.status_code >= 500)


class BootBudget:
    """Failed startup attempts in the current clock hour, kept in a small file.

    The file lets `--once` fires and supervisor restarts share one hourly
    budget. Without a path (dry-run, in-memory state) the count lives only in
    this process. A file that cannot be read or written counts as empty.
    """

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._hour, self._failures = -1, 0
        if path is not None:
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                saved = json.loads(Path(path).read_text())
                self._hour, self._failures = int(saved["hour"]), int(saved["failures"])

    @staticmethod
    def _current_hour() -> int:
        return int(time.time() // 3600)

    def failures_this_hour(self) -> int:
        return self._failures if self._hour == self._current_hour() else 0

    def exhausted(self) -> bool:
        return self.failures_this_hour() >= BOOT_ATTEMPTS_PER_HOUR

    def next_hour(self) -> float:
        """POSIX time at which the next clock hour starts."""
        return (self._current_hour() + 1) * 3600.0

    def record_failure(self) -> int:
        """Count a failed attempt and return the hour's total so far."""
        self._failures = self.failures_this_hour() + 1
        self._hour = self._current_hour()
        self._save()
        return self._failures

    def start_new_hour(self) -> None:
        """Begin a fresh count after waiting out an exhausted hour."""
        self._hour, self._failures = self._current_hour(), 0
        self._save()

    def clear(self) -> None:
        """Forget all failures once rotor has started."""
        self._hour, self._failures = -1, 0
        if self.path is not None:
            with contextlib.suppress(OSError):
                Path(self.path).unlink(missing_ok=True)

    def _save(self) -> None:
        if self.path is None:
            return
        with contextlib.suppress(OSError):
            Path(self.path).write_text(
                json.dumps({"hour": self._hour, "failures": self._failures}))


def _boot_budget_path(state_path: str | Path) -> str | None:
    """The startup-attempt file beside the state file, or None for in-memory state."""
    if str(state_path) == ":memory:":
        return None
    return str(Path(state_path).expanduser()) + ".boot.json"


def _identity(client: SeraClient) -> dict[str, str]:
    """The wallet and Sera deployment a live run trades on."""
    return {
        "owner": client.owner_address.lower(),
        "chain_id": str(client.eip712_domain["chainId"]),
        "sera_contract": str(client.eip712_domain["verifyingContract"]).lower(),
    }


def _describe_identity(identity: dict[str, str]) -> str:
    return (
        f"wallet {identity.get('owner')} on chain {identity.get('chain_id')} "
        f"(Sera {identity.get('sera_contract')})"
    )


def _state_path() -> str:
    """Return the unresolved-state database path."""
    return str(get_config("ROTOR_STATE_PATH", DEFAULT_STATE_PATH) or DEFAULT_STATE_PATH)


def group_intents(
    intents: list[OrderIntent],
    vl_min_size: int,
    vl_max_size: int = 10**9,
) -> list[tuple[str, list[OrderIntent], bool]]:
    """Group intents by spent token and mark whether each group must use VL.

    Every same-spent-token group with at least `vl_min_size` legs goes as a VL
    batch, budgeted or not: each order is sized to what the token may spend
    (its budget or the vault balance), and Sera freezes only a batch's largest
    leg, whereas standalone orders would each lock their full amount and Sera
    would refuse all but the first. A group larger than Sera's batch maximum is
    split into chunks of at most `vl_max_size` legs so an oversized group cannot
    make `place_vl_batch` reject (and tear down) every tick.
    """
    # Spent token is the budget dimension Sera VL uses for shared collateral.
    by_spent: dict[str, list[OrderIntent]] = {}
    for intent in intents:
        by_spent.setdefault(intent.spent_token, []).append(intent)
    groups: list[tuple[str, list[OrderIntent], bool]] = []
    for spent_token, rows in sorted(by_spent.items()):
        if len(rows) < vl_min_size:
            # A group too small for a batch places standalone.
            groups.append((spent_token, rows, False))
            continue
        # VL-eligible group: chunk so no batch exceeds Sera's maximum.
        for start in range(0, len(rows), vl_max_size):
            chunk = rows[start:start + vl_max_size]
            # A trailing chunk below the batch minimum places standalone.
            groups.append((spent_token, chunk, len(chunk) >= vl_min_size))
    return groups


def _build_runtimes(
    config: VLConfig,
    client: SeraClient,
    *,
    max_reference_age_s: float | None,
    carry_stale: bool = True,
    log: logging.Logger | None = None,
    skipped: dict[str, str] | None = None,
) -> list[PairRuntime]:
    """Build runtime objects for every configured pair rotor can quote.

    A pair whose market Sera does not list (delisted, or configured in the other
    orientation) or whose token has no currency mapping is skipped with a
    warning, and its reason recorded in `skipped`, so the other pairs still
    quote. Raises only when no pair is left.
    """
    skipped = {} if skipped is None else skipped
    log = log or logging.getLogger("rotor.mm.vl")
    token_to_iso = load_token_to_iso()
    # Reuse one source adapter per provider name to avoid duplicate HTTP clients.
    source_cache = {}
    runtimes: list[PairRuntime] = []
    for pair in config.pairs:
        info = client.markets_by_symbol.get(pair.market)
        if info is None:
            skipped[pair.market] = _unlisted_reason(pair.market, client.markets_by_symbol)
            log.warning("not quoting %s: %s", pair.market, skipped[pair.market])
            continue
        # Reference providers work in ISO symbols, not Sera token symbols.
        try:
            iso_base, iso_quote = market_to_iso_pair(pair.market, token_to_iso)
        except ValueError as exc:
            skipped[pair.market] = str(exc)
            log.warning("not quoting %s: %s", pair.market, exc)
            continue
        if pair.price_source not in source_cache:
            source_cache[pair.price_source] = load_price_reference_source(pair.price_source)
        source = source_cache[pair.price_source]
        # Every pair, whatever its mid source, quotes at Wise's after-fee prices.
        if "wise" not in source_cache:
            source_cache["wise"] = load_price_reference_source("wise")
        runtimes.append(
            PairRuntime(
                config=pair,
                info=info,
                oracle=ReferencePriceOracle(
                    source, max_age_s=max_reference_age_s, carry_stale=carry_stale),
                iso_base=iso_base,
                iso_quote=iso_quote,
                settlement=source_cache["wise"],
            )
        )
    if not runtimes:
        raise ValueError(
            "no configured pair can be quoted: "
            + "; ".join(f"{market} ({why})" for market, why in skipped.items())
        )
    return runtimes


def _unlisted_reason(market: str, markets_by_symbol: dict) -> str:
    """Explain why a configured market is not quoted, hinting at its inverse."""
    base, quote = market.split("/", 1)
    inverse = f"{quote}/{base}"
    if inverse in markets_by_symbol:
        return f"Sera lists this pair as {inverse}; configure it that way"
    return "Sera does not list this market"


def _validate_budget_caps(
    groups: list[tuple[str, list[OrderIntent], bool]],
    config: VLConfig,
) -> None:
    """Reject when a budgeted token's placements could spend more than its budget.

    Spend is counted the way Sera locks it. A VL batch's legs share one budget
    and only one can fill at a time, so Sera freezes just its largest leg; the
    batch counts as that leg. Each standalone order counts in full, and several
    batches or standalone orders on one token add up.
    """
    budgets = {budget.token: budget.amount for budget in config.budgets}
    spend_by_token: dict[str, Decimal] = {}
    for spent_token, intents, use_vl in groups:
        if spent_token not in budgets:
            # An unbudgeted token has no cap to check.
            continue
        spends = [_intent_spend_amount(intent) for intent in intents]
        group_spend = max(spends) if use_vl else sum(spends, Decimal("0"))
        spend_by_token[spent_token] = (
            spend_by_token.get(spent_token, Decimal("0")) + group_spend
        )
    for token, spend in spend_by_token.items():
        budget = budgets[token]
        if spend > budget:
            raise ValueError(
                f"{token} orders could spend {fmt(spend)}, "
                f"above configured budget {fmt(budget)}"
            )


def _intent_spend_amount(intent: OrderIntent) -> Decimal:
    """Return the amount of spent_token consumed by an intent."""
    # Bids spend quote notional: base quantity times quote-per-base price.
    if intent.side == "bid":
        return intent.qty_base * intent.price
    # Asks spend/sell base quantity.
    return intent.qty_base


def _build_tick_intents(
    config: VLConfig,
    runtimes: list[PairRuntime],
    positions: dict[str, Position],
    *,
    expiration: int,
    log: logging.Logger,
    refs: TickReferences | None = None,
    now: float | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
    unreadable: set[str] | frozenset[str] = frozenset(),
    funds: dict[str, Decimal] | None = None,
) -> list[OrderIntent]:
    """Build all order intents for one loop tick, leaving out `skip` markets.

    Markets in `unreadable` (fills that could not be read) are left out too,
    and recorded in `refs.skipped` so the not-quoting alert covers them.

    Each pair is isolated: a pair Wise cannot price (its own reference failing
    falls back to Wise's mid), or a quote that fails market minimums, skips
    only that pair, so one
    misbehaving market cannot sideline quoting on every other healthy market.
    When `refs` is given it records what each pair quoted on or why it skipped.
    """
    now = time.time() if now is None else now
    budgets = {budget.token: budget.amount for budget in config.budgets}
    out: list[OrderIntent] = []
    for runtime in runtimes:
        if runtime.config.market in skip:
            continue
        if runtime.config.market in unreadable:
            if refs is not None:
                refs.skipped[runtime.config.market] = RuntimeError("its fills could not be read")
            continue
        try:
            out.extend(
                _intents_for_runtime(
                    runtime,
                    config,
                    positions.get(runtime.config.market),
                    expiration=expiration,
                    log=log,
                    refs=refs,
                    now=now,
                    budgets=budgets,
                    funds=funds,
                )
            )
        except Exception as exc:
            log.warning("skip pair %s: %s", runtime.config.market, exc)
            if refs is not None:
                refs.skipped[runtime.config.market] = exc
    return out


def _intents_for_runtime(
    runtime: PairRuntime,
    config: VLConfig,
    position: Position | None,
    *,
    expiration: int,
    log: logging.Logger,
    refs: TickReferences | None = None,
    now: float,
    budgets: dict[str, Decimal],
    funds: dict[str, Decimal] | None,
) -> list[OrderIntent]:
    """Build order intents for a single configured pair."""
    # The mid: the pair's own source while fresh, then Wise's live mid, and a
    # carried daily rate (weekend, holiday) only while Wise has no mid either.
    try:
        ref = runtime.oracle.get(
            runtime.iso_base, runtime.iso_quote, amount=runtime.config.qty_base
        )
    except ReferenceUnavailable as exc:
        # A source that is down, or past its carry limit, is replaced by Wise's
        # live mid rather than taking the pair down.
        ref = _wise_mid(runtime, exc, now=now)
        if refs is not None:
            refs.fallback[runtime.config.market] = exc
    else:
        if ref.stale:
            try:
                ref = _wise_mid(runtime, ReferenceUnavailable("carried rate"), now=now)
            except ReferenceUnavailable:
                pass
            else:
                if refs is not None:
                    refs.covered.add(runtime.config.market)
    log.debug(
        "%s mid=%s src=%s qty=%s%s",
        runtime.config.market,
        fmt(ref.rate),
        ref.source,
        fmt(runtime.config.qty_base),
        f" stale_age={ref.age_s:.0f}s" if ref.stale else "",
    )
    # One bid and one ask of the configured size, priced at Wise's after-fee
    # rates for that size (or around the mid where Wise cannot price a side).
    intents = _normal_intents(runtime, ref.rate, expiration)
    intents = _at_wise_prices(intents, runtime, ref, now=now, log=log, refs=refs)
    if position is not None:
        intents = _with_return_order(
            intents, runtime, position, config, mid=ref.rate, now=now, log=log)
    intents = _within_limits(intents, runtime.info, budgets, funds, log)
    # Sizes are final now; Wise's fees fall with size, so price them again
    # (a reply for an unchanged size is reused, not re-requested).
    intents = _at_wise_prices(intents, runtime, ref, now=now, log=log, refs=refs)
    intents = _without_self_cross(intents, log)
    if refs is not None:
        refs.quoted[runtime.config.market] = ref
    return intents


def _without_self_cross(intents: list[OrderIntent], log: logging.Logger) -> list[OrderIntent]:
    """Drop a normal order that would trade against this pair's own return order.

    The return order is priced from the mid and its fill price, the normal order
    from Wise's live rate, so once the market moves away from a daily mid the two
    can meet. Sera would refuse the later one as a self-trade, and with it any VL
    batch it belongs to. Holding a position, the return order is the one to keep.
    """
    returning = next((intent for intent in intents if intent.is_return), None)
    if returning is None:
        return intents
    out: list[OrderIntent] = []
    for intent in intents:
        crosses = not intent.is_return and (
            intent.price >= returning.price if returning.side == "ask"
            else intent.price <= returning.price
        )
        if crosses:
            log.info(
                "%s %s %s left out: it would cross the return %s at %s",
                intent.market, intent.side, fmt(intent.price), returning.side,
                fmt(returning.price),
            )
            continue
        out.append(intent)
    return out


def _normal_intents(runtime: PairRuntime, mid: Decimal, expiration: int) -> list[OrderIntent]:
    """One bid and one ask of the pair's size; prices are set at Wise's rates next.

    A size below the market's minimum (checked at mid) skips the pair.
    """
    info = runtime.info
    qty = quantize_qty(info, runtime.config.qty_base)
    validate_minimums(info, "bid", qty, mid)
    validate_minimums(info, "ask", qty, mid)
    return [
        OrderIntent(
            market=runtime.config.market,
            side="bid",
            qty_base=qty,
            price=mid,
            expiration=expiration,
            spent_token=info.quote_symbol.upper(),
        ),
        OrderIntent(
            market=runtime.config.market,
            side="ask",
            qty_base=qty,
            price=mid,
            expiration=expiration,
            spent_token=info.base_symbol.upper(),
        ),
    ]


def _read_funds(client: SeraClient, log: logging.Logger) -> dict[str, Decimal] | None:
    """Read the wallet's vault balances, or None (budgets only) if that fails."""
    try:
        return client.get_vault_balances()
    except Exception as exc:
        log.warning("could not read vault balances; sizing to budgets only: %s", exc)
        return None


def _spend_limit(
    token: str,
    budgets: dict[str, Decimal],
    funds: dict[str, Decimal] | None,
) -> tuple[Decimal, str] | None:
    """Most one order may spend of `token`, and what sets it; None if unlimited.

    A budgeted token is capped by its budget. When the wallet's vault balances
    are known, every token is also capped by what the vault holds, so a token
    the wallet does not hold has a limit of zero.
    """
    budget = budgets.get(token)
    if funds is None:
        return None if budget is None else (budget, "budget")
    held = funds.get(token, Decimal("0"))
    if budget is not None and budget <= held:
        return budget, "budget"
    return held, "balance"


def _within_limits(
    intents: list[OrderIntent],
    info,
    budgets: dict[str, Decimal],
    funds: dict[str, Decimal] | None,
    log: logging.Logger,
) -> list[OrderIntent]:
    """Fit each order to what it may spend: its token's budget and vault balance.

    Sizes are fixed in base tokens, so a bid's quote spend rises with the price.
    An order over its limit is cut to what the limit buys, rounded down to the
    market's size step, instead of failing the tick's budget check or being
    rejected by Sera. An order on a token the wallet does not hold is left out,
    and one that trimming takes below the market minimum is dropped.
    """
    out: list[OrderIntent] = []
    for intent in intents:
        cap = _spend_limit(intent.spent_token, budgets, funds)
        if cap is None:
            out.append(intent)
            continue
        limit, source = cap
        if _intent_spend_amount(intent) <= limit:
            out.append(intent)
            continue
        if limit <= 0:
            log.debug(
                "%s %s left out: the wallet holds no %s",
                intent.market, intent.side, intent.spent_token,
            )
            continue
        # A bid spends quote tokens (qty x price); an ask spends base tokens (qty).
        qty = quantize_qty(info, limit if intent.side == "ask" else limit / intent.price)
        try:
            validate_minimums(info, intent.side, qty, intent.price)
        except ValueError as exc:
            log.warning(
                "%s %s dropped: trimmed to its %s %s it is too small (%s)",
                intent.market, intent.side, intent.spent_token, source, exc,
            )
            continue
        log.debug(
            "%s %s trimmed from %s to %s to fit the %s %s",
            intent.market, intent.side, fmt(intent.qty_base), fmt(qty),
            intent.spent_token, source,
        )
        out.append(replace(intent, qty_base=qty))
    return out


def _wise_mid(runtime: PairRuntime, reason: Exception, *, now: float) -> ReferencePrice:
    """Wise's mid-market rate for a pair whose own source failed with `reason`."""
    wise = runtime.settlement
    pair = f"{runtime.iso_base}/{runtime.iso_quote}"
    try:
        obs = wise.mid_rate(runtime.iso_base, runtime.iso_quote, runtime.config.qty_base)
    except Exception as exc:
        raise ReferenceUnavailable(
            f"{reason}; Wise's mid failed too ({type(exc).__name__}: {exc})") from exc
    age = now - obs.ts
    if age > wise.default_max_age_s:
        raise ReferenceUnavailable(f"{reason}; Wise's mid is {age:.0f}s old too")
    return ReferencePrice(pair=pair, rate=obs.rate, ts=obs.ts, source=obs.source, age_s=age)


def _fx_weekend(now: float) -> bool:
    """Whether `now` falls in the FX weekend: Friday 21:00 to Sunday 21:00 UTC."""
    t = time.gmtime(now)
    hour_of_week = t.tm_wday * 24 + t.tm_hour  # Monday 00:00 is hour 0.
    return 4 * 24 + 21 <= hour_of_week < 6 * 24 + 21


def _at_wise_prices(
    intents: list[OrderIntent],
    runtime: PairRuntime,
    mid: ReferencePrice,
    *,
    now: float,
    log: logging.Logger,
    refs: TickReferences | None = None,
) -> list[OrderIntent]:
    """Price each normal order at what converting its own size through Wise costs.

    A bid is placed at Wise's after-fee rate for converting its size of base
    into quote, and an ask at Wise's after-fee cost of buying its size of base:
    a taker gets what converting through Wise would give. Wise's rates follow
    its live mid, so the quotes do too, whatever the daily reference says. Fees
    fall with size, so this runs again once sizes are final (after budget and
    balance trimming). Return orders keep their auction price: they walk back
    to the fill price, which Wise's rate would stop them reaching. A bid that
    falls below the market's minimum notional is dropped.

    Where Wise cannot price a side (it offers no such route, it is
    unreachable, or its rate is older than its freshness window), that order
    sits at the mid +/- the pair's `fallback_bps`, or `fallback_weekend_bps`
    on an FX weekend, on a carried mid, or while Wise is unreachable.
    """
    wise = runtime.settlement
    info = runtime.info
    pair = runtime.config
    out: list[OrderIntent] = []
    for intent in intents:
        if intent.is_return:
            out.append(intent)
            continue
        fetch = wise.sell_rate if intent.side == "bid" else wise.buy_rate
        fallback = None
        try:
            obs = fetch(runtime.iso_base, runtime.iso_quote, intent.qty_base)
            age = now - obs.ts
            if age > wise.default_max_age_s:
                raise ReferenceUnavailable(f"Wise's rate is {age:.0f}s old")
            price = quantize_price(info, obs.rate, intent.side)
        except Exception as exc:
            outage = not isinstance(exc, WiseNoRoute)
            wide = outage or mid.stale or _fx_weekend(now)
            bps = pair.fallback_weekend_bps if wide else pair.fallback_bps
            sign = -1 if intent.side == "bid" else 1
            price = quantize_price(info, mid.rate * (1 + sign * bps / BPS), intent.side)
            if outage and refs is not None:
                refs.wise_outage.setdefault(intent.market, exc)
            fallback = f"mid {fmt(mid.rate)} {'-' if sign < 0 else '+'} {fmt(bps)} bps: " \
                f"Wise cannot price it ({type(exc).__name__}: {exc})"
        if price == intent.price:
            # Unchanged since the first pass (already logged there).
            out.append(intent)
            continue
        if fallback is not None:
            log.info("%s %s at %s", intent.market, intent.side, fallback)
        try:
            validate_minimums(info, intent.side, intent.qty_base, price)
        except ValueError as exc:
            log.warning(
                "%s %s dropped: at its price it is too small (%s)",
                intent.market, intent.side, exc,
            )
            continue
        log.debug(
            "%s %s priced at %s for %s (was %s)",
            intent.market, intent.side, fmt(price), fmt(intent.qty_base), fmt(intent.price),
        )
        out.append(replace(intent, price=price))
    return out


def _with_return_order(
    intents: list[OrderIntent],
    runtime: PairRuntime,
    position: Position,
    config: VLConfig,
    *,
    mid: Decimal,
    now: float,
    log: logging.Logger,
) -> list[OrderIntent]:
    """Swap the normal order on a position's return side for its return order.

    The return order never spends more than the normal order it replaces, so
    budgets and the per-tick budget guard see no new spend; a larger position is
    returned over several fills. A position too small to meet the market minimum,
    or smaller than RETURN_MIN_SHARE of the normal order, keeps the normal order
    instead, and the next fill on that side absorbs it.
    """
    side = position.return_side
    normal = next((intent for intent in intents if intent.side == side), None)
    if normal is None:
        return intents
    if abs(position.net_base) < RETURN_MIN_SHARE * normal.qty_base:
        log.debug(
            "%s position %s is under %s of the normal %s %s; quoting normally",
            runtime.config.market, fmt(position.net_base), RETURN_MIN_SHARE,
            side, fmt(normal.qty_base),
        )
        return intents
    info = runtime.info
    target = return_price(
        position,
        mid,
        fixed_bps=runtime.config.fixed_bps,
        return_bps=config.return_bps,
        now=now,
        window_seconds=float(config.return_window_hours) * 3600.0,
    )
    # Bids round down and asks round up, so rounding never crosses the fill price.
    price = quantize_price(info, target, side)
    if price <= 0:
        return intents
    # A bid spends quote tokens (qty x price); an ask spends base tokens (qty).
    limit = normal.qty_base if side == "ask" else normal.qty_base * normal.price / price
    qty = quantize_qty(info, min(abs(position.net_base), limit))
    try:
        validate_minimums(info, side, qty, price)
    except ValueError as exc:
        log.debug(
            "%s position %s too small to return alone (%s); quoting normally",
            runtime.config.market,
            fmt(position.net_base),
            exc,
        )
        return intents
    log.debug(
        "%s return %s qty=%s price=%s position=%s@%s",
        runtime.config.market,
        side,
        fmt(qty),
        fmt(price),
        fmt(position.net_base),
        fmt(position.price),
    )
    returning = replace(normal, qty_base=qty, price=price, is_return=True)
    return [returning if intent is normal else intent for intent in intents]


@dataclass
class PlacementIssues:
    """What one tick's placement learned about Sera's markets."""

    # Markets Sera rejected as not active.
    inactive: set[str] = field(default_factory=set)
    # Whether a rejection suggests stale grids, minimums, or batch limits.
    stale_metadata: bool = False
    # Placements sent without a usable answer (a timeout, a server error, an
    # unreadable reply). Sera may have booked them, so they are tracked for
    # cancellation, and their orders stay in the ledger.
    unanswered: list[LivePlacement] = field(default_factory=list)
    # market -> the refusal of an order or batch that held one of its orders.
    refused: dict[str, Exception] = field(default_factory=dict)

    def note(self, exc: Exception) -> None:
        if isinstance(exc, errors.SeraError) and (
            exc.status_code == 422 or exc.error_code == "AMOUNT_BELOW_MIN"
        ):
            self.stale_metadata = True


def _update_dark_pairs(
    dark_pairs: DarkPairAlerts,
    refs: TickReferences,
    sent: set[str],
    issues: PlacementIssues,
    *,
    notifier: TelegramNotifier | None,
    log: logging.Logger,
) -> None:
    """Advance the not-quoting alerts from what this tick actually placed."""
    _not_quoting(refs, sent, issues)
    # An alert failure must never cost a tick, so it is only logged.
    try:
        dark_pairs.update(refs, notifier=notifier, log=log)
    except Exception as exc:
        log.warning("not-quoting alert failed: %s", exc)


def _not_quoting(refs: TickReferences, sent: set[str], issues: PlacementIssues) -> None:
    """Move pairs that placed nothing this tick from `quoted` to `skipped`.

    A pair counts as quoting only if one of its orders went out (or may have:
    unanswered). One whose orders Sera refuses every tick, or that had no order
    to place, is not quoting even while the other pairs are. Markets Sera
    called inactive have their own alert and are left as they are.
    """
    for market in list(refs.quoted):
        if market in sent or market in issues.inactive:
            continue
        refs.skipped[market] = issues.refused.get(market) or RuntimeError(
            "no order to place (no funds for it, or under the market minimum)")
        del refs.quoted[market]


def _inactive_market(exc: Exception, intents: list[OrderIntent]) -> str | None:
    """The market among `intents` that a placement error says is not active."""
    if not isinstance(exc, errors.SeraError):
        return None
    markets = {intent.market for intent in intents}
    # A single order gets the PAIR_INACTIVE code; a VL batch gets Sera's
    # message, which names the pair as BASE/QUOTE like rotor's market symbols.
    if exc.error_code == "PAIR_INACTIVE" and len(markets) == 1:
        return next(iter(markets))
    match = _INACTIVE_PAIR.search(exc.detail or "")
    named = match.group(1).upper() if match else None
    return next((market for market in markets if market.upper() == named), None)


def _place_groups(
    client: SeraClient,
    groups: list[tuple[str, list[OrderIntent], bool]],
    log: logging.Logger,
    *,
    state: StateStore | None = None,
    issues: PlacementIssues | None = None,
) -> list[LivePlacement]:
    """Place grouped intents and return the placements Sera accepted.

    Each VL batch and each standalone order is placed on its own. One that Sera
    rejects (an unfunded order, a rate limit) is logged and skipped; the rest
    still go out and are tracked. A VL batch rejected because one of its
    markets is not active is re-sent without that market. With `state`, every
    order is recorded for fill settlement before it is sent, and removed again
    only if Sera refuses it. `issues` collects what placement learned,
    including placements sent without a usable answer.
    """
    issues = PlacementIssues() if issues is None else issues
    placed: list[LivePlacement] = []
    for spent_token, intents, use_vl in groups:
        if use_vl:
            # Whatever the batch had to shed below Sera's minimum goes out alone.
            intents = _place_vl_batch(
                client, spent_token, intents, placed, log, state=state, issues=issues)
        for intent in intents:
            _place_standalone(client, intent, placed, log, state=state, issues=issues)
    return placed


def _place_vl_batch(
    client: SeraClient,
    spent_token: str,
    intents: list[OrderIntent],
    placed: list[LivePlacement],
    log: logging.Logger,
    *,
    state: StateStore | None,
    issues: PlacementIssues,
) -> list[OrderIntent]:
    """Place one VL batch, dropping any leg Sera says is on an inactive market.

    Returns the legs left over when dropping takes the batch below Sera's
    minimum batch size; the caller places those as standalone orders.
    """
    legs = list(intents)
    while len(legs) >= client.vl_batch_min:
        batch = client.sign_vl_batch([
            VLLeg(
                market=i.market,
                side=i.side,
                qty_base=i.qty_base,
                price=i.price,
                expiration=i.expiration,
            )
            for i in legs
        ])
        _record_before_sending(state, batch.orders, legs)
        try:
            result = client.send_vl_batch(batch)
            placement = _placement_from_vl_result(spent_token, result, legs)
        except Exception as exc:
            if not _refused(exc):
                log.error(
                    "VL batch %s spent=%s legs=%d got no usable answer (%s); "
                    "tracking it in case Sera booked it",
                    batch.vl_batch_id, spent_token, len(legs), exc,
                )
                issues.unanswered.append(_unanswered_placement(
                    "vl", spent_token, batch.orders, legs, vl_batch_id=batch.vl_batch_id))
                return []
            if state is not None:
                state.remove_unsettled([order.order_id for order in batch.orders])
            issues.note(exc)
            inactive = _inactive_market(exc, legs)
            if inactive is None:
                for leg in legs:
                    issues.refused.setdefault(leg.market, exc)
                log.error(
                    "could not place VL batch spent=%s legs=%d: %s",
                    spent_token, len(legs), exc,
                )
                return []
            issues.inactive.add(inactive)
            log.warning(
                "%s is not active on Sera; sending the %s batch without it",
                inactive, spent_token,
            )
            legs = [leg for leg in legs if leg.market != inactive]
            continue
        placed.append(placement)
        log.debug(
            "placed VL batch %s spent=%s legs=%d amendments=%d cancelled=%d",
            result.vl_batch_id,
            spent_token,
            len(result.records),
            len(result.amendments),
            len(result.cancelled),
        )
        return []
    return legs


def _place_standalone(
    client: SeraClient,
    intent: OrderIntent,
    placed: list[LivePlacement],
    log: logging.Logger,
    *,
    state: StateStore | None,
    issues: PlacementIssues,
) -> None:
    """Place one ordinary order, logging and skipping it if Sera rejects it."""
    order = client.sign_order(
        intent.market,
        intent.side,
        intent.qty_base,
        intent.price,
        intent.expiration,
    )
    _record_before_sending(state, [order], [intent])
    try:
        record = client.send_order(order)
    except Exception as exc:
        if not _refused(exc):
            log.error(
                "%s %s order %s got no usable answer (%s); tracking it in case Sera booked it",
                intent.market, intent.side, order.order_id, exc,
            )
            issues.unanswered.append(_unanswered_placement(
                "standalone", intent.spent_token, [order], [intent]))
            return
        if state is not None:
            state.remove_unsettled([order.order_id])
        issues.note(exc)
        inactive = _inactive_market(exc, [intent])
        if inactive is not None:
            issues.inactive.add(inactive)
        else:
            issues.refused.setdefault(intent.market, exc)
        log.error(
            "could not place %s %s spent=%s: %s",
            intent.market,
            intent.side,
            intent.spent_token,
            exc,
        )
        return
    placement = LivePlacement(
        kind="standalone",
        spent_token=intent.spent_token,
        orders=[_live_from_record(
            intent.market,
            intent.side,
            record,
            qty_base=intent.qty_base,
        )],
        placed_at=time.time(),
    )
    placed.append(placement)
    log.debug(
        "placed standalone %s %s side=%s spent=%s",
        intent.market,
        record.order_id,
        intent.side,
        intent.spent_token,
    )


def _refused(exc: Exception) -> bool:
    """Whether Sera answered a placement with a 4xx refusal, so booked nothing.

    Anything else (a timeout, a dropped connection, a server error, a reply
    rotor cannot read) leaves open whether the order is live on Sera.
    """
    return isinstance(exc, errors.SeraError) and 400 <= exc.status_code < 500


def _record_before_sending(
    state: StateStore | None,
    orders: list[SignedOrder],
    intents: list[OrderIntent],
) -> None:
    """Put signed orders in the settlement ledger before they are sent.

    Their ids are final once signed, so a fill on any order Sera books is
    counted even if its answer is lost. An order Sera never books has no
    fills and settles at zero after its expiry.
    """
    if state is None:
        return
    now = time.time()
    state.add_unsettled([
        UnsettledOrder(
            order_id=order.order_id,
            market=intent.market,
            side=intent.side,
            price=intent.price,
            expiration=intent.expiration,
            placed_at=now,
        )
        for order, intent in zip(orders, intents, strict=True)
    ])


def _unanswered_placement(
    kind: str,
    spent_token: str,
    orders: list[SignedOrder],
    intents: list[OrderIntent],
    *,
    vl_batch_id: str | None = None,
) -> LivePlacement:
    """Track a placement Sera may have booked, so the next cycle cancels it."""
    now = time.time()
    return LivePlacement(
        kind=kind,
        spent_token=spent_token,
        orders=[
            LiveOrder(
                side=intent.side,
                order_id=order.order_id,
                uuid_int=order.uuid_int,
                placed_at=now,
                market=intent.market,
                qty_base=intent.qty_base,
            )
            for order, intent in zip(orders, intents, strict=True)
        ],
        placed_at=now,
        vl_batch_id=vl_batch_id,
    )


def _placement_from_vl_result(
    spent_token: str,
    result: VLBatchResult,
    intents: list[OrderIntent],
) -> LivePlacement:
    """Convert Sera's VL batch result into local live-placement state."""
    # Use one timestamp for every sibling so cooldown checks are consistent.
    now = time.time()
    # Legs Sera dropped at placement never rest; do not track them as live.
    cancelled_ids = set(result.cancelled)
    # Sera may clip a leg's size; track the accepted (amended) amount so a full
    # fill at the clipped size is recognized and pruned.
    amended = {
        a.order_id: Decimal(str(a.actual_amount))
        for a in result.amendments
        if a.order_id and a.actual_amount not in (None, "")
    }
    orders: list[LiveOrder] = []
    for record, intent in zip(result.records, intents, strict=True):
        if record.order_id in cancelled_ids:
            continue
        orders.append(
            LiveOrder(
                side=intent.side,
                order_id=record.order_id,
                uuid_int=record.uuid_int,
                placed_at=now,
                market=intent.market,
                qty_base=amended.get(record.order_id, intent.qty_base),
            )
        )
    return LivePlacement(
        kind="vl",
        spent_token=spent_token,
        orders=orders,
        placed_at=now,
        vl_batch_id=result.vl_batch_id,
    )


def _cancel_placements(
    client: SeraClient,
    placements: list[LivePlacement],
    log: logging.Logger,
) -> list[LivePlacement]:
    """Cancel live placements and return placements that remain live."""
    remaining: list[LivePlacement] = []
    for placement in placements:
        if placement.kind == "vl" and placement.vl_batch_id:
            # VL batches are cancelled by batch id instead of each child.
            batch_id = placement.vl_batch_id
            cancel = lambda batch_id=batch_id: client.cancel_vl_batch(batch_id)  # noqa: E731
            if not _cancel_one(cancel, f"VL batch {batch_id}", log):
                remaining.append(placement)
            continue
        # Standalone placements cancel each live order separately; only the
        # orders still live stay tracked.
        leftover = [
            order for order in placement.orders
            if not _cancel_one(
                lambda order=order: client.cancel_order(order.order_id, order.uuid_int),
                f"{order.side} order {order.order_id}",
                log,
            )
        ]
        if leftover:
            placement.orders = leftover
            remaining.append(placement)
    return remaining


def _cancel_one(cancel: Callable[[], bool], what: str, log: logging.Logger) -> bool:
    """Cancel one order or batch; return whether it no longer needs tracking.

    Gone on Sera (expired, filled, unknown) counts as done, so a stuck cancel
    cannot wedge the cancel-first loop forever. A cooldown or any other
    (retryable) error keeps it tracked for the next try.
    """
    try:
        ok = cancel()
    except Exception as exc:
        if _is_order_gone(exc):
            log.warning("%s already gone; dropping: %s", what, exc)
            return True
        log.warning("cancel %s failed: %s", what, exc)
        return False
    log.debug("%s %s", "cancelled" if ok else "cancel cooldown", what)
    return ok


def _is_order_gone(exc: Exception) -> bool:
    """Return True when a cancel error means the order no longer exists on Sera.

    A 404 (or a typed code about a missing/already-resolved order) means the
    order expired, filled, or was already cancelled — retaining it would block
    all future quoting. Genuinely retryable errors (5xx, network, cooldown) are
    NOT treated as gone, so a maybe-live order is never dropped prematurely.
    """
    if not isinstance(exc, errors.SeraError):
        return False
    if exc.status_code == 404:
        return True
    # Sera answers a cancel for an order or batch it does not know with a 400
    # that says so: "Order not found for cancellation:..." or "VL batch not
    # found: ...". An unanswered placement Sera never booked ends up here.
    if exc.status_code == 400 and "not found" in (exc.detail or "").lower():
        return True
    code = (exc.error_code or "").upper()
    return any(
        marker in code
        for marker in ("NOT_FOUND", "ALREADY_CANCEL", "ALREADY_FILLED", "ORDER_GONE", "UNKNOWN_ORDER")
    )


def _load_live_placements(
    client: SeraClient,
    markets: set[str],
    log: logging.Logger,
    *,
    announce: bool = True,
    fallback: list[LivePlacement] | None = None,
    retries: int = 2,
    retry_wait: float = 2.0,
    stop: StopFlag,
    known: set[str] | None = None,
    foreign: list[str] | None = None,
) -> list[LivePlacement]:
    """Reattach open orders from Sera for configured markets.

    A transient reattach failure is retried before falling back to local state:
    if orders were placed just before a crash and the boot reattach blips, a bare
    fallback to empty local state would orphan them (live on Sera, untracked).

    With `known` (the order ids this rotor's state and ledger hold), only those
    are reattached: an open order rotor did not place belongs to someone else
    on this wallet (another rotor, say), and reattaching would cancel it. Its id
    goes into `foreign` instead.
    """
    rows = None
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            rows = client.get_open_orders()
            break
        except Exception as exc:
            last_exc = exc
            if attempt < retries and stop.wait(retry_wait):
                break
    if rows is None:
        log.warning(
            "could not reattach open VL orders after %d attempt(s): %s",
            retries + 1,
            last_exc,
        )
        # Preserve caller fallback so transient API failure does not orphan state.
        return list(fallback or [])

    standalone: list[LivePlacement] = []
    batches: dict[str, list[LiveOrder]] = {}
    for row in rows:
        market = str(row.get("symbol") or row.get("market") or "").upper()
        if market not in markets:
            continue
        # Accept only bid/ask orders with enough ids to cancel later.
        side = str(row.get("side") or "").lower()
        order_id = row.get("trade_id") or row.get("order_id")
        uuid_int = row.get("uuid_int")
        if side not in ("bid", "ask") or not order_id or not uuid_int:
            continue
        if known is not None and str(order_id) not in known:
            if foreign is not None:
                foreign.append(str(order_id))
            continue
        # Preserve server creation time when available for cooldown math.
        live_order = LiveOrder(
            side=side,
            order_id=str(order_id),
            uuid_int=int(uuid_int),
            placed_at=parse_ts(row.get("created_at")) or time.time(),
            market=market,
            qty_base=_open_order_qty_base(row),
        )
        vl_batch_id = str(row.get("vl_batch_id") or "").strip()
        if vl_batch_id:
            batches.setdefault(vl_batch_id, []).append(live_order)
        else:
            standalone.append(
                LivePlacement(
                    kind="standalone",
                    spent_token="",
                    orders=[live_order],
                    placed_at=live_order.placed_at,
                )
            )

    out = list(standalone)
    for vl_batch_id, orders in batches.items():
        # Batch placed_at is the oldest sibling time for cooldown purposes.
        out.append(
            LivePlacement(
                kind="vl",
                spent_token="",
                orders=orders,
                placed_at=min(o.placed_at for o in orders),
                vl_batch_id=vl_batch_id,
            )
        )
    # Reattached placements are tracked for cancellation only. Fills are settled
    # from the unsettled_orders ledger, which recorded each order when placed.
    if out and announce:
        log.debug("reattached %d open placement(s)", len(out))
    return out


def _wait_until_gone(
    client: SeraClient,
    order_ids: set[str],
    log: logging.Logger,
    *,
    stop: StopFlag,
    polls: int = CANCEL_SETTLE_POLLS,
    interval: float = CANCEL_SETTLE_INTERVAL_SECONDS,
) -> None:
    """Wait (briefly) until cancelled orders no longer show as open on Sera.

    Sera acknowledges a cancel before applying it, and a VL batch keeps its
    budget frozen until then. Earn wallets hold exactly their budget, so new
    quotes placed at once can be refused for lack of funds. After `polls`
    reads rotor places anyway; a refusal then affects only that placement.
    """
    for _ in range(polls):
        try:
            open_ids = {
                str(row.get("trade_id") or row.get("order_id"))
                for row in client.get_open_orders()
            }
        except Exception as exc:
            log.debug("could not confirm cancels before placing: %s", exc)
            return
        if not open_ids & order_ids:
            return
        if stop.wait(interval):
            return
    log.debug("%d cancelled order(s) still open; placing anyway", len(open_ids & order_ids))


def _network_name(client: SeraClient) -> str:
    """A readable name for the chain the client bootstrapped against."""
    chain = int((client.eip712_domain or {}).get("chainId") or 0)
    return {1: "Ethereum mainnet", 11155111: "Sepolia testnet"}.get(chain, f"chain {chain}")


def _known_order_ids(state: StateStore | None, stored: list[LivePlacement]) -> set[str]:
    """Order ids this rotor placed: its tracked orders and its settlement ledger.

    Every order is in the ledger from before it is sent until it has expired
    and settled, so an open order missing from both was not placed by rotor
    on this state file.
    """
    known = {order.order_id for placement in stored for order in placement.orders}
    if state is not None:
        known |= {order.order_id for order in state.load_unsettled()}
    return known


def _foreign_orders_message(client: SeraClient, foreign: list[str]) -> str:
    shown = ", ".join(foreign[:3]) + (", ..." if len(foreign) > 3 else "")
    return (
        f"wallet {client.owner_address} has {len(foreign)} open order(s) on this "
        f"rotor's markets that it did not place ({shown}): another rotor, or "
        "someone else, is trading this wallet. Run one rotor per wallet; rotor "
        "leaves those orders alone. Stop the other trader or cancel them (they "
        "also expire with their TTL), then start again."
    )


def _read_fills(
    client: SeraClient, order_ids: list[str]
) -> tuple[dict[str, list[dict]], dict[str, Exception], set[str]]:
    """Fetch each order's fills once: the fills, any failures, and unknown orders.

    An order Sera itself says it has no record of has no fills, and is also
    listed as unknown. An order whose fills cannot be read, including a 404
    that is not Sera's own answer (an edge or proxy error page), or with a row
    rotor cannot parse, is returned with its error instead, so a bad read
    affects only that order's market and is retried next tick.
    """
    fills: dict[str, list[dict]] = {}
    failed: dict[str, Exception] = {}
    unknown: set[str] = set()
    for order_id in dict.fromkeys(order_ids):
        try:
            rows = client.get_order_fills(order_id)
            for row in rows:
                _filled_base_qty(row)
        except Exception as exc:
            if _is_unknown_order(exc):
                fills[order_id] = []
                unknown.add(order_id)
            else:
                failed[order_id] = exc
            continue
        fills[order_id] = rows
    return fills, failed, unknown


def _is_unknown_order(exc: Exception) -> bool:
    """Whether this is Sera's own answer for an order it has no record of.

    Sera answers GET /fills/{id} for an unknown order with a JSON 404 whose
    detail is "Order not found"; any other 404 is not evidence of no fills.
    """
    return (
        isinstance(exc, errors.SeraError)
        and exc.status_code == 404
        and (exc.detail or "").strip().lower() == "order not found"
    )


def _markets_of(
    order_ids: dict[str, Exception] | set[str],
    unsettled: list[UnsettledOrder],
    live: list[LivePlacement],
) -> set[str]:
    """Return the markets of the given ledger or live orders."""
    market_of = {order.order_id: order.market for order in unsettled}
    for placement in live:
        for order in placement.orders:
            market_of.setdefault(order.order_id, order.market)
    return {market_of[order_id] for order_id in order_ids if market_of.get(order_id)}


def _report_read_failures(
    failed: dict[str, Exception],
    unsettled: list[UnsettledOrder],
    live: list[LivePlacement],
    previous: dict[str, int],
    *,
    log: logging.Logger,
    notifier: TelegramNotifier,
) -> dict[str, int]:
    """Log this tick's unreadable orders and return their consecutive counts.

    Each tick logs one warning per affected market. A market whose orders stay
    unreadable for READ_FAILURE_ALERT_TICKS ticks in a row is reported once as
    an error (and through Telegram when enabled), since it will not quote until
    those fills can be read.
    """
    counts = {order_id: previous.get(order_id, 0) + 1 for order_id in failed}
    by_market: dict[str, list[str]] = {}
    for order_id in failed:
        market = next(iter(_markets_of({order_id}, unsettled, live)), "?")
        by_market.setdefault(market, []).append(order_id)
    for market, order_ids in sorted(by_market.items()):
        error = failed[order_ids[0]]
        log.warning(
            "could not read fills for %s order(s) %s; not quoting it this tick: %s",
            market, ",".join(order_ids), error,
        )
        if max(counts[order_id] for order_id in order_ids) == READ_FAILURE_ALERT_TICKS:
            log.error(
                "%s fills unreadable for %d ticks in a row; it is not quoting",
                market, READ_FAILURE_ALERT_TICKS,
            )
            notifier.notify_error(f"{market} not quoting", error)
    return counts


def _filled_quantities(fills: dict[str, list[dict]]) -> dict[str, Decimal]:
    """Return each order's filled base quantity across all of its fills."""
    return {
        order_id: sum((_filled_base_qty(fill) for fill in rows), Decimal("0"))
        for order_id, rows in fills.items()
    }


def _positions_for_quoting(
    state: StateStore,
    unsettled: list[UnsettledOrder],
    fills: dict[str, list[dict]],
    *,
    server_now: float,
    log: logging.Logger,
    unknown: set[str] = frozenset(),
    stuck: set[str] | None = None,
) -> dict[str, Position]:
    """Settle every final order into the stored positions, then add fills in flight.

    An order is final once its signed expiration (plus a short grace) has passed
    in Sera server time and none of its fills is still settling: nothing about it
    can change, so its settled quantity goes into the stored position and the
    order leaves the ledger in one transaction. That holds however the order left
    the book (cancelled, filled, expired, or orphaned by a restart), so no fill is
    missed or counted twice. Visible fills on every other order are added on top
    for quoting only, so a fill holds its return side from the first tick it shows.
    """
    in_flight: list[tuple[UnsettledOrder, Decimal]] = []
    for order in unsettled:
        if order.order_id not in fills:
            # Unreadable this tick: settle nothing (its market is not quoted).
            continue
        rows = fills[order.order_id]
        settled = _settled_fill_qty(rows)
        if settled is None or server_now < order.expiration + SETTLE_GRACE_SECONDS:
            in_flight.append((order, _visible_fill_qty(rows)))
            if (
                settled is None
                and stuck is not None
                and order.order_id not in stuck
                and server_now >= order.expiration + STUCK_SETTLEMENT_SECONDS
            ):
                stuck.add(order.order_id)
                log.warning(
                    "%s %s order %s: fills still settling %s after expiry; its "
                    "return side is held, but the fill is not stored until it settles",
                    order.market, order.side, order.order_id,
                    _fmt_duration(server_now - order.expiration),
                )
            continue
        before, after = state.settle_order(order, settled)
        if order.order_id in unknown:
            # Expected only for a placement whose answer was lost and that
            # Sera never booked; anything else is worth a look.
            log.warning(
                "%s %s order %s: Sera has no record of it; settled with no fills",
                order.market, order.side, order.order_id,
            )
        if settled > 0:
            log.info(
                "%s %s filled %s @ %s; position %s -> %s",
                order.market,
                order.side,
                fmt(settled),
                fmt(order.price),
                _position_text(before),
                _position_text(after),
            )
    positions = state.load_positions()
    for order, qty in in_flight:
        if qty <= 0:
            continue
        moved = apply_fill(
            positions.get(order.market), order.side, qty, order.price, order.placed_at)
        if moved is None:
            positions.pop(order.market, None)
        else:
            positions[order.market] = moved
    return positions


def _settled_fill_qty(fills: list[dict]) -> Decimal | None:
    """Return an order's settled base quantity, or None while a fill is unsettled.

    Failed and reverted fills moved no tokens and count as zero. A fill with no
    `settlement_status` counts as settled; any other status means wait.
    """
    total = Decimal("0")
    for fill in fills:
        status = _settlement_status(fill)
        if status == "settled":
            total += _filled_base_qty(fill)
        elif status not in ("failed", "reverted"):
            return None
    return total


def _visible_fill_qty(fills: list[dict]) -> Decimal:
    """Return the base quantity of every fill that has not failed or reverted."""
    return sum(
        (
            _filled_base_qty(fill)
            for fill in fills
            if _settlement_status(fill) not in ("failed", "reverted")
        ),
        Decimal("0"),
    )


def _settlement_status(fill: dict) -> str:
    """Return a fill's settlement status, treating a missing one as settled."""
    return str(fill.get("settlement_status") or "settled").lower()


def _position_text(position: Position | None) -> str:
    """Format a position for logs: flat, or signed base size @ breakeven."""
    if position is None:
        return "flat"
    return f"{fmt(position.net_base)} @ {fmt(position.price)}"


def _prune_fully_filled_placements(
    placements: list[LivePlacement],
    filled_by_order_id: dict[str, Decimal],
    log: logging.Logger,
) -> list[LivePlacement]:
    """Drop locally known fully filled orders from unresolved active state."""
    if not filled_by_order_id:
        return placements
    remaining: list[LivePlacement] = []
    for placement in placements:
        leftover: list[LiveOrder] = []
        for order in placement.orders:
            filled = filled_by_order_id.get(order.order_id, Decimal("0"))
            if order.qty_base is not None and filled >= order.qty_base:
                log.debug(
                    "pruned fully filled %s %s qty=%s",
                    order.side,
                    order.order_id,
                    fmt(filled),
                )
                continue
            leftover.append(order)
        if leftover:
            placement.orders = leftover
            remaining.append(placement)
    return remaining


def _filled_base_qty(fill: dict) -> Decimal:
    """Extract filled base quantity from known Sera fill response shapes."""
    # Some responses provide nested trade rows; sum their quantities.
    trades = fill.get("trades")
    if isinstance(trades, list):
        total = Decimal("0")
        for trade in trades:
            if isinstance(trade, dict):
                total += Decimal(str(trade.get("quantity", "0")))
        return total
    # Other responses expose a flat amount-like field.
    for key in ("filled_base_amount", "quantity", "amount"):
        if fill.get(key) is not None:
            return Decimal(str(fill[key]))
    # Missing amount is treated as zero and ignored by callers.
    return Decimal("0")


def _open_order_qty_base(row: dict) -> Decimal | None:
    """Extract an open order's base size when Sera exposes it."""
    for key in ("qty_base", "base_amount", "quantity", "amount"):
        value = row.get(key)
        if value is not None:
            return Decimal(str(value))
    return None


def _log_groups(
    groups: list[tuple[str, list[OrderIntent], bool]],
    log: logging.Logger,
    *,
    dry_run: bool,
) -> None:
    """Log the placement path and economics for every order intent.

    A dry run's whole output is these lines, so they are warnings there (seen
    at the default level); in live mode they are debug traces.
    """
    emit = log.warning if dry_run else log.debug
    for spent_token, intents, use_vl in groups:
        path = "VL" if use_vl else "standalone"
        for intent in intents:
            emit(
                "%sintent %s %s side=%s spent=%s price=%s qty=%s",
                "dry-run " if dry_run else "",
                path,
                intent.market,
                intent.side,
                spent_token,
                fmt(intent.price),
                fmt(intent.qty_base),
            )


def _live_from_record(
    market: str,
    side: str,
    record: OrderRecord,
    *,
    qty_base: Decimal | None = None,
) -> LiveOrder:
    """Convert a placed order record into local live-order state."""
    return LiveOrder(
        side=side,
        order_id=record.order_id,
        uuid_int=record.uuid_int,
        placed_at=time.time(),
        market=market,
        qty_base=qty_base,
    )
