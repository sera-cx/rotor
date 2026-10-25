"""Tests for rotor.notifications.telegram.TelegramNotifier."""

from __future__ import annotations

import json
import logging
import time
from unittest.mock import patch

import httpx
import pytest

from rotor.notifications.telegram import HEARTBEAT_SECONDS, MAX_MESSAGE_CHARS, TelegramNotifier


def _recording_client(sent: list, status: int = 200, body: dict | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _notifier(monkeypatch, *, token="tok", normal="normal", important="", debug="",
              http: httpx.Client | None = None, **kwargs) -> TelegramNotifier:
    """A notifier configured through TELEGRAM_* settings, posting to `http`."""
    for key, value in (("TELEGRAM_TOKEN", token), ("TELEGRAM_NORMAL_CHAT_ID", normal),
                       ("TELEGRAM_IMPORTANT_CHAT_ID", important),
                       ("TELEGRAM_DEBUG_CHAT_ID", debug)):
        monkeypatch.setenv(key, value)
    notifier = TelegramNotifier(**kwargs)
    if http is not None:
        notifier._http = http
    return notifier


@pytest.fixture
def notifier(monkeypatch):
    """Every chat configured; dry_run so nothing hits the network."""
    return _notifier(monkeypatch, token="mock-token", normal="normal-chat",
                     important="important-chat", debug="debug-chat", dry_run=True)


def test_missing_token_forces_dry_run(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        n = _notifier(monkeypatch, token="", normal="c")
    assert n._dry_run is True
    assert any("TELEGRAM_TOKEN is empty" in r.message for r in caplog.records)
    with patch.object(n, "_post") as post:
        n.send_raw("x", important=True)
    post.assert_not_called()


def test_a_requested_dry_run_never_posts_and_does_not_warn(monkeypatch, caplog):
    # A run without --telegram, on a host whose config holds a real token.
    with caplog.at_level(logging.WARNING):
        n = _notifier(monkeypatch, important="loud", dry_run=True)
        with patch.object(n, "_post") as post:
            n.send_raw("x", important=True)
            n.notify_startup("started")
    post.assert_not_called()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_send_resolves_normal_channel(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.send_raw("hi")
    send.assert_called_once_with("hi", channel="normal")


def test_send_raw_important_hits_both_channels(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.send_raw("boom", important=True)
    channels = [c.kwargs["channel"] for c in send.call_args_list]
    assert channels == ["normal", "important"]


@pytest.mark.parametrize("channel", ["important", "debug"])
def test_an_unset_channel_falls_back_to_normal(monkeypatch, channel):
    n = _notifier(monkeypatch, normal="normal")
    with patch.object(n, "_post") as send:
        n._send("x", channel=channel)
    assert send.call_args.args[1] == "normal"


def test_send_swallows_exceptions(monkeypatch, caplog):
    n = _notifier(monkeypatch, normal="c")
    with patch.object(n, "_post", side_effect=RuntimeError("network")):
        # Must not raise into the MM loop.
        n._send("x", channel="normal")
    assert "send failed (normal): network" in caplog.text


def test_notify_error_routes_important(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.notify_error("place bid", ValueError("nope"))
    channels = [c.kwargs["channel"] for c in send.call_args_list]
    assert channels == ["normal", "important"]
    assert "place bid" in send.call_args_list[0].args[0]


def test_notify_startup_and_shutdown(notifier):
    with patch.object(notifier, "_send") as send:
        notifier.notify_startup("started")
        notifier.notify_shutdown("stopped")
    assert "🟢" in send.call_args_list[0].args[0]
    assert "🔴" in send.call_args_list[1].args[0]


def test_heartbeat_rate_limited(notifier):
    with patch.object(notifier, "_send") as send:
        # Just sent (constructor sets _last_message_at = now) → suppressed.
        notifier.heartbeat()
        send.assert_not_called()
        # Pretend enough time elapsed.
        notifier._last_message_at = time.time() - HEARTBEAT_SECONDS - 1
        notifier.heartbeat()
    send.assert_called_once()
    assert send.call_args.kwargs["channel"] == "debug"


def test_token_is_trimmed(monkeypatch):
    sent = []
    n = _notifier(monkeypatch, token=" 123:ABC \n", normal="c", http=_recording_client(sent))
    n._send("hello", channel="normal")
    assert sent[0].url.path == "/bot123:ABC/sendMessage"


def test_send_failure_never_logs_the_token(monkeypatch, caplog):
    # httpx's status error quotes the request URL, which holds the token.
    n = _notifier(monkeypatch, token="123:SECRET", normal="c",
                  http=_recording_client([], status=401, body={"ok": False}))
    with caplog.at_level(logging.WARNING):
        n._send("x", channel="normal")
    assert "123:SECRET" not in caplog.text
    assert "/bot<TELEGRAM_TOKEN>/sendMessage" in caplog.text


def test_send_posts_to_the_bot_api(monkeypatch):
    sent = []
    n = _notifier(monkeypatch, normal="c", http=_recording_client(sent))
    n._send("hello", channel="normal")
    assert str(sent[0].url) == "https://api.telegram.org/bottok/sendMessage"
    # The normal channel is the quiet one: no push notification.
    assert json.loads(sent[0].content) == {
        "chat_id": "c", "text": "hello", "disable_notification": True}


def test_a_message_telegram_refuses_is_a_failed_send(monkeypatch, caplog):
    n = _notifier(monkeypatch, normal="c",
                  http=_recording_client([], body={"ok": False, "description": "bad"}))
    n._send("hello", channel="normal")
    assert "Telegram refused the message" in caplog.text


def test_a_long_alert_is_cut_to_telegrams_limit(monkeypatch):
    sent = []
    n = _notifier(monkeypatch, normal="c", http=_recording_client(sent))
    n._send("x" * 9000, channel="normal")
    text = json.loads(sent[0].content)["text"]
    assert len(text) == MAX_MESSAGE_CHARS
    assert text.endswith("[cut]")


def test_a_token_without_a_chat_id_warns_and_dry_runs(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        n = _notifier(monkeypatch, normal="", important="")
    assert n._dry_run is True
    assert "no TELEGRAM_NORMAL_CHAT_ID" in caplog.text


def test_only_the_important_channel_notifies(monkeypatch):
    n = _notifier(monkeypatch, normal="normal", important="loud")
    with patch.object(n, "_post") as send:
        n.send_raw("boom", important=True)
    assert [(c.args[1], c.kwargs["silent"]) for c in send.call_args_list] == [
        ("normal", True), ("loud", False)]


def test_an_important_message_is_sent_once_when_there_is_one_chat(monkeypatch):
    n = _notifier(monkeypatch, normal="normal", important="")
    with patch.object(n, "_post") as send:
        n.send_raw("boom", important=True)
    assert [(c.args[1], c.kwargs["silent"]) for c in send.call_args_list] == [("normal", False)]
