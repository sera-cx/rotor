"""Telegram notifier for the Sera market maker.

A small three-channel notifier the market-maker runners can use to push
operational alerts to a phone:

  * NORMAL channel    — routine lifecycle messages (startup, shutdown,
                        recoveries). Sent silently (no push notification).
  * IMPORTANT channel — errors and anything that needs a human now. The
                        only channel that pushes a notification. With no
                        important chat configured, important messages go
                        once to the normal chat, with the notification.
  * DEBUG channel     — heartbeat keep-alive only. Falls back to the normal
                        channel when no debug chat is configured.

Configuration is resolved through `rotor.config.get_config`, so values may
come from the environment, a root `.env`, or an untracked `rotor/secret.py`:

    TELEGRAM_TOKEN              — bot token from @BotFather
    TELEGRAM_NORMAL_CHAT_ID     — silent lifecycle channel
    TELEGRAM_IMPORTANT_CHAT_ID  — loud error channel (optional)
    TELEGRAM_DEBUG_CHAT_ID      — heartbeat channel (optional)

This module degrades gracefully: if no token is configured, or if
`python-telegram-bot` is not installed, sends become debug dry-run log
lines instead of network calls. Telegram failures never raise into the
market-making loop.
"""

from __future__ import annotations

import asyncio
import logging
import time

# `import telegram` is deferred to `_async_send` so importing this module
# (e.g. for dry-run wiring or tests) does not require python-telegram-bot.
from rotor.config import get_config

# Heartbeat interval — if nothing has been sent this long, emit "still alive".
HEARTBEAT_SECONDS = 60 * 60  # 1 hour


