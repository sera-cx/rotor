"""Token-symbol to ISO-currency mapping for simple FX market making."""

from __future__ import annotations

# Every stablecoin the Earn setup offers, by the fiat currency it tracks, plus
# the plain ISO codes. AXCNH tracks offshore yuan (CNH), not onshore CNY: a
# source without CNH skips the pair rather than quote off a different rate.
_TOKENS_BY_ISO: dict[str, tuple[str, ...]] = {
    "USD": ("USDC", "USDT", "USD"),
    "AED": ("AEDZ", "AED"),
    "ARS": ("WARS", "ARS"),
    "AUD": ("AUDD", "AUDF", "AUDM", "AUDX", "AUD"),
    "BRL": ("BRLV", "BRZ", "BRL"),
    "CAD": ("CADC", "QCAD", "CAD"),
    "CHF": ("CHFAU", "VCHF", "CHF"),
    "CLP": ("WCLP", "CLP"),
    "CNH": ("AXCNH", "CNH"),
    "COP": ("WCOP", "COP"),
    "EUR": (
        "EUR0", "EURAU", "EURC", "EURE", "EURI", "EUROP", "EURQ", "EURR", "EURS",
        "EURT", "VEUR", "EUR",
    ),
    "GBP": ("TGBP", "GBP"),
    "IDR": ("IDRT", "IDR"),
    "JPY": ("JPYC", "JPY"),
    "MXN": ("MXNB", "MXNT", "MXN"),
    "MYR": ("MYRC", "MYRT", "MYR"),
    "NGN": ("CNGN", "NGN"),
    "NZD": ("NZDD", "NZD"),
    "PEN": ("WPEN", "PEN"),
    "SGD": ("XSGD", "SGD"),
    "TRY": ("ITRY", "TRYB", "TRY"),
    "ZAR": ("ZARP", "ZAR"),
}
DEFAULT_TOKEN_TO_ISO: dict[str, str] = {
    token: iso for iso, tokens in _TOKENS_BY_ISO.items() for token in tokens
}


def parse_market_symbol(market: str) -> tuple[str, str]:
    """Split a Sera market symbol like `XSGD/USDC` into token symbols."""
    parts = [p.strip().upper() for p in market.replace("_", "/").split("/")]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"market must look like BASE/QUOTE, got {market!r}")
    return parts[0], parts[1]


def market_to_iso_pair(
    market: str,
    token_to_iso: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Map a Sera token market into an ISO FX pair.

    Examples:
        `XSGD/USDC` -> (`SGD`, `USD`)
        `MYRT/USDT` -> (`MYR`, `USD`)
    """
    # User overrides win over built-ins so new stablecoin symbols can be added
    # without changing source.
    mapping = {**DEFAULT_TOKEN_TO_ISO, **(token_to_iso or {})}
    # Parse before mapping so the error for malformed market symbols is clear.
    base_token, quote_token = parse_market_symbol(market)
    try:
        return mapping[base_token], mapping[quote_token]
    except KeyError as exc:
        # Missing token mappings are configuration issues, not provider issues.
        raise ValueError(
            f"no ISO mapping for token {exc.args[0]!r}; add PRICE_REFERENCE_TOKEN_TO_ISO_JSON"
        ) from exc
