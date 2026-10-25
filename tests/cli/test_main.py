"""Tests for the Rotor CLI argument and wallet-gating behavior."""

from __future__ import annotations

import pytest

from rotor import config as config_module
from rotor.cli import main as cli
from rotor.mm import vl_runner
from rotor.security import key as wallet_key


def test_missing_command_exits_with_parser_error():
    with pytest.raises(SystemExit) as ei:
        cli.main([])

    assert ei.value.code == 2


# What `rotor mm vl --config vl.toml --dry-run --once` passes the runner.
_DEFAULTS = {
    "config_path": "vl.toml",
    "poll_seconds": 305.0,
    "dry_run": True,
    "once": True,
    "telegram": False,
    "auto_update": False,
    "update_interval_seconds": 86400.0,
    "log_level": "WARNING",
    "halt_on_stale_reference": False,
}


@pytest.mark.parametrize(("flags", "changed"), [
    pytest.param([], {}, id="defaults"),
    pytest.param(
        ["--telegram", "--auto-update", "--auto-update-interval-seconds", "60"],
        {"telegram": True, "auto_update": True, "update_interval_seconds": 60.0},
        id="telegram-and-auto-update"),
    pytest.param(["--halt-on-stale-reference"], {"halt_on_stale_reference": True},
                 id="halt-on-stale-reference"),
])
def test_vl_flags_pass_to_the_runner(monkeypatch, flags, changed):
    captured = {}
    monkeypatch.setattr(vl_runner, "run", lambda **kwargs: captured.update(kwargs) or 0)

    assert cli.main(["mm", "vl", "--config", "vl.toml", "--dry-run", "--once", *flags]) == 0
    assert {key: captured[key] for key in _DEFAULTS} == {**_DEFAULTS, **changed}


def test_live_vl_requires_wallet_unlock(monkeypatch, capsys):
    monkeypatch.setattr(wallet_key, "_account", None)
    monkeypatch.delenv("SERA_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", True)

    assert cli.main(["mm", "vl", "--config", "vl.toml"]) == 1
    assert "no SERA_PRIVATE_KEY" in capsys.readouterr().err


_GOOD_CONFIG = """fixed_bps = "50"
return_bps = "2"

[[budget]]
token = "USDC"
amount = "1000"

[[pair]]
market = "XSGD/USDC"
price_source = "ecb"
qty_base = "100"
"""


def test_check_config_loads_the_runner_and_config_without_the_wallet(
    monkeypatch, tmp_path, capsys,
):
    def no_wallet():
        raise AssertionError("--check-config must not unlock the wallet")

    monkeypatch.setattr(cli, "_unlock_wallet", no_wallet)
    good = tmp_path / "good.toml"
    good.write_text(_GOOD_CONFIG)
    bad = tmp_path / "bad.toml"
    bad.write_text(_GOOD_CONFIG.replace('price_source = "ecb"', 'price_source = "mas"'))

    assert cli.main(["mm", "vl", "--config", str(good), "--check-config"]) == 0
    assert "config ok" in capsys.readouterr().out
    assert cli.main(["mm", "vl", "--config", str(bad), "--check-config"]) == 1
    assert "'mas' was removed" in capsys.readouterr().err


def test_an_unexpected_unlock_failure_prints_only_its_type(monkeypatch, capsys):
    secret = "0x" + "ab" * 32

    def unlock():
        raise RuntimeError(f"something about {secret}")

    monkeypatch.setattr(wallet_key, "unlock", unlock)

    assert cli.main(["mm", "vl", "--config", "vl.toml"]) == 1
    err = capsys.readouterr().err
    assert "could not unlock the wallet (RuntimeError)" in err
    assert secret not in err