class TelegramNotifier:
    """Three-channel Telegram notifier for market-maker lifecycle events.

    Usage::

        notifier = TelegramNotifier()          # reads tokens from config
        notifier.notify_startup("XSGD/USDC mm started, qty=100")
        notifier.notify_error("place bid", exc)
        notifier.heartbeat()                    # self-rate-limited keep-alive
        notifier.notify_shutdown("XSGD/USDC mm stopped")

    All public methods are safe to call from the synchronous MM loop and
    never raise on Telegram/network failures.
    """

    def __init__(
        self,
        token: str | None = None,
        normal_chat_id: str | None = None,
        important_chat_id: str | None = None,
        debug_chat_id: str | None = None,
        log: logging.Logger | None = None,
        dry_run: bool = False,
    ):
        """Wire up channel routing.

        Args:
            token: Bot token. Defaults to ``TELEGRAM_TOKEN`` config. Empty
                forces dry-run.
            normal_chat_id: Chat id for the silent lifecycle channel.
            important_chat_id: Chat id for the loud error channel.
            debug_chat_id: Chat id for heartbeats.
            log: Optional logger; defaults to the module logger.
            dry_run: If True (or if no token resolves), log instead of send.
        """
        # Explicit constructor args override config, which helps tests and
        # one-off scripts avoid environment mutations.
        self._token = (token if token is not None else get_config(
            "TELEGRAM_TOKEN", "")).strip()
        self._normal_chat_id = (
            normal_chat_id if normal_chat_id is not None
            else get_config("TELEGRAM_NORMAL_CHAT_ID", "")
        )
        self._important_chat_id = (
            important_chat_id if important_chat_id is not None
            else get_config("TELEGRAM_IMPORTANT_CHAT_ID", "")
        )
        self._debug_chat_id = (
            debug_chat_id if debug_chat_id is not None
            else get_config("TELEGRAM_DEBUG_CHAT_ID", "")
        )
        # Use runner logger when provided so notification logs stay in the same
        # output stream.
        self._log = log or logging.getLogger(__name__)
        # dry_run is forced on later if no token exists.
        self._dry_run = dry_run

        # Last time ANY message was sent — drives heartbeat rate-limiting.
        self._last_message_at: float = time.time()

        if not self._token:
            # Missing token is non-fatal; the notifier becomes log-only.
            self._log.warning(
                "TelegramNotifier: TELEGRAM_TOKEN is empty — messages will be "
                "dry-logged only."
            )
            self._dry_run = True

    # -----------------------------------------------------------------------
    # Public API: market-maker lifecycle events
    # -----------------------------------------------------------------------

    def notify_startup(self, text: str) -> None:
        """Announce that the market maker has started (normal channel)."""
        # Lifecycle messages go to the quiet normal channel.
        self.send_raw(f"🟢 {text}")

    def notify_shutdown(self, text: str) -> None:
        """Announce that the market maker is shutting down (normal channel)."""
        # Shutdown is routine and therefore normal-channel only.
        self.send_raw(f"🔴 {text}")

    def notify_error(self, context: str, error: object) -> None:
        """Report an operational error to the normal AND important channels."""
        # Errors get a timestamp and exception type for quick triage.
        timestamp = time.strftime("%H:%M:%S")
        self.send_raw(
            f"⚠️ error: {context}\n{type(error).__name__}: {error}\nat {timestamp}",
            important=True,
        )

    def heartbeat(self) -> bool:
        """Emit a "still alive" message if silent for `HEARTBEAT_SECONDS`.

        Call on every loop tick; self-rate-limits. Goes to the debug channel
        (falling back to normal if no debug chat is configured).

        Returns:
            True if a heartbeat was emitted this call, False otherwise.
        """
        # Suppress heartbeats while recent messages prove the loop is alive.
        if time.time() - self._last_message_at < HEARTBEAT_SECONDS:
            return False
        # Debug channel is intended for low-priority keep-alive signals.
        self._send("💓 rotor market maker alive.", channel="debug")
        self._last_message_at = time.time()
        return True

    def send_raw(self, text: str, *, important: bool = False, debug: bool = False) -> None:
        """Send a raw text message.

        Routing:
          * ``debug=True``: send ONLY to the debug channel.
          * ``important=True``: send to normal AND important.
          * default: send to normal only.
        """
        # Debug messages are not duplicated into normal/important channels.
        if debug:
            self._send(text, channel="debug")
        elif important and not self._important_chat_id:
            # With no separate important chat both channels are the same chat:
            # one message, with the important channel's notification.
            self._send(text, channel="important")
        else:
            # Important messages still go to normal so the full timeline exists
            # in one channel.
            self._send(text, channel="normal")
            if important:
                self._send(text, channel="important")
        # Any attempted send/log counts as activity for heartbeat suppression.
        self._last_message_at = time.time()

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    def _send(self, text: str, *, channel: str) -> bool:
        """Resolve the target chat id for the channel and dispatch.

        Channel routing:
          * ``"normal"``    → normal_chat_id
          * ``"important"`` → important_chat_id (fallback to normal)
          * ``"debug"``     → debug_chat_id    (fallback to normal)

        Returns:
            True if a real Telegram send happened, False on dry-run or a
            swallowed exception (Telegram failures must not crash the loop).

        Raises:
            ValueError: If `channel` is not one of the allowed values.
        """
        # Resolve logical channel into a concrete chat id, with fallbacks for
        # optional important/debug channels.
        if channel == "normal":
            chat_id = self._normal_chat_id
        elif channel == "important":
            chat_id = self._important_chat_id or self._normal_chat_id
        elif channel == "debug":
            chat_id = self._debug_chat_id or self._normal_chat_id
        else:
            raise ValueError(f"unknown channel {channel!r}")

        # Dry-run or missing chat id never attempts network I/O.
        if self._dry_run or not chat_id:
            self._log.debug("[telegram dry-run %s] %s", channel, text)
            return False

        try:
            # The public notifier API is sync, so run the async Telegram send
            # inside a short-lived event loop.
            # Only the important channel pushes a notification.
            asyncio.run(self._async_send(text, chat_id, silent=channel != "important"))
            return True
        except Exception as e:
            # Telegram failures must NOT take down the MM loop.
            # Telegram's errors can quote the token ("The token `...` was
            # rejected by the server"); never let it reach the log.
            self._log.warning(
                "TelegramNotifier: send failed (%s): %s",
                channel,
                str(e).replace(self._token, "<TELEGRAM_TOKEN>"),
            )
            return False

    async def _async_send(self, text: str, chat_id: str, *, silent: bool = False) -> None:
        """Perform the actual `python-telegram-bot` `send_message` call.

        The `import telegram` is local so the rest of the package imports
        without python-telegram-bot installed.
        """
        try:
            # Import locally so Telegram is optional for users who do not enable
            # notifications.
            import telegram
        except ImportError:
            self._log.warning("TelegramNotifier: python-telegram-bot not installed")
            self._log.debug("[telegram dry-run fallback] %s", text)
            return
        # python-telegram-bot exposes Bot as an async context manager.
        bot = telegram.Bot(self._token)
        async with bot:
            # Plain-text send; no markdown/HTML parsing is requested.
            await bot.send_message(chat_id=chat_id, text=text, disable_notification=silent)
