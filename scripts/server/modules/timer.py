"""
Прослойка server → сервис timer.

Живой timer — другой процесс; команды уходят на его Unix-сокет.
"""

from __future__ import annotations

import os
import tomllib
from typing import Optional

from main import log, cmd_send

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_SERVER_DIR, "config.toml")
DEFAULT_SOCKET = "/var/run/arcade-timer.sock"


def _socket_path() -> str:
    if not os.path.isfile(_CONFIG_PATH):
        return DEFAULT_SOCKET
    try:
        with open(_CONFIG_PATH, "rb") as config_file:
            data = tomllib.load(config_file)
        section = data.get("server", {})
        if isinstance(section, dict):
            value = section.get("timer_socket", DEFAULT_SOCKET)
            if isinstance(value, str) and value.strip():
                return value.strip()
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        pass
    return DEFAULT_SOCKET


def send(action: str, socket_path: Optional[str] = None) -> bool:
    """INCREASE / PLAYPAUSE / STOP / ADD_15 → Unix-сокет таймера."""
    path = socket_path or _socket_path()
    ok = cmd_send(path, action)
    if ok:
        log(f"TIMER bridge: sent {action!r} → {path}")
    return ok
