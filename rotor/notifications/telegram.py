"""Telegram notifier for the Sera market maker.

Three channels: normal (silent lifecycle messages), important (errors; the only
channel that pushes a notification) and debug (heartbeat only). Important and
debug fall back to the normal chat when unset.

TELEGRAM_TOKEN and TELEGRAM_{NORMAL,IMPORTANT,DEBUG}_CHAT_ID resolve through
`rotor.config.get_config`, so they may come from the environment or a root `.env`.
With no token or chat id, sends become debug
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

    def __init__(self, *, log: logging.Logger | None = None, dry_run: bool = False):
        """Read the TELEGRAM_* settings. `dry_run` (a run without --telegram)
        only logs at DEBUG, and does not warn about missing settings."""
        self._token = get_config("TELEGRAM_TOKEN", "").strip()
        self._chats = {
            "normal": get_config("TELEGRAM_NORMAL_CHAT_ID", ""),
            "important": get_config("TELEGRAM_IMPORTANT_CHAT_ID", ""),
            "debug": get_config("TELEGRAM_DEBUG_CHAT_ID", ""),
        }
        self._log = log or logging.getLogger(__name__)
        self._dry_run = dry_run
        self._http = httpx.Client(timeout=10.0)

        # Last time ANY message was sent — drives heartbeat rate-limiting.
        self._last_message_at: float = time.time()

        if dry_run:
            return
        if not self._token:
            self._log.warning(
                "TelegramNotifier: TELEGRAM_TOKEN is empty — messages will be "
                "dry-logged only."
            )
            self._dry_run = True
        elif not (self._chats["normal"] or self._chats["important"]):
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

    def heartbeat(self) -> None:
        """Emit a "still alive" message if silent for `HEARTBEAT_SECONDS`.

        Call on every loop tick; self-rate-limits.
        """
        if time.time() - self._last_message_at < HEARTBEAT_SECONDS:
            return
        self._send("💓 rotor market maker alive.", channel="debug")
        self._last_message_at = time.time()

    def send_raw(self, text: str, *, important: bool = False) -> None:
        """Send to the normal channel, and with `important` to the important one."""
        if important and not self._chats["important"]:
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

    def _send(self, text: str, *, channel: str) -> None:
        """Send `text` to the channel's chat; failures are logged, never raised."""
        chat_id = self._chats[channel] or self._chats["normal"]
        if self._dry_run or not chat_id:
            self._log.debug("[telegram dry-run %s] %s", channel, text)
            return

        if len(text) > MAX_MESSAGE_CHARS:
            cut = "\n…[cut]"
            text = text[: MAX_MESSAGE_CHARS - len(cut)] + cut
        try:
            # Only the important channel pushes a notification.
            self._post(text, chat_id, silent=channel != "important")
        except Exception as e:
            # Telegram failures must NOT take down the MM loop.
            # httpx errors quote the request URL, which holds the token; never
            # let it reach the log.
            self._log.warning(
                "TelegramNotifier: send failed (%s): %s",
                channel,
                str(e).replace(self._token, "<TELEGRAM_TOKEN>"),
            )

    def _post(self, text: str, chat_id: str, *, silent: bool) -> None:
        """Send one plain-text message with the Bot API's sendMessage."""
        response = self._http.post(
            f"{TELEGRAM_API}/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_notification": silent},
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError(f"Telegram refused the message: {response.text[:200]}")
