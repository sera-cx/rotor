"""TOML config loading for multi-pair VL market making."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from rotor.price_reference import parse_market_symbol

PRICE_SOURCE_CHOICES = {"wise", "ecb", "bnm"}
# Spreads around the mid for a side Wise cannot price: on weekdays with a live
# or fresh mid, and on FX weekends, carried rates and Wise outages.
DEFAULT_FALLBACK_BPS = Decimal("50")
DEFAULT_FALLBACK_WEEKEND_BPS = Decimal("200")
# Keys each table may hold; anything else is a typo or a global setting written
# after a table header (TOML scoping), which would otherwise be ignored.
GLOBAL_KEYS = {
    "fixed_bps", "return_bps", "return_window_hours", "fallback_bps",
    "fallback_weekend_bps", "budget", "pair",
}
PAIR_KEYS = {
    "market", "price_source", "qty_base", "fixed_bps", "fallback_bps", "fallback_weekend_bps",
}


@dataclass(frozen=True)
class BudgetConfig:
    """Shared collateral budget for one spent token symbol."""

    # Token symbol whose spend is capped across VL siblings.
    token: str
    # Human-decimal maximum budget for the token.
    amount: Decimal


@dataclass(frozen=True)
class PairConfig:
    """Per-market VL quoting config."""

    # Sera market label, normalized to BASE/QUOTE.
    market: str
    price_sources: tuple[str, ...]
    # Base-token order size before quantization.
    qty_base: Decimal
    # Where a return order starts (2 x fixed_bps from the fill), possibly
    # inherited from global config.
    fixed_bps: Decimal
    # Spread around the mid for a side Wise cannot price, on weekdays.
    fallback_bps: Decimal = DEFAULT_FALLBACK_BPS
    # The same on FX weekends, on a carried rate, or while Wise is unreachable.
    fallback_weekend_bps: Decimal = DEFAULT_FALLBACK_WEEKEND_BPS


@dataclass(frozen=True)
class VLConfig:
    """Validated VL strategy config."""

    budgets: list[BudgetConfig]
    pairs: list[PairConfig]
    # Global defaults copied into pairs when pair-level values are absent.
    fixed_bps: Decimal
    # Closest a return order may quote to the latest source mid.
    return_bps: Decimal
    # Hours a return order takes to walk from 2 x fixed_bps away to the fill price.
    return_window_hours: Decimal = Decimal("48")

    @property
    def budget_tokens(self) -> set[str]:
        """Return the set of token symbols with configured VL budgets."""
        return {budget.token for budget in self.budgets}


def load_vl_config(path: str | Path) -> VLConfig:
    """Load and validate a VL TOML config file."""
    config_path = Path(path)
    # TOML files must be opened as bytes for tomllib.
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)
    # Keep all validation in parse_vl_config so tests can use dict input.
    return parse_vl_config(raw, source=str(config_path))


def parse_vl_config(raw: dict[str, Any], *, source: str = "<memory>") -> VLConfig:
    """Validate a parsed VL config dict."""
    unknown = sorted(set(raw) - GLOBAL_KEYS)
    if unknown:
        raise ValueError(f"{source}: unknown setting(s) {', '.join(unknown)}")
    # Global strategy defaults apply to each pair unless the pair overrides.
    fixed_bps = _bps(raw, "fixed_bps", Decimal("8"), source=source)
    fallback_bps = _bps(raw, "fallback_bps", DEFAULT_FALLBACK_BPS, source=source)
    fallback_weekend_bps = _bps(
        raw, "fallback_weekend_bps", DEFAULT_FALLBACK_WEEKEND_BPS, source=source)
    return_bps = _bps(raw, "return_bps", Decimal("2"), source=source)
    return_window_hours = _decimal(raw, "return_window_hours", Decimal("48"))
    if return_window_hours <= 0:
        raise ValueError(f"{source}: return_window_hours must be > 0")

    budgets = _parse_budgets(raw.get("budget"), source=source)
    pairs = _parse_pairs(
        raw.get("pair"),
        source=source,
        default_fixed_bps=fixed_bps,
        default_fallback_bps=fallback_bps,
        default_fallback_weekend_bps=fallback_weekend_bps,
    )
    # Reject inverse duplicates because they would compete for the same market
    # exposure from opposite directions.
    _validate_unique_markets(pairs, source=source)
    # A budget on a token no market spends (a typo, say) would never be applied,
    # so the cap the operator meant to set would silently not exist.
    traded = {token for pair in pairs for token in parse_market_symbol(pair.market)}
    for budget in budgets:
        if budget.token not in traded:
            raise ValueError(
                f"{source}: budget token {budget.token} is not traded by any configured "
                f"market ({', '.join(sorted(traded))}); check its spelling"
            )
    return VLConfig(
        budgets=budgets,
        pairs=pairs,
        fixed_bps=fixed_bps,
        return_bps=return_bps,
        return_window_hours=return_window_hours,
    )


def _parse_budgets(value, *, source: str) -> list[BudgetConfig]:
    """Parse and validate one or more `[[budget]]` TOML tables."""
    rows = _list_table(value)
    if not rows:
        raise ValueError(f"{source}: at least one [[budget]] is required")
    out: list[BudgetConfig] = []
    seen: set[str] = set()
    for i, row in enumerate(rows):
        # A global key written after the [[budget]] header lands in this table
        # (TOML scoping) and would otherwise be silently ignored.
        extra = sorted(set(row) - {"token", "amount"})
        if extra:
            raise ValueError(
                f"{source}: [[budget]] #{i + 1} has unknown key(s) {', '.join(extra)}; "
                "global settings must come before the first [[budget]] or [[pair]]"
            )
        # Token symbols are case-insensitive externally but stored uppercase.
        token = str(row.get("token", "")).strip().upper()
        if not token:
            raise ValueError(f"{source}: [[budget]] #{i + 1} missing token")
        if token in seen:
            raise ValueError(f"{source}: duplicate budget token {token}")
        amount = _decimal(row, "amount", None)
        if amount <= 0:
            raise ValueError(f"{source}: budget {token} amount must be > 0")
        seen.add(token)
        out.append(BudgetConfig(token=token, amount=amount))
    return out


def _parse_pairs(
    value,
    *,
    source: str,
    default_fixed_bps: Decimal,
    default_fallback_bps: Decimal = DEFAULT_FALLBACK_BPS,
    default_fallback_weekend_bps: Decimal = DEFAULT_FALLBACK_WEEKEND_BPS,
) -> list[PairConfig]:
    """Parse and validate one or more `[[pair]]` TOML tables."""
    rows = _list_table(value)
    if not rows:
        raise ValueError(f"{source}: at least one [[pair]] is required")
    out: list[PairConfig] = []
    for i, row in enumerate(rows):
        # Normalize market spelling so `xsgd_usdc` and `XSGD/USDC` converge.
        market = str(row.get("market", "")).strip().upper().replace("_", "/")
        if not market:
            raise ValueError(f"{source}: [[pair]] #{i + 1} missing market")
        # Reuse the shared symbol parser for delimiter/side validation.
        parse_market_symbol(market)
        extra = sorted(set(row) - PAIR_KEYS)
        if extra:
            raise ValueError(
                f"{source}: [[pair]] {market} has unknown key(s) {', '.join(extra)}; "
                "global settings must come before the first [[budget]] or [[pair]]"
            )
        # Where this market's mid comes from: one source, or several in the
        # order to try them.
        price_sources = _price_sources(row.get("price_source", "wise"), where=f"{source}: {market}")
        # Quantity is mandatory per pair because markets have different scales.
        qty_base = _decimal(row, "qty_base", None)
        if qty_base <= 0:
            raise ValueError(f"{source}: {market} qty_base must be > 0")
        # Pair-level strategy values override globals; otherwise defaults apply.
        where = f"{source}: {market}"
        fallback_bps = _bps(row, "fallback_bps", default_fallback_bps, source=where)
        fallback_weekend_bps = _bps(
            row, "fallback_weekend_bps", default_fallback_weekend_bps, source=where)
        if fallback_weekend_bps < fallback_bps:
            raise ValueError(
                f"{where}: fallback_weekend_bps ({fallback_weekend_bps}) must be at least "
                f"fallback_bps ({fallback_bps})")
        out.append(
            PairConfig(
                market=market,
                price_sources=price_sources,
                qty_base=qty_base,
                fixed_bps=_bps(row, "fixed_bps", default_fixed_bps, source=where),
                fallback_bps=fallback_bps,
                fallback_weekend_bps=fallback_weekend_bps,
            )
        )
    return out


def _price_sources(value: Any, *, where: str) -> tuple[str, ...]:
    """A pair's price_source: one name, or a non-empty list of names in order."""
    names = [value] if isinstance(value, str) else value
    if not isinstance(names, list) or not names:
        raise ValueError(f"{where}: price_source must be a source name or a list of them")
    out: list[str] = []
    for raw in names:
        name = str(raw).strip().lower()
        if name == "mas":
            # Its public daily dataset stopped in 2015, and MAS's current API
            # needs a registered key.
            raise ValueError(
                f"{where}: price_source 'mas' was removed (MAS publishes no public "
                "daily rate API); use 'ecb' or 'bnm', which both publish SGD")
        if name == "fed":
            # FRED's H.10 series come out weekly, so a Fed rate is never fresh
            # by the daily window, and every currency it had, ECB has too.
            raise ValueError(
                f"{where}: price_source 'fed' was removed (the Fed publishes its "
                "rates weekly, not daily); use 'ecb', which covers every currency it had")
        if name not in PRICE_SOURCE_CHOICES:
            raise ValueError(f"{where}: unsupported price_source {name!r}")
        if name in out:
            raise ValueError(f"{where}: price_source lists {name!r} twice")
        out.append(name)
    return tuple(out)


