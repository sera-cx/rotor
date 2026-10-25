"""Basic package health checks that should hold in every local run."""

from __future__ import annotations

import importlib

PUBLIC_MODULES = [
    "rotor",
    "rotor.cli.main",
    "rotor.config",
    "rotor.mm.exchange.client",
    "rotor.mm.exchange.eip712",
    "rotor.mm.exchange.errors",
    "rotor.mm.grid",
    "rotor.mm.quote_engine",
    "rotor.mm.vl_config",
    "rotor.mm.vl_runner",
    "rotor.notifications.telegram",
    "rotor.price_reference.aggregator",
    "rotor.price_reference.config",
    "rotor.price_reference.sources",
    "rotor.price_reference.symbols",
    "rotor.security.key",
    "rotor.updater",
]


def test_public_modules_import_without_runtime_side_effects():
    for module_name in PUBLIC_MODULES:
        importlib.import_module(module_name)

