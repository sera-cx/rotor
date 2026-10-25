"""Client helpers for Sera, the settlement layer rotor quotes on.

Modules:
    client:  REST wrapper for config, health, markets, locally built order
             placement, cancel, balances, and open-order and fill reads.
    eip712:  Typed-data definitions for `Order`, `CancelOrder`, plus the
             `uuid_int` bit-packing helper.
    errors:  `SeraError` + `SeraCancelCooldown` typed envelope parser.
"""

# This package marker documents the Sera client layer but intentionally avoids
# imports so HTTP/signing dependencies load only from their concrete modules.