def _validate_unique_markets(pairs: list[PairConfig], *, source: str) -> None:
    """Reject duplicate markets even when configured in inverse order."""
    seen: dict[tuple[str, str], str] = {}
    for pair in pairs:
        # Parse BASE/QUOTE and sort to make X/Y and Y/X collide.
        base, quote = parse_market_symbol(pair.market)
        canonical = tuple(sorted((base, quote)))
        if canonical in seen:
            raise ValueError(
                f"{source}: duplicate or inverse market {pair.market} "
                f"conflicts with {seen[canonical]}"
            )
        # Store the first configured spelling for a helpful conflict message.
        seen[canonical] = pair.market


def _list_table(value) -> list[dict[str, Any]]:
    """Normalize TOML table input into a list of dict rows."""
    if value is None:
        return []
    if isinstance(value, list):
        # Copy rows so later validation cannot mutate caller-owned data.
        return [dict(row) for row in value]
    # A single table is accepted as a one-row list for tests/flexibility.
    return [dict(value)]


def _decimal(row: dict[str, Any], key: str, default: Decimal | None) -> Decimal:
    """Read a finite Decimal config value with an optional default."""
    value = row.get(key, default)
    if value is None:
        raise ValueError(f"missing required {key}")
    try:
        # Convert through str to avoid binary float artifacts.
        out = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{key} must be decimal, got {value!r}") from exc
    if not out.is_finite():
        raise ValueError(f"{key} must be a finite number, got {value!r}")
    return out


def _bps(row: dict[str, Any], key: str, default: Decimal, *, source: str) -> Decimal:
    """Read a basis-point spread: more than 0 and less than 10000 (100%)."""
    value = _decimal(row, key, default)
    if not 0 < value < 10000:
        raise ValueError(f"{source}: {key} must be more than 0 and less than 10000, got {value}")
    return value
