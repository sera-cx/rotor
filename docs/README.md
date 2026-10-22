# Documentation Directory

This folder keeps concise operator notes for the remaining Rotor runtime.

Files:

- `audit-guide.md`: audit checklist and focused test map for the current VL
  runtime, Telegram notifier, and autonomous updater.
- `vl-mm.md`: TOML config, dry-run/live commands, logging, and autonomous update
  behavior for `rotor mm vl`.
- `manus.md`: deploying on Manus — the exact secret-store env manifest and the
  scheduled fire-once (`--once`) run model.

Provider-specific FX reference notes live in `rotor/price_reference/README.md`;
the current supported source names are `wise`, `ecb`, `fed`, and `bnm`.
`wise`, `ecb`, and `bnm` work without a key (`WISE_API_TOKEN` is
optional); `fed` needs a free `FRED_API_KEY`. Live source checks live in `tests/live/`.
