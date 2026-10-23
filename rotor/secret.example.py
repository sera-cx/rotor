"""Optional local config template.

Environment variables are preferred. For most deployments, copy the root
`.env.example` file to `.env` instead. A root `.env` is loaded automatically.
If you specifically want Python file-based config, copy this file to
`rotor/secret.py`; that file is gitignored and used only as a fallback.
"""

# Sera REST API.
# Base URL points to testnet by default so a copied template does not target
# production unintentionally.
SERA_BASE_URL = "https://api.testnet.sera.cx/api/v1"
# API credentials are required for authenticated Sera endpoints.
SERA_API_KEY = ""
SERA_API_SECRET = ""

# Maker signing key. Required for live (non-dry-run) order placement.
# Keep this file, the root `.env`, or the environment protected.
SERA_PRIVATE_KEY = ""

# Tiny unresolved-state DB. This is not trade history.
ROTOR_STATE_PATH = ".rotor_state.sqlite"

# Each [[pair]] picks its price source in the TOML config (`wise` when omitted).
# Wise supplies the after-fee prices every pair's normal quotes sit at. The token is
# optional; it only raises Wise's rate limits.
WISE_API_TOKEN = ""
# Wise endpoint root; useful to override in tests or regional deployments.
WISE_BASE_URL = "https://api.wise.com"
# ECB publishes an XML daily reference-rate feed at this URL.
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
# Fed H.10 rates use the official FRED API; set a free FRED_API_KEY from
# https://fred.stlouisfed.org/docs/api/api_key.html (the fredgraph.csv scrape
# endpoint is bot-walled and unreliable from servers).
FRED_API_KEY = ""
FRED_API_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
FED_H10_SERIES_JSON = {"EUR": "DEXUSEU", "MYR": "DEXMAUS", "SGD": "DEXSIUS"}
# Bank Negara Malaysia publishes MYR-crossed exchange rates here.
BNM_EXCHANGE_RATE_BASE_URL = "https://api.bnm.gov.my/public/exchange-rate"

# Optional mapping for tokens whose market symbols differ from ISO currency
# codes used by reference-rate providers.
PRICE_REFERENCE_TOKEN_TO_ISO = {
    # "GBPT": "GBP",
}

# Telegram alerts (optional). Enable with `rotor mm vl --telegram`.
# Leave TELEGRAM_TOKEN empty to keep notifications in dry-run (log-only) mode.
TELEGRAM_TOKEN = ""
TELEGRAM_NORMAL_CHAT_ID = ""
TELEGRAM_IMPORTANT_CHAT_ID = ""
TELEGRAM_DEBUG_CHAT_ID = ""
