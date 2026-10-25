# Sera Client Package

This package contains Sera-specific REST and signing payload helpers.

Files:

- `__init__.py`: package summary and module index.
- `client.py`: small Sera REST wrapper for config/health/markets, order
  placement, VL batch placement/cancel, balances, open orders, and fills.
  Requires an `https://` base URL (except `localhost`), builds and signs every
  order locally (never a server-supplied struct), pins the signing contract for
  mainnet and Sepolia, paces requests to Sera's per-wallet limits, and
  paginates fills.
- `known_tokens.py`: addresses and decimals of Sera's mainnet tokens, which
  bootstrap checks `/markets` against.
- `eip712.py`: EIP-712 type definitions plus Sera `uuid_int` bit packing for
  standalone and VL orders.
- `errors.py`: typed error envelopes for Sera HTTP responses, including cancel
  cooldown handling (classified by error code, with a message-substring fallback
  only when no code is present).

Keep Sera API shape changes isolated here so runners can stay focused on market
making decisions instead of wire-format details.
