"""Telegram notifier for the Sera market maker.

Three channels: normal (silent lifecycle messages), important (errors; the only
channel that pushes a notification) and debug (heartbeat only). Important and
debug fall back to the normal chat when unset.

TELEGRAM_TOKEN and TELEGRAM_{NORMAL,IMPORTANT,DEBUG}_CHAT_ID resolve through
`rotor.config.get_config`, so they may come from the environment, a root `.env`,
or an untracked `rotor/secret.py`. With no token or chat id, sends become debug
dry-run log lines. Messages go straight to the Bot API over httpx. Telegram
failures never raise into the market-making loop.
"""

from __future__ import annotations

import logging
import time

import httpx

from rotor.config import get_config

# If nothing has been sent this long, emit "still alive".
HEARTBEAT_SECONDS = 60 * 60
# Telegram refuses messages over 4096 characters, so a longer alert (an error
# that embeds a whole HTML error page) is cut rather than lost.
MAX_MESSAGE_CHARS = 4000
TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Three-channel notifier; public methods never raise on Telegram failures."""

    def __init__(
        self,
        token: str | None = None,
        normal_chat_id: str | None = None,
        important_chat_id: str | None = None,
        debug_chat_id: str | None = None,
        log: logging.Logger | None = None,
        dry_run: bool = False,
        http: httpx.Client | None = None,
    ):
        """Wire up channel routing.

        Args:
            token: Bot token. Defaults to ``TELEGRAM_TOKEN`` config. Empty
                forces dry-run.
            normal_chat_id: Chat id for the silent lifecycle channel.
            important_chat_id: Chat id for the loud error channel.
            debug_chat_id: Chat id for heartbeats.
            log: Optional logger; defaults to the module logger.
            dry_run: If True (or if no token or chat id resolves), log instead
                of send.
            http: Optional HTTP client, for tests.
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
        self._log = log or logging.getLogger(__name__)
        self._dry_run = dry_run
        self._http = http or httpx.Client(timeout=10.0)

        # Last time ANY message was sent — drives heartbeat rate-limiting.
        self._last_message_at: float = time.time()

        if not self._token:
            self._log.warning(
                "TelegramNotifier: TELEGRAM_TOKEN is empty — messages will be "
                "dry-logged only."
            )
            self._dry_run = True
        elif not (self._normal_chat_id or self._important_chat_id):
            # A token alone sends nothing; say so instead of dropping alerts.
            self._log.warning(
                "TelegramNotifier: no TELEGRAM_NORMAL_CHAT_ID or "
                "TELEGRAM_IMPORTANT_CHAT_ID — messages will be dry-logged only."
            )
            self._dry_run = True

    def notify_startup(self, text: str) -> None:
        """Announce that the market maker has started (normal channel)."""
        self.send_raw(f"🟢 {text}")

    def notify_shutdown(self, text: str) -> None:
        """Announce that the market maker is shutting down (normal channel)."""
        self.send_raw(f"🔴 {text}")

    def notify_error(self, context: str, error: object) -> None:
        """Report an operational error to the normal AND important channels."""
        timestamp = time.strftime("%H:%M:%S")
        self.send_raw(
            f"⚠️ error: {context}\n{type(error).__name__}: {error}\nat {timestamp}",
            important=True,
        )

    def heartbeat(self) -> bool:
        """Emit a "still alive" message if silent for `HEARTBEAT_SECONDS`.

        Call on every loop tick; self-rate-limits.

        Returns:
            True if a heartbeat was emitted this call, False otherwise.
        """
        if time.time() - self._last_message_at < HEARTBEAT_SECONDS:
            return False
        self._send("💓 rotor market maker alive.", channel="debug")
        self._last_message_at = time.time()
        return True

    def send_raw(self, text: str, *, important: bool = False, debug: bool = False) -> None:
        """Send to normal, to normal and important, or (`debug`) to debug only."""
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

    def _send(self, text: str, *, channel: str) -> bool:
        """Send `text` to the channel's chat.

        Returns:
            True if a real Telegram send happened, False on dry-run or a
            swallowed exception (Telegram failures must not crash the loop).

        Raises:
            ValueError: If `channel` is not one of the allowed values.
        """
        if channel == "normal":
            chat_id = self._normal_chat_id
        elif channel == "important":
            chat_id = self._important_chat_id or self._normal_chat_id
        elif channel == "debug":
            chat_id = self._debug_chat_id or self._normal_chat_id
        else:
            raise ValueError(f"unknown channel {channel!r}")

        if self._dry_run or not chat_id:
            self._log.debug("[telegram dry-run %s] %s", channel, text)
            return False

        if len(text) > MAX_MESSAGE_CHARS:
            cut = "\n…[cut]"
            text = text[: MAX_MESSAGE_CHARS - len(cut)] + cut
        try:
            # Only the important channel pushes a notification.
            self._post(text, chat_id, silent=channel != "important")
            return True
        except Exception as e:
            # Telegram failures must NOT take down the MM loop.
            # httpx errors quote the request URL, which holds the token; never
            # let it reach the log.
            self._log.warning(
                "TelegramNotifier: send failed (%s): %s",
                channel,
                str(e).replace(self._token, "<TELEGRAM_TOKEN>"),
            )
            return False

    def _post(self, text: str, chat_id: str, *, silent: bool) -> None:
        """Send one plain-text message with the Bot API's sendMessage."""
        response = self._http.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_notification": silent},
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError(f"Telegram refused the message: {response.text[:200]}")
