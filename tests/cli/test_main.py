"""Tests for the Rotor CLI argument and wallet-gating behavior."""

from __future__ import annotations

import pytest

from rotor import config as config_module
from rotor.cli import main as cli
from rotor.mm import vl_runner
from rotor.security import key as wallet_key


def test_version_exits_successfully(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--version"])

    assert ei.value.code == 0
    assert "rotor" in capsys.readouterr().out


def test_missing_command_exits_with_parser_error():
    with pytest.raises(SystemExit) as ei:
        cli.main([])

    assert ei.value.code == 2


def test_vl_dry_run_passes_args_to_runner(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(vl_runner, "run", fake_run)

    assert cli.main(["mm", "vl", "--config", "vl.toml", "--dry-run", "--once"]) == 0
    assert captured["config_path"] == "vl.toml"
    assert captured["poll_seconds"] == 305.0
    assert captured["dry_run"] is True
    assert captured["once"] is True
    assert captured["auto_update"] is False
    assert captured["update_interval_seconds"] == 86400.0
    assert captured["log_level"] == "WARNING"
    assert captured["halt_on_stale_reference"] is False


def test_vl_auto_update_flags_pass_to_runner(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(vl_runner, "run", fake_run)

    assert cli.main([
        "mm",
        "vl",
        "--config",
        "vl.toml",
        "--dry-run",
        "--once",
        "--telegram",
        "--auto-update",
        "--auto-update-interval-seconds",
        "60",
    ]) == 0

    assert captured["auto_update"] is True
    assert captured["update_interval_seconds"] == 60.0
    assert captured["telegram"] is True



def test_vl_halt_on_stale_reference_flag_passes_to_runner(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(vl_runner, "run", fake_run)

    assert cli.main([
        "mm", "vl", "--config", "vl.toml", "--dry-run", "--once", "--halt-on-stale-reference",
    ]) == 0
    assert captured["halt_on_stale_reference"] is True


def test_live_vl_requires_wallet_unlock(monkeypatch, capsys):
    wallet_key.lock()
    monkeypatch.delenv("SERA_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", True)

    assert cli.main(["mm", "vl", "--config", "vl.toml"]) == 1
    assert "no SERA_PRIVATE_KEY" in capsys.readouterr().err


def test_smoke_command_is_not_available():
    with pytest.raises(SystemExit) as ei:
        cli.main(["smoke", "market", "--qty", "1"])

    assert ei.value.code == 2


def test_mm_simple_is_not_available():
    with pytest.raises(SystemExit) as ei:
        cli.main(["mm", "simple", "--qty", "1"])

    assert ei.value.code == 2
