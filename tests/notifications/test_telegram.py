"""Tests for rotor.notifications.telegram.TelegramNotifier.

Covers the MM-focused notifier surface:
  * package re-export
  * dry-run when no token is configured
  * channel resolution + fallback (important/debug -> normal)
  * send_raw routing (normal / important / debug)
  * lifecycle helpers (startup, shutdown, quote, error)
  * heartbeat rate-limiting
  * _async_send mocked python-telegram-bot Bot call
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from rotor.notifications import TelegramNotifier
from rotor.notifications.telegram import HEARTBEAT_SECONDS


@pytest.fixture
def notifier():
    """Notifier with explicit config; dry_run so nothing hits the network."""
    return TelegramNotifier(
        token="mock-token",
        normal_chat_id="normal-chat",
        important_chat_id="important-chat",
        debug_chat_id="debug-chat",
        dry_run=True,
    )


def test_package_reexport():
    assert callable(TelegramNotifier)


def test_missing_token_forces_dry_run(caplog):
    with caplog.at_level(logging.WARNING):
        n = TelegramNotifier(token="", normal_chat_id="c")
    assert n._dry_run is True
    assert any("TELEGRAM_TOKEN is empty" in r.message for r in caplog.records)


def test_send_resolves_normal_channel(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.send_raw("hi")
    send.assert_called_once_with("hi", channel="normal")


def test_send_raw_important_hits_both_channels(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.send_raw("boom", important=True)
    channels = [c.kwargs["channel"] for c in send.call_args_list]
    assert channels == ["normal", "important"]


def test_send_raw_debug_only(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.send_raw("ping", debug=True)
    send.assert_called_once_with("ping", channel="debug")


def test_important_falls_back_to_normal_when_unset():
    n = TelegramNotifier(token="t", normal_chat_id="normal",
                         important_chat_id="", dry_run=True)
    with patch.object(n, "_async_send", new=AsyncMock()) as send:
        n._dry_run = False
        n._send("x", channel="important")
    # chat_id passed to _async_send should be the normal chat (fallback)
    assert send.call_args.args[1] == "normal"


def test_debug_falls_back_to_normal_when_unset():
    n = TelegramNotifier(token="t", normal_chat_id="normal",
                         debug_chat_id="", dry_run=True)
    with patch.object(n, "_async_send", new=AsyncMock()) as send:
        n._dry_run = False
        n._send("x", channel="debug")
    assert send.call_args.args[1] == "normal"


def test_send_unknown_channel_raises(notifier):
    with pytest.raises(ValueError):
        notifier._send("x", channel="bogus")


def test_dry_run_does_not_call_async_send(notifier):
    with patch.object(notifier, "_async_send", new=AsyncMock()) as send:
        result = notifier._send("x", channel="normal")
    assert result is False
    send.assert_not_called()


def test_send_swallows_exceptions():
    n = TelegramNotifier(token="t", normal_chat_id="c")
    n._dry_run = False
    with patch.object(n, "_async_send", new=MagicMock()), \
            patch("asyncio.run", side_effect=RuntimeError("network")):
        # Must not raise into the MM loop.
        assert n._send("x", channel="normal") is False


def test_notify_error_routes_important(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.notify_error("place bid", ValueError("nope"))
    channels = [c.kwargs["channel"] for c in send.call_args_list]
    assert channels == ["normal", "important"]
    assert "place bid" in send.call_args_list[0].args[0]


def test_notify_quote_normal_channel(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.notify_quote("XSGD/USDC", bid="0.74",
                              ask="0.75", qty="100", benchmark="0.745")
    send.assert_called_once()
    body = send.call_args.args[0]
    assert "XSGD/USDC" in body and "0.74" in body and "0.75" in body


def test_notify_startup_and_shutdown(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.notify_startup("started")
        notifier.notify_shutdown("stopped")
    assert "🟢" in send.call_args_list[0].args[0]
    assert "🔴" in send.call_args_list[1].args[0]


def test_heartbeat_rate_limited(notifier):
    # Just sent (constructor sets _last_message_at = now) → suppressed.
    assert notifier.heartbeat() is False
    # Pretend enough time elapsed.
    notifier._last_message_at = time.time() - HEARTBEAT_SECONDS - 1
    with patch.object(notifier, "_send") as send:
        assert notifier.heartbeat() is True
    send.assert_called_once()
    assert send.call_args.kwargs["channel"] == "debug"


def test_token_is_trimmed():
    n = TelegramNotifier(token=" 123:ABC \n", normal_chat_id="c")
    n._dry_run = False
    fake_bot = MagicMock()
    fake_bot.__aenter__ = AsyncMock(return_value=fake_bot)
    fake_bot.__aexit__ = AsyncMock(return_value=False)
    fake_bot.send_message = AsyncMock()
    fake_telegram = MagicMock()
    fake_telegram.Bot.return_value = fake_bot
    with patch.dict("sys.modules", {"telegram": fake_telegram}):
        assert n._send("hello", channel="normal") is True
    fake_telegram.Bot.assert_called_once_with("123:ABC")


def test_send_failure_never_logs_the_token(caplog):
    # python-telegram-bot's InvalidToken message quotes the token.
    n = TelegramNotifier(token="123:SECRET", normal_chat_id="c")
    n._dry_run = False
    rejected = RuntimeError("The token `123:SECRET` was rejected by the server.")
    with patch.object(n, "_async_send", new=MagicMock()), \
            patch("asyncio.run", side_effect=rejected), \
            caplog.at_level(logging.WARNING):
        assert n._send("x", channel="normal") is False
    assert "123:SECRET" not in caplog.text
    assert "The token `<TELEGRAM_TOKEN>` was rejected" in caplog.text


def test_async_send_invokes_bot():
    n = TelegramNotifier(token="tok", normal_chat_id="c")
    n._dry_run = False
    fake_bot = MagicMock()
    fake_bot.__aenter__ = AsyncMock(return_value=fake_bot)
    fake_bot.__aexit__ = AsyncMock(return_value=False)
    fake_bot.send_message = AsyncMock()
    fake_telegram = MagicMock()
    fake_telegram.Bot.return_value = fake_bot
    with patch.dict("sys.modules", {"telegram": fake_telegram}):
        assert n._send("hello", channel="normal") is True
    fake_telegram.Bot.assert_called_once_with("tok")
    # The normal channel is the quiet one: no push notification.
    fake_bot.send_message.assert_awaited_once_with(
        chat_id="c", text="hello", disable_notification=True)


def test_async_send_missing_dependency_degrades(caplog):
    n = TelegramNotifier(token="tok", normal_chat_id="c")
    n._dry_run = False
    with patch.dict("sys.modules", {"telegram": None}):
        with caplog.at_level(logging.WARNING):
            assert n._send("hello", channel="normal") is True
    assert any(
        "python-telegram-bot not installed" in r.message for r in caplog.records)


def test_only_the_important_channel_notifies():
    n = TelegramNotifier(token="t", normal_chat_id="normal", important_chat_id="loud")
    n._dry_run = False
    with patch.object(n, "_async_send", new=AsyncMock()) as send:
        n.send_raw("boom", important=True)
    assert [(c.args[1], c.kwargs["silent"]) for c in send.call_args_list] == [
        ("normal", True), ("loud", False)]


def test_an_important_message_is_sent_once_when_there_is_one_chat():
    n = TelegramNotifier(token="t", normal_chat_id="normal", important_chat_id="")
    n._dry_run = False
    with patch.object(n, "_async_send", new=AsyncMock()) as send:
        n.send_raw("boom", important=True)
    assert [(c.args[1], c.kwargs["silent"]) for c in send.call_args_list] == [("normal", False)]
