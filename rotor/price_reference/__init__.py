"""External FX reference feeds for Rotor market-making runners."""

# Re-export the reference-price API the runner and config use from one import path.
from rotor.price_reference.aggregator import (
    ReferencePriceOracle,
    ReferenceTooStale,
    ReferenceUnavailable,
)
from rotor.price_reference.config import load_price_reference_source, load_token_to_iso
from rotor.price_reference.models import ReferencePrice
from rotor.price_reference.symbols import market_to_iso_pair, parse_market_symbol

__all__ = [
    "ReferencePrice",
    "ReferencePriceOracle",
    "ReferenceTooStale",
    "ReferenceUnavailable",
    "load_price_reference_source",
    "load_token_to_iso",
    "market_to_iso_pair",
    "parse_market_symbol",
]
