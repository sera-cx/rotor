"""Pytest fixtures for the Telegram notifier tests."""

from __future__ import annotations

import pytest

from rotor import config as config_module


@pytest.fixture(autouse=True)
def _only_the_tests_telegram_config(monkeypatch: pytest.MonkeyPatch):
    """Let TELEGRAM_* come only from what each test sets: a developer's real
    environment or root .env may hold a real token."""
    for key in ("TELEGRAM_TOKEN", "TELEGRAM_NORMAL_CHAT_ID", "TELEGRAM_IMPORTANT_CHAT_ID",
                "TELEGRAM_DEBUG_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", True)
    monkeypatch.setattr(config_module, "_DOTENV", {})
