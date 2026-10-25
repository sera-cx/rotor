"""Tests for environment-first configuration loading."""

from __future__ import annotations

import pytest

from rotor import config as config_module
from rotor.config import get_config, get_config_json


def test_get_config_reads_dotenv_when_env_absent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ROTOR_DOTENV_ONLY", raising=False)
    monkeypatch.delenv("ROTOR_DOTENV_EXPORTED", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", False)
    (tmp_path / ".env").write_text(
        "ROTOR_DOTENV_ONLY=from-dotenv\n"
        "export ROTOR_DOTENV_EXPORTED='from-export'\n"
        "# ignored\n",
        encoding="utf-8",
    )

    assert get_config("ROTOR_DOTENV_ONLY") == "from-dotenv"
    assert get_config("ROTOR_DOTENV_EXPORTED") == "from-export"


def test_get_config_prefers_environment_over_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ROTOR_DOTENV_ENV_WINS", "from-env")
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", False)
    (tmp_path / ".env").write_text(
        "ROTOR_DOTENV_ENV_WINS=from-dotenv\n",
        encoding="utf-8",
    )

    assert get_config("ROTOR_DOTENV_ENV_WINS") == "from-env"


def test_get_config_returns_default_when_missing(monkeypatch):
    monkeypatch.delenv("NOT_CONFIGURED", raising=False)

    assert get_config("NOT_CONFIGURED", "fallback") == "fallback"


def test_get_config_json_parses_environment_json(monkeypatch):
    monkeypatch.setenv("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", '{"xphp": "PHP"}')

    assert get_config_json("PRICE_REFERENCE_TOKEN_TO_ISO_JSON") == {"xphp": "PHP"}


def test_get_config_json_uses_default_for_blank_environment_value(monkeypatch):
    monkeypatch.setenv("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", "")

    assert get_config_json("PRICE_REFERENCE_TOKEN_TO_ISO_JSON", {}) == {}


def test_get_config_json_raises_on_invalid_environment_json(monkeypatch):
    monkeypatch.setenv("BROKEN_JSON", "{")

    with pytest.raises(ValueError):
        get_config_json("BROKEN_JSON")


def test_dotenv_does_not_truncate_unquoted_value_with_hash(monkeypatch, tmp_path):
    # A secret containing " #" must not be silently truncated at an inline comment.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ROTOR_SECRET_WITH_HASH", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", False)
    (tmp_path / ".env").write_text(
        "ROTOR_SECRET_WITH_HASH=ab cd #ef\n",
        encoding="utf-8",
    )

    assert get_config("ROTOR_SECRET_WITH_HASH") == "ab cd #ef"


def test_a_real_variable_beats_a_dotenv_line_even_a_blank_one(monkeypatch, tmp_path):
    # .env.example ships blank SERA_* lines.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERA_API_KEY", "from-env")
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", False)
    (tmp_path / ".env").write_text("SERA_API_KEY=\nROTOR_ONLY_IN_DOTENV=x\n", encoding="utf-8")

    assert get_config("SERA_API_KEY") == "from-env"
    assert get_config("ROTOR_ONLY_IN_DOTENV") == "x"
    # .env never leaks into the process environment.
    assert "ROTOR_ONLY_IN_DOTENV" not in __import__("os").environ


@pytest.mark.parametrize(("line", "value"), [
    ('KEY="https://api.sera.cx/api/v1"  # mainnet', "https://api.sera.cx/api/v1"),
    ("KEY='tok en'#note", "tok en"),
    ('KEY="quoted"', "quoted"),
    ('KEY="unterminated # still verbatim', '"unterminated # still verbatim'),
    ('KEY="a" b', '"a" b'),
])
def test_a_comment_after_a_quoted_value_is_dropped(monkeypatch, tmp_path, line, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KEY", raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", False)
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")

    assert get_config("KEY") == value
