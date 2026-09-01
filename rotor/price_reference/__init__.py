"""External FX reference feeds for Rotor market-making runners."""

# Re-export the reference-price public API from one package-level import path.
from rotor.price_reference.aggregator import ReferencePriceOracle, ReferenceUnavailable
from rotor.price_reference.config import load_price_reference_source, load_token_to_iso
from rotor.price_reference.models import PriceObservation, ReferencePrice
from rotor.price_reference.sources import (
    BnmRateSource,
    CoinGeckoRateSource,
    EcbRateSource,
    FedH10RateSource,
    MasRateSource,
    RateSource,
    WiseRateSource,
)
from rotor.price_reference.symbols import market_to_iso_pair, parse_market_symbol

# Keep the package API explicit for auditors and wildcard imports.
__all__ = [
    "PriceObservation",
    "RateSource",
    "ReferencePrice",
    "ReferencePriceOracle",
    "ReferenceUnavailable",
    "CoinGeckoRateSource",
    "EcbRateSource",
    "FedH10RateSource",
    "BnmRateSource",
    "MasRateSource",
    "WiseRateSource",
    "load_price_reference_source",
    "load_token_to_iso",
    "market_to_iso_pair",
    "parse_market_symbol",
]
