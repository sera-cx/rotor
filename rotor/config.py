"""Small configuration helpers.

Runtime config is environment-first for open-source friendliness. Rotor also
loads lightweight `.env` files.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_DOTENV_LOADED = False
# Values read from `.env` files. Kept apart from os.environ so a real
# environment variable (bare or prefixed) always beats any `.env` line.
_DOTENV: dict[str, str] = {}

_log = logging.getLogger(__name__)


def get_config(name: str, default: Any = None) -> Any:
    """Return a config value from the environment or `.env`, else `default`."""
    _load_dotenv_once()
    found, value = _env_value(name)
    return value if found else default


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def require_https(name: str, url: str) -> str:
    """Return `url` if it is https:// (plain http only for localhost), else raise.

    The message names the setting but never echoes its value, which could be a
    secret pasted into the wrong variable.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https" or (scheme == "http" and (parts.hostname or "") in _LOCAL_HOSTS):
        return url
    raise ValueError(
        f"{name} must be an https:// URL (plain http only for localhost); "
        f"got {'an ' + scheme + '://' if scheme else 'a value with no scheme'}"
    )


def get_config_json(name: str, default: Any = None) -> Any:
    """Return a JSON config value from the environment or `.env`, else `default`."""
    text = (get_config(name) or "").strip()
    return json.loads(text) if text else default


def _env_value(name: str) -> tuple[bool, str | None]:
    """Resolve an env var, honoring an optional platform namespace prefix.

    Some hosts inject stored secrets under a namespace prefix. Set
    ``ROTOR_ENV_PREFIX`` so a value rotor reads as ``SERA_API_KEY`` still
    resolves when the platform injected it as ``<PREFIX>SERA_API_KEY`` — no
    per-name renaming or guessing. Order: the real environment (bare name,
    then prefixed), then `.env` (bare, then prefixed), so a real variable is
    never hidden by a `.env` line, even a blank one.
    """
    prefix = os.environ.get("ROTOR_ENV_PREFIX") or _DOTENV.get("ROTOR_ENV_PREFIX", "")
    names = [name, f"{prefix}{name}"] if prefix else [name]
    for source in (os.environ, _DOTENV):
        for key in names:
            if key in source:
                return True, source[key]
    return False, None


def _load_dotenv_once() -> None:
    """Load `.env` files once without overriding real process environment."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    _DOTENV.clear()
    for path in _dotenv_paths():
        if path.is_file():
            _load_dotenv_file(path)


def _dotenv_paths() -> list[Path]:
    """Return likely deployment `.env` locations."""
    cwd_env = Path.cwd() / ".env"
    package_env = Path(__file__).resolve().parents[1] / ".env"
    paths: list[Path] = []
    for path in (cwd_env, package_env):
        if path not in paths:
            paths.append(path)
    return paths


def _load_dotenv_file(path: Path) -> None:
    """Parse a small KEY=VALUE `.env` file; the first file to set a key wins."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _valid_env_key(key):
            # Surface (without the value) that a non-empty line was ignored, so a
            # typo'd key is not silently dropped into the call-site default.
            _log.warning("ignoring malformed .env line in %s (invalid key)", path.name)
            continue
        _DOTENV.setdefault(key, _parse_dotenv_value(value.strip()))


def _valid_env_key(key: str) -> bool:
    """Return whether `key` is a conventional shell env var name."""
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def _parse_dotenv_value(value: str) -> str:
    """Parse a lightweight dotenv value with optional surrounding quotes.

    A quoted value is the text between its quotes; a ``# comment`` after the
    closing quote is dropped (``KEY="x"  # note`` gives ``x``). Unquoted values
    are taken verbatim (after trailing-whitespace trim): we deliberately do NOT
    strip an inline ``#`` comment there, because a secret/token may legitimately
    contain ``" #"`` and silently truncating it produces a wrong-but-non-empty
    credential. Quote a value to put a comment after it.
    """
    if value[:1] in {"'", '"'}:
        end = value.find(value[0], 1)
        rest = value[end + 1:].strip() if end > 0 else None
        if rest is not None and (not rest or rest.startswith("#")):
            return value[1:end]
    return value.rstrip()
