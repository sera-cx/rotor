# Rotor Package

Importable package behind the `rotor` CLI.

Files and folders:

- `config.py`: environment-first config helpers with `.env` loading and optional
  untracked `rotor/secret.py` fallback.
- `secret.example.py`: safe template for Python file-based local config.
- `cli/`: argparse command tree for `rotor mm vl`.
- `mm/`: VL runner, TOML config loader, and the Sera API integration.
- `notifications/`: optional Telegram lifecycle/error notifications.
- `price_reference/`: FX source adapters (`wise`, `ecb`, `fed`, `bnm`)
  and token-to-ISO mapping.
- `security/`: signing-key loading and EIP-712 signing helpers.
- `updater.py`: opt-in daily Git pull helper for autonomous source deployments.
