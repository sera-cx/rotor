"""Builders shared by the tests/mm modules (imported by bare name: no __init__.py)."""

from __future__ import annotations

from decimal import Decimal

from rotor.mm.exchange.client import MarketInfo
from rotor.mm.vl_config import BudgetConfig, PairConfig, VLConfig
from rotor.mm.vl_runner import OrderIntent


def market(symbol="XSGD/USDC", *, min_bid_quote="0", min_ask="0", **fields) -> MarketInfo:
    """A market on a 0.0001 price and 0.01 size grid; `fields` override the rest."""
    base, quote = symbol.split("/")
    values = {
        "symbol": symbol,
        "base_symbol": base,
        "quote_symbol": quote,
        "base_address": f"0x{base.lower()}",
        "quote_address": f"0x{quote.lower()}",
        "base_decimals": 6,
        "quote_decimals": 6,
        "tick_precision": 4,
        "quantity_precision": 2,
        "price_step": Decimal("0.0001"),
        "amount_step": Decimal("0.01"),
        "min_bid_quote_amount": Decimal(min_bid_quote),
        "min_ask_amount": Decimal(min_ask),
    }
    return MarketInfo(**{**values, **fields})


def intent(
    market: str = "XSGD/USDC",
    side: str = "bid",
    spent: str = "USDC",
    *,
    price: str = "1.00",
    qty: str = "100",
) -> OrderIntent:
    return OrderIntent(
        market=market,
        side=side,
        qty_base=Decimal(qty),
        price=Decimal(price),
        expiration=999,
        spent_token=spent,
    )


def vl_config(
    *,
    budget: str = "1000",
    return_bps: str = "2",
    window: str = "48",
    pairs: list[PairConfig] | None = None,
) -> VLConfig:
    """A config with one USDC budget and fixed_bps 50."""
    return VLConfig(
        budgets=[BudgetConfig("USDC", Decimal(budget))],
        pairs=pairs or [],
        fixed_bps=Decimal("50"),
        return_bps=Decimal(return_bps),
        return_window_hours=Decimal(window),
    )
