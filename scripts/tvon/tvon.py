#!/usr/bin/python3
"""
HDMI-CEC: автозапуск телевизора на Batocera.

Отдельный сервис (не связан с main-оркестратором).
SSH-учётка читается из scripts/main/config.toml.
Логи → scripts/logs.log.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
import time

_TVON_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_TVON_DIR)
_MAIN_DIR = os.path.join(_SCRIPTS_ROOT, "main")
_CONFIG_PATH = os.path.join(_TVON_DIR, "config.toml")

if _MAIN_DIR not in sys.path:
    sys.path.insert(0, _MAIN_DIR)

from main import log, load_ssh_config


@dataclass(frozen=True)
class TvonConfig:
    enabled: bool = True
    device: int = 0
    initial_delay_sec: int = 10
    interval_sec: int = 5
    max_wait_sec: int = 180
    cec_debug: int = 1


def _read_bool(section: dict, key: str, default: bool) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _read_positive_int(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value <= 0:
        raise ValueError(f"{key} must be greater than 0")
    return value


def _read_non_negative_int(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0:
        raise ValueError(f"{key} must be greater than or equal to 0")
    return value


def load_tvon_config() -> TvonConfig:
    defaults = TvonConfig()
    if not os.path.isfile(_CONFIG_PATH):
        return defaults
    with open(_CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)
    section = data.get("tvon", {})
    if not isinstance(section, dict):
        raise ValueError("[tvon] section must be a table")
    return TvonConfig(
        enabled=_read_bool(section, "enabled", defaults.enabled),
        device=_read_non_negative_int(section, "device", defaults.device),
        initial_delay_sec=_read_non_negative_int(
            section, "initial_delay_sec", defaults.initial_delay_sec
        ),
        interval_sec=_read_positive_int(section, "interval_sec", defaults.interval_sec),
        max_wait_sec=_read_positive_int(section, "max_wait_sec", defaults.max_wait_sec),
        cec_debug=_read_non_negative_int(section, "cec_debug", defaults.cec_debug),
    )


def _cec_client() -> str:
    return shutil.which("cec-client") or "cec-client"


def cec_cmd(command: str, debug: int = 1) -> str:
    try:
        result = subprocess.run(
            [_cec_client(), "-s", "-d", str(debug)],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return ((result.stdout or "") + (result.stderr or "")).replace("\r", "")
    except (OSError, subprocess.TimeoutExpired) as error:
        return str(error)


def tv_power(device: int = 0, debug: int = 1) -> str:
    out = cec_cmd(f"pow {device}", debug=debug)
    match = re.search(r"power status:\s*(\S+)", out)
    return match.group(1) if match else "unknown"


def wake_tv(device: int = 0, debug: int = 1) -> None:
    cec_cmd(f"on {device}", debug=debug)
    cec_cmd("as", debug=debug)


def active_source(debug: int = 1) -> None:
    cec_cmd("as", debug=debug)


def supervise(tvon: TvonConfig) -> int:
    log("TVON: start")
    if not tvon.enabled:
        log("TVON: disabled in config")
        return 0

    if not shutil.which("cec-client"):
        log("TVON: ERROR cec-client not found")
        return 1

    try:
        ssh = load_ssh_config()
        ssh_user = ssh.user
    except Exception:
        ssh_user = "?"

    log(
        f"TVON: config device={tvon.device} "
        f"initial_delay={tvon.initial_delay_sec}s "
        f"interval={tvon.interval_sec}s "
        f"max_wait={tvon.max_wait_sec}s "
        f"ssh_user={ssh_user}"
    )

    time.sleep(tvon.initial_delay_sec)

    elapsed = 0
    status = "unknown"
    while elapsed < tvon.max_wait_sec:
        log(f"TVON: wake elapsed={elapsed}s")
        wake_tv(tvon.device, debug=tvon.cec_debug)
        time.sleep(2)
        status = tv_power(tvon.device, debug=tvon.cec_debug)
        log(f"TVON: power={status}")

        if status == "on":
            active_source(debug=tvon.cec_debug)
            log("TVON: TV is on, done")
            return 0

        time.sleep(tvon.interval_sec)
        elapsed += tvon.interval_sec + 2

    log(f"TVON: timeout after {tvon.max_wait_sec}s, last power={status}")
    return 1


def run() -> int:
    try:
        tvon = load_tvon_config()
    except (tomllib.TOMLDecodeError, ValueError, OSError) as error:
        log(f"TVON: ERROR config {_CONFIG_PATH}: {error}")
        return 1
    return supervise(tvon)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("gameStart", "gameStop"):
        raise SystemExit(0)
    raise SystemExit(run())
