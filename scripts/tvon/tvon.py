#!/usr/bin/python3
"""
HDMI-CEC: автозапуск телевизора на Batocera.

Отдельный сервис. Логи → scripts/logs.log.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import threading
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


# RPi5: HDMI0=HDMI-A-1/card0, HDMI1=HDMI-A-2/card1
_DEFAULT_HDMI_OUTPUTS = ("HDMI-A-1", "HDMI-A-2")
_HDMI_TO_CARD = {
    "HDMI-A-1": "card0",
    "HDMI-A-2": "card1",
}


@dataclass(frozen=True)
class TvonConfig:
    enabled: bool = True
    device: int = 0
    osd_name: str = "Arcade"
    physical_address: str = "auto"
    hdmi_output: str = "auto"
    initial_delay_sec: int = 10
    interval_sec: int = 5
    max_wait_sec: int = 180
    hdmi_wait_sec: int = 60
    settle_sec: int = 15
    claim_retries: int = 3
    claim_interval_sec: int = 3
    claim_hold_sec: int = 12
    active_source_retries: int = 3
    active_source_recheck_sec: int = 3
    # Keep CEC session alive so TV does not drop Active Source after claim exits.
    # Seconds to hold after stages succeed (keeper stays up during stages until stop).
    active_source_keep_sec: int = 120
    refresh_display: bool = True
    refresh_audio: bool = True
    audio_hard_restart: bool = True
    # Unconditional ES restart last resort on stage picture (risky on Pi5+NVMe).
    restart_es: bool = False
    # After stages OK: restart ES only if it launched with settled display list [ ].
    restart_es_if_empty: bool = True
    es_restart_settle_sec: int = 8
    display_retries: int = 5
    display_retry_sec: int = 3
    picture_retries: int = 5
    audio_retries: int = 3
    post_check_attempts: int = 2
    post_check_sec: int = 8
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


def _read_str(section: dict, key: str, default: str) -> str:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


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
        osd_name=_read_str(section, "osd_name", defaults.osd_name),
        physical_address=_read_str(section, "physical_address", defaults.physical_address),
        hdmi_output=_read_str(section, "hdmi_output", defaults.hdmi_output),
        initial_delay_sec=_read_non_negative_int(
            section, "initial_delay_sec", defaults.initial_delay_sec
        ),
        interval_sec=_read_positive_int(section, "interval_sec", defaults.interval_sec),
        max_wait_sec=_read_positive_int(section, "max_wait_sec", defaults.max_wait_sec),
        hdmi_wait_sec=_read_positive_int(section, "hdmi_wait_sec", defaults.hdmi_wait_sec),
        settle_sec=_read_non_negative_int(section, "settle_sec", defaults.settle_sec),
        claim_retries=_read_positive_int(section, "claim_retries", defaults.claim_retries),
        claim_interval_sec=_read_positive_int(
            section, "claim_interval_sec", defaults.claim_interval_sec
        ),
        claim_hold_sec=_read_positive_int(section, "claim_hold_sec", defaults.claim_hold_sec),
        active_source_retries=_read_positive_int(
            section, "active_source_retries", defaults.active_source_retries
        ),
        active_source_recheck_sec=_read_non_negative_int(
            section, "active_source_recheck_sec", defaults.active_source_recheck_sec
        ),
        active_source_keep_sec=_read_positive_int(
            section, "active_source_keep_sec", defaults.active_source_keep_sec
        ),
        refresh_display=_read_bool(section, "refresh_display", defaults.refresh_display),
        refresh_audio=_read_bool(section, "refresh_audio", defaults.refresh_audio),
        audio_hard_restart=_read_bool(
            section, "audio_hard_restart", defaults.audio_hard_restart
        ),
        restart_es=_read_bool(section, "restart_es", defaults.restart_es),
        restart_es_if_empty=_read_bool(
            section, "restart_es_if_empty", defaults.restart_es_if_empty
        ),
        es_restart_settle_sec=_read_positive_int(
            section, "es_restart_settle_sec", defaults.es_restart_settle_sec
        ),
        display_retries=_read_positive_int(section, "display_retries", defaults.display_retries),
        display_retry_sec=_read_positive_int(
            section, "display_retry_sec", defaults.display_retry_sec
        ),
        picture_retries=_read_positive_int(
            section, "picture_retries", defaults.picture_retries
        ),
        audio_retries=_read_positive_int(section, "audio_retries", defaults.audio_retries),
        post_check_attempts=_read_positive_int(
            section, "post_check_attempts", defaults.post_check_attempts
        ),
        post_check_sec=_read_non_negative_int(
            section, "post_check_sec", defaults.post_check_sec
        ),
        cec_debug=_read_non_negative_int(section, "cec_debug", defaults.cec_debug),
    )


def _cec_client() -> str:
    return shutil.which("cec-client") or "cec-client"


def physical_address_bytes(physical_address: str) -> str:
    """1.0.0.0 → '10:00' для CEC Active Source."""
    parts = [int(p) for p in physical_address.split(".")]
    while len(parts) < 4:
        parts.append(0)
    if len(parts) != 4 or any(p < 0 or p > 15 for p in parts):
        raise ValueError(f"invalid physical_address: {physical_address!r}")
    b0 = ((parts[0] & 0xF) << 4) | (parts[1] & 0xF)
    b1 = ((parts[2] & 0xF) << 4) | (parts[3] & 0xF)
    return f"{b0:02x}:{b1:02x}"


def cec_cmd(command: str, *, osd_name: str = "Arcade", debug: int = 1) -> str:
    """Одна команда (-s). Для power on/poll; Active Source так не удерживается."""
    try:
        result = subprocess.run(
            [_cec_client(), "-s", "-d", str(debug), "-o", osd_name],
            input=f"{command}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return ((result.stdout or "") + (result.stderr or "")).replace("\r", "")
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"TVON: cec '{command}' ERROR {error}")
        return str(error)


def cec_session(
    commands: list[str],
    *,
    osd_name: str = "Arcade",
    debug: int = 1,
    hold_sec: float = 12,
) -> str:
    """
    Одна сессия cec-client без -s и без 'q' (q гасит ТВ!).
    Active Source держится, пока процесс жив — hold_sec даёт ТВ время переключить вход.
    """
    proc = subprocess.Popen(
        [_cec_client(), "-d", str(debug), "-o", osd_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    chunks: list[str] = []

    def _drain_stdout() -> None:
        try:
            if proc.stdout:
                data = proc.stdout.read()
                if data:
                    chunks.append(data)
        except OSError:
            pass

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        for command in commands:
            if proc.poll() is not None:
                break
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            time.sleep(0.6)
        time.sleep(max(0.0, hold_sec))
    except (OSError, BrokenPipeError) as error:
        log(f"TVON: cec session ERROR {error}")
    finally:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        reader.join(timeout=5)
    return "".join(chunks).replace("\r", "")


def tv_power(device: int = 0, *, osd_name: str = "Arcade", debug: int = 1) -> str:
    out = cec_cmd(f"pow {device}", osd_name=osd_name, debug=debug)
    match = re.search(r"power status:\s*(\S+)", out)
    return match.group(1) if match else "unknown"


def wake_tv(device: int = 0, *, osd_name: str = "Arcade", debug: int = 1) -> None:
    # Как в первой рабочей версии: on + as на каждой попытке.
    # Иначе ТВ может включиться, но остаться на другом входе — DRM так и не станет connected.
    cec_cmd(f"on {device}", osd_name=osd_name, debug=debug)
    cec_cmd("as", osd_name=osd_name, debug=debug)


def parse_active_source(scan_out: str) -> str:
    match = re.search(r"currently active source:\s*(.+)", scan_out, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "unknown"


def active_source_ok(active: str, physical_address: str) -> bool:
    lowered = active.lower()
    if "unknown" in lowered or "-1" in lowered:
        return False
    if physical_address in active:
        return True
    if "recorder" in lowered or "playback" in lowered or "arcade" in lowered:
        return True
    return bool(re.search(r"\(\d+\)", active))


def hdmi_output_candidates(hdmi_output: str) -> tuple[str, ...]:
    """hdmi_output=auto → оба порта RPi5; иначе список через запятую или один выход."""
    value = hdmi_output.strip()
    if not value or value.lower() == "auto":
        return _DEFAULT_HDMI_OUTPUTS
    parts = tuple(p.strip() for p in value.split(",") if p.strip())
    return parts or _DEFAULT_HDMI_OUTPUTS


def hdmi_connector_paths(output_name: str) -> list[str]:
    needle = output_name.strip()
    if needle.lower() == "auto":
        return sorted(glob.glob("/sys/class/drm/card*-HDMI*/status"))
    paths = sorted(glob.glob(f"/sys/class/drm/card*-{needle}/status"))
    if paths:
        return paths
    return sorted(glob.glob("/sys/class/drm/card*-HDMI*/status"))


def hdmi_link_status(output_name: str) -> str:
    paths = hdmi_connector_paths(output_name)
    if not paths:
        return "missing"
    statuses = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as status_file:
                statuses.append(status_file.read().strip() or "empty")
        except OSError:
            statuses.append("error")
    if any(s == "connected" for s in statuses):
        return "connected"
    return statuses[0] if statuses else "missing"


def detect_connected_hdmi(candidates: tuple[str, ...]) -> str | None:
    """Первый DRM HDMI со status=connected среди кандидатов."""
    for name in candidates:
        if hdmi_link_status(name) == "connected":
            return name
    return None


def hdmi_drm_statuses(candidates: tuple[str, ...]) -> dict[str, str]:
    return {name: hdmi_link_status(name) for name in candidates}


def hdmi_drm_unreliable(statuses: dict[str, str]) -> bool:
    """sysfs/DRM не читается (часто при I/O stress) — тяжёлый refresh опасен."""
    if not statuses:
        return True
    return any(s in ("error", "missing", "empty") for s in statuses.values())


def wait_hdmi_connected(tvon: TvonConfig) -> str | None:
    candidates = hdmi_output_candidates(tvon.hdmi_output)
    log(f"TVON: hdmi-wait candidates={','.join(candidates)} timeout={tvon.hdmi_wait_sec}s")
    deadline = time.monotonic() + tvon.hdmi_wait_sec
    while time.monotonic() < deadline:
        found = detect_connected_hdmi(candidates)
        if found:
            log(f"TVON: hdmi-wait ok output={found}")
            return found
        statuses = hdmi_drm_statuses(candidates)
        log(f"TVON: hdmi-wait status={statuses}")
        time.sleep(min(2, tvon.interval_sec))
    last = hdmi_drm_statuses(candidates)
    log(f"TVON: hdmi-wait timeout, last={last}")
    return None


def resolve_physical_address(tvon: TvonConfig, hdmi_output: str | None) -> str:
    """
    auto: из cec scan (наш OSD) или эвристика по порту RPi5
    (HDMI-A-1→1.0.0.0, HDMI-A-2→2.0.0.0).
    """
    configured = tvon.physical_address.strip()
    if configured.lower() != "auto":
        return configured

    try:
        out = cec_cmd("scan", osd_name=tvon.osd_name, debug=tvon.cec_debug)
    except Exception as error:
        log(f"TVON: pa scan ERROR {error}")
        out = ""

    # Ищем physical address у устройства с нашим OSD / Recorder 1
    blocks = re.split(r"\ndevice #", out)
    for block in blocks:
        low = block.lower()
        if tvon.osd_name.lower() not in low and "recorder 1" not in low:
            continue
        match = re.search(r"physical address:\s*([\d.]+)", block, re.IGNORECASE)
        if match:
            pa = match.group(1).strip()
            log(f"TVON: pa from cec scan={pa}")
            return pa

    # Fallback по порту
    if hdmi_output == "HDMI-A-2":
        log("TVON: pa fallback 2.0.0.0 (HDMI-A-2)")
        return "2.0.0.0"
    log("TVON: pa fallback 1.0.0.0")
    return "1.0.0.0"


def physical_address_candidates(primary: str) -> tuple[str, ...]:
    """На claim пробуем primary, затем второй типичный PA RPi5."""
    alts = ("1.0.0.0", "2.0.0.0")
    ordered = [primary]
    for pa in alts:
        if pa not in ordered:
            ordered.append(pa)
    return tuple(ordered)


def wait_tv_power_on(tvon: TvonConfig) -> str:
    elapsed = 0
    status = "unknown"
    while elapsed < tvon.max_wait_sec:
        log(f"TVON: wake elapsed={elapsed}s")
        wake_tv(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
        time.sleep(2)
        status = tv_power(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
        log(f"TVON: power={status}")
        if status == "on":
            return status
        time.sleep(tvon.interval_sec)
        elapsed += tvon.interval_sec + 2
    return status


def claim_input_session(tvon: TvonConfig, physical_address: str) -> tuple[bool, str]:
    """as + Image View On + Active Source в одной сессии; sp нельзя (только ТВ)."""
    pa_hex = physical_address_bytes(physical_address)
    commands = [
        f"on {tvon.device}",
        "as",
        "tx 10:04",
        f"tx 1f:82:{pa_hex}",
        "as",
        "scan",
    ]
    log(f"TVON: claim session pa={physical_address} hold={tvon.claim_hold_sec}s")
    out = cec_session(
        commands,
        osd_name=tvon.osd_name,
        debug=tvon.cec_debug,
        hold_sec=tvon.claim_hold_sec,
    )
    active = parse_active_source(out)
    log(f"TVON: verify active_source={active}")
    return active_source_ok(active, physical_address), active


def claim_with_retries(tvon: TvonConfig, physical_address: str) -> bool:
    """Legacy short claim (exits cec-client). Prefer ensure_active_source for hold."""
    pas = physical_address_candidates(physical_address)
    log(f"TVON: claim input retries={tvon.claim_retries} pas={','.join(pas)}")
    for attempt in range(1, tvon.claim_retries + 1):
        pa = pas[(attempt - 1) % len(pas)]
        log(f"TVON: claim attempt {attempt}/{tvon.claim_retries} pa={pa}")
        ok, _active = claim_input_session(tvon, pa)
        if ok:
            return True
        if attempt < tvon.claim_retries:
            time.sleep(tvon.claim_interval_sec)
    return False


def query_active_source(tvon: TvonConfig) -> str:
    """Independent CEC scan — catches Active Source drop after cec-client exits."""
    out = cec_cmd("scan", osd_name=tvon.osd_name, debug=tvon.cec_debug)
    active = parse_active_source(out)
    log(f"TVON: verify active_source={active}")
    return active


# Background CEC hold: this TV clears Active Source as soon as cec-client exits.
_source_keeper_thread: threading.Thread | None = None
_source_keeper_proc: subprocess.Popen[str] | None = None
_source_keeper_stop = threading.Event()
_source_claim_ok = False


def clear_stale_cec_clients() -> None:
    """Kill leftover cec-client (orphans from prior TVON) so the adapter is free."""
    stop_active_source_keeper()
    killed = 0
    for tool in ("killall", "pkill"):
        if not shutil.which(tool):
            continue
        try:
            if tool == "killall":
                result = subprocess.run(
                    ["killall", "-q", "cec-client"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            else:
                result = subprocess.run(
                    ["pkill", "-x", "cec-client"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            if result.returncode in (0, 1):
                killed = 1
                break
        except (OSError, subprocess.TimeoutExpired) as error:
            log(f"TVON: clear cec via {tool} ERROR {error}")
    if killed:
        time.sleep(0.8)
        log("TVON: cleared stale cec-client")


def stop_active_source_keeper() -> None:
    global _source_keeper_thread, _source_keeper_proc, _source_claim_ok
    _source_keeper_stop.set()
    proc = _source_keeper_proc
    if proc is not None and proc.poll() is None:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    thread = _source_keeper_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=8)
    _source_keeper_thread = None
    _source_keeper_proc = None
    _source_claim_ok = False


def _keeper_write(proc: subprocess.Popen[str], commands: list[str]) -> bool:
    try:
        assert proc.stdin is not None
        for command in commands:
            if proc.poll() is not None:
                return False
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
            time.sleep(0.6)
        return True
    except (OSError, BrokenPipeError, AssertionError):
        return False


def start_active_source_keeper(tvon: TvonConfig, physical_address: str) -> None:
    """
    One long-lived cec-client: claim Active Source, then keep the process open
    (with periodic refresh) until stop_active_source_keeper().
    """
    global _source_keeper_thread, _source_keeper_proc, _source_claim_ok
    stop_active_source_keeper()
    _source_keeper_stop.clear()
    pa_hex = physical_address_bytes(physical_address)
    refresh_sec = max(8.0, float(min(20, tvon.claim_hold_sec)))

    def _run() -> None:
        global _source_keeper_proc, _source_claim_ok
        log(
            f"TVON: active_source keeper start refresh={int(refresh_sec)}s "
            f"pa={physical_address}"
        )
        proc = subprocess.Popen(
            [_cec_client(), "-d", str(tvon.cec_debug), "-o", tvon.osd_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _source_keeper_proc = proc
        chunks: list[str] = []

        def _drain() -> None:
            try:
                if proc.stdout:
                    data = proc.stdout.read()
                    if data:
                        chunks.append(data)
            except OSError:
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            claim_cmds = [
                f"on {tvon.device}",
                "as",
                "tx 10:04",
                f"tx 1f:82:{pa_hex}",
                "as",
            ]
            if not _keeper_write(proc, claim_cmds):
                log("TVON: active_source keeper claim write failed")
                _source_claim_ok = False
                return
            _source_claim_ok = True
            log("TVON: active_source keeper claimed")
            next_refresh = time.monotonic() + refresh_sec
            while not _source_keeper_stop.is_set():
                if proc.poll() is not None:
                    log("TVON: active_source keeper process exited early")
                    _source_claim_ok = False
                    break
                now = time.monotonic()
                if now >= next_refresh:
                    if not _keeper_write(
                        proc, ["as", f"tx 1f:82:{pa_hex}", "as"]
                    ):
                        log("TVON: active_source keeper refresh write failed")
                        _source_claim_ok = False
                        break
                    log("TVON: active_source keeper refresh")
                    next_refresh = time.monotonic() + refresh_sec
                time.sleep(0.5)
        except (OSError, BrokenPipeError) as error:
            log(f"TVON: active_source keeper ERROR {error}")
            _source_claim_ok = False
        finally:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            reader.join(timeout=3)
            if _source_keeper_proc is proc:
                _source_keeper_proc = None
            log("TVON: active_source keeper stop")

    _source_keeper_thread = threading.Thread(
        target=_run, name="tvon-cec-keeper", daemon=True
    )
    _source_keeper_thread.start()
    # Wait until claim commands land (5 cmds × 0.6s) or thread dies.
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        if _source_claim_ok:
            break
        thread = _source_keeper_thread
        if thread is None or not thread.is_alive():
            break
        time.sleep(0.2)


def source_keeper_alive() -> bool:
    proc = _source_keeper_proc
    if proc is not None and proc.poll() is None and _source_claim_ok:
        return True
    return (
        _source_keeper_thread is not None
        and _source_keeper_thread.is_alive()
        and _source_claim_ok
    )


def verify_active_source(tvon: TvonConfig, physical_address: str) -> bool:
    # Competing scan while keeper holds the bus knocks Active Source to unknown(-1).
    if source_keeper_alive():
        log("TVON: verify active_source keeper=alive")
        return True
    active = query_active_source(tvon)
    ok = active_source_ok(active, physical_address)
    if ok:
        return True
    log(
        f"TVON: active_source not ours (want pa={physical_address}, got={active})"
    )
    return False


def ensure_active_source(tvon: TvonConfig, physical_address: str) -> bool:
    """
    Claim + hold in one continuous cec-client session (no exit between claim and hold).
    """
    if source_keeper_alive():
        log("TVON: active_source already held by keeper")
        return True

    pas = physical_address_candidates(physical_address)
    for attempt in range(1, tvon.active_source_retries + 1):
        pa = pas[(attempt - 1) % len(pas)]
        log(
            f"TVON: active_source ensure "
            f"{attempt}/{tvon.active_source_retries} pa={pa}"
        )
        start_active_source_keeper(tvon, pa)
        if tvon.active_source_recheck_sec > 0:
            time.sleep(tvon.active_source_recheck_sec)
        if verify_active_source(tvon, pa):
            log(f"TVON: active_source ok (check {attempt})")
            return True
        stop_active_source_keeper()
        log("TVON: active_source keeper did not stick")
        wake_tv(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
        time.sleep(tvon.claim_interval_sec)

    ok = verify_active_source(tvon, physical_address)
    if ok:
        log("TVON: active_source ok after recover")
    else:
        log("TVON: active_source still lost after retries")
    return ok


def _display_env() -> dict[str, str]:
    """batocera-resolution/wlr-randr нужен Wayland сокет labwc."""
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", "/var/run")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


def refresh_batocera_output(tvon: TvonConfig, preferred: str | None) -> str | None:
    """После boot без ТВ ES часто без выхода — setOutput на живой HDMI."""
    tool = shutil.which("batocera-resolution")
    if not tool:
        log("TVON: batocera-resolution not found, skip display refresh")
        return None

    candidates = hdmi_output_candidates(tvon.hdmi_output)
    # Сначала детект / preferred, потом остальные
    ordered: list[str] = []
    live = preferred or detect_connected_hdmi(candidates)
    if live:
        ordered.append(live)
    for name in candidates:
        if name not in ordered:
            ordered.append(name)

    env = _display_env()
    for attempt in range(1, tvon.display_retries + 1):
        for output in ordered:
            log(
                f"TVON: refresh display setOutput {output} "
                f"({attempt}/{tvon.display_retries})"
            )
            try:
                result = subprocess.run(
                    [tool, "setOutput", output],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                    env=env,
                )
                if result.returncode == 0:
                    log(f"TVON: setOutput ok ({output})")
                    return output
                err = ((result.stdout or "") + (result.stderr or "")).strip()[:200]
                log(
                    f"TVON: setOutput {output} failed rc={result.returncode} "
                    f"{err or '(no output — often no WAYLAND_DISPLAY)'}"
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                log(f"TVON: setOutput {output} ERROR {error}")
        if attempt < tvon.display_retries:
            time.sleep(tvon.display_retry_sec)
    return None


def _run_init_script(script: str, action: str, timeout: int = 60) -> int:
    path = f"/etc/init.d/{script}"
    if not os.path.isfile(path):
        log(f"TVON: {path} missing")
        return 1
    try:
        result = subprocess.run(
            [path, action],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_display_env(),
        )
        out = ((result.stdout or "") + (result.stderr or "")).strip()[:200]
        if out:
            log(f"TVON: {script} {action}: {out}")
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"TVON: {script} {action} ERROR {error}")
        return 1


def _audio_card_for_output(hdmi_output: str | None) -> str | None:
    if not hdmi_output:
        return None
    return _HDMI_TO_CARD.get(hdmi_output)


def _pick_hdmi_sink(prefer_card: str | None = None) -> str | None:
    tool = shutil.which("batocera-audio")
    if not tool:
        return None
    try:
        result = subprocess.run(
            [tool, "list"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_display_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    hdmi_sinks: list[str] = []
    for line in (result.stdout or "").splitlines():
        if not line.strip() or "\t" not in line:
            continue
        sink_id, _desc = line.split("\t", 1)
        sink_id = sink_id.strip()
        if "hdmi" in sink_id.lower() and sink_id not in {"auto", "auto_null"}:
            hdmi_sinks.append(sink_id)

    if prefer_card:
        for sink_id in hdmi_sinks:
            if prefer_card in sink_id.lower():
                return sink_id
    return hdmi_sinks[0] if hdmi_sinks else None


def _pick_hdmi_profile(prefer_card: str | None = None) -> str | None:
    tool = shutil.which("batocera-audio")
    if not tool:
        return None
    try:
        result = subprocess.run(
            [tool, "list-profiles"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_display_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    stereo_preferred = None
    stereo_any = None
    any_hdmi = None
    for line in (result.stdout or "").splitlines():
        if not line.strip() or "\t" not in line:
            continue
        profile_id, _desc = line.split("\t", 1)
        profile_id = profile_id.strip()
        low = profile_id.lower()
        if "hdmi" not in low:
            continue
        if any_hdmi is None:
            any_hdmi = profile_id
        if "hdmi-stereo" in low:
            if prefer_card and prefer_card in low:
                stereo_preferred = profile_id
            elif stereo_any is None:
                stereo_any = profile_id
    return stereo_preferred or stereo_any or any_hdmi


def _apply_hdmi_audio(
    prefer_card: str | None,
    *,
    attempts: int = 5,
    allow_audio_start: bool = True,
) -> bool:
    """Профиль + sink HDMI через batocera-audio (без обязательного S06 restart)."""
    tool = shutil.which("batocera-audio")
    if not tool:
        log("TVON: batocera-audio not found")
        return False

    env = _display_env()
    last_err = ""
    for attempt in range(1, attempts + 1):
        time.sleep(2 if attempt > 1 else 1)
        profile = _pick_hdmi_profile(prefer_card)
        if profile:
            log(f"TVON: set-profile {profile} (try {attempt}/{attempts})")
            subprocess.run(
                [tool, "set-profile", profile],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=env,
            )

        sink = _pick_hdmi_sink(prefer_card)
        if not sink:
            log(f"TVON: no HDMI sink yet (try {attempt}/{attempts})")
            if allow_audio_start and attempt == max(3, attempts // 2):
                # PipeWire мог не подняться после restart — ещё один старт
                _run_init_script("S06audio", "start", timeout=90)
                time.sleep(2)
                _run_init_script("S27audioconfig", "start", timeout=90)
            continue

        log(f"TVON: set sink {sink} (try {attempt}/{attempts})")
        result = subprocess.run(
            [tool, "set", sink],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            vol = subprocess.run(
                [tool, "setSystemVolume", "100"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=env,
            )
            if vol.returncode != 0:
                log("TVON: setSystemVolume 100 failed (non-fatal)")
            log("TVON: audio HDMI ok")
            return True
        last_err = ((result.stdout or "") + (result.stderr or "")).strip()[:200]
        log(f"TVON: audio set failed rc={result.returncode} {last_err}")

    if last_err:
        log(f"TVON: audio refresh gave up ({last_err})")
    else:
        log("TVON: no HDMI sink in batocera-audio list yet")
    return False


def refresh_batocera_audio(
    tvon: TvonConfig,
    hdmi_output: str | None,
    *,
    soft_only: bool = False,
) -> bool:
    """
    Если HDMI не было при старте PipeWire — остаётся Dummy (auto_null).
    soft_only: только set-profile/set (без S06audio restart) — безопаснее, когда
    картинка уже есть; полный restart PipeWire на части плат зависает систему.
    """
    prefer_card = _audio_card_for_output(hdmi_output)
    log(
        f"TVON: refresh audio prefer={hdmi_output or 'any'} "
        f"card={prefer_card or 'any'} soft_only={soft_only}"
    )
    if soft_only:
        return _apply_hdmi_audio(prefer_card, attempts=3, allow_audio_start=False)

    _run_init_script("S06audio", "restart", timeout=90)
    time.sleep(3)
    _run_init_script("S27audioconfig", "restart", timeout=90)
    return _apply_hdmi_audio(prefer_card, attempts=5, allow_audio_start=True)


def restart_emulationstation() -> None:
    """
    batocera-es-swissknife --restart на части Pi5 (NVMe) даёт чёрный экран
    и Input/output error на /userdata. Безусловный last resort — restart_es;
    штатный путь — restart_es_if_empty только при settled list [ ].
    """
    tool = shutil.which("batocera-es-swissknife")
    if not tool:
        log("TVON: batocera-es-swissknife not found, skip ES restart")
        return
    log("TVON: restart EmulationStation")
    try:
        result = subprocess.run(
            [tool, "--restart"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=_display_env(),
        )
        if result.returncode != 0:
            err = ((result.stdout or "") + (result.stderr or "")).strip()[:200]
            log(f"TVON: ES restart failed rc={result.returncode} {err}")
        else:
            log("TVON: ES restart ok")
    except (OSError, subprocess.TimeoutExpired) as error:
        log(f"TVON: ES restart ERROR {error}")


_SCREEN_CHECKER_STATUS = "/var/run/batocera-switch-screen-checker-status"
_DISPLAY_LOG = "/userdata/system/logs/display.log"


def _parse_output_list_token(raw: str) -> tuple[str, ...]:
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts


def settled_display_outputs_from_status() -> tuple[str, ...] | None:
    """Current checker status file; None if missing."""
    if not os.path.isfile(_SCREEN_CHECKER_STATUS):
        return None
    try:
        with open(_SCREEN_CHECKER_STATUS, encoding="utf-8") as handle:
            return _parse_output_list_token(handle.read())
    except OSError:
        return None


def es_launched_with_empty_outputs() -> bool:
    """
    True if the latest EmulationStation launch used an empty settled display list.
    Prefer the nearest signal before the last 'Launching EmulationStation'
    (Updated/Assigned/settled) — an older empty Checker-Init must not win over a
    later 'Updated video outputs: HDMI-A-1' from a good launch.
    """
    try:
        with open(_DISPLAY_LOG, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        status = settled_display_outputs_from_status()
        empty = status is not None and len(status) == 0
        log(
            f"TVON: settled list (status only)="
            f"{list(status) if status is not None else 'missing'} empty={empty}"
        )
        return empty

    launches = [
        m.start() for m in re.finditer(r"Launching EmulationStation", text)
    ]
    if not launches:
        status = settled_display_outputs_from_status()
        empty = status is not None and len(status) == 0
        log(
            f"TVON: settled list (no ES launch in log, status)="
            f"{list(status) if status is not None else 'missing'} empty={empty}"
        )
        return empty

    before = text[: launches[-1]]
    signals: list[tuple[int, tuple[str, ...], str]] = []
    for match in re.finditer(
        r"Storing settled display list:\s*\[([^\]]*)\]", before
    ):
        signals.append(
            (match.end(), _parse_output_list_token(match.group(1)), "settled")
        )
    for match in re.finditer(
        r"Assigned from file\s*-\s*(.+)", before, re.IGNORECASE
    ):
        signals.append(
            (match.end(), _parse_output_list_token(match.group(1)), "assigned")
        )
    for match in re.finditer(r"Updated video outputs:\s*(.+)", before):
        signals.append(
            (match.end(), _parse_output_list_token(match.group(1)), "updated")
        )

    if signals:
        _pos, outputs, kind = max(signals, key=lambda item: item[0])
        empty = len(outputs) == 0
        log(
            f"TVON: outputs at last ES launch ({kind})="
            f"{list(outputs) or '[]'} empty={empty}"
        )
        return empty

    status = settled_display_outputs_from_status()
    empty = status is not None and len(status) == 0
    log(
        f"TVON: settled list fallback status="
        f"{list(status) if status is not None else 'missing'} empty={empty}"
    )
    return empty


def recover_es_if_empty_outputs(
    tvon: TvonConfig, active_hdmi: str | None
) -> bool:
    """
    After successful TV/HDMI/CEC stages: restart ES only when it started with
    settled display list [ ] (slow TV). Fast TV already had HDMI in the list — skip.
    """
    if not tvon.restart_es_if_empty:
        return False
    if not es_launched_with_empty_outputs():
        log("TVON: ES settled outputs non-empty — skip ES restart")
        return False
    log(
        "TVON: ES launched with empty settled display list [ ] — "
        "setOutput + ES restart"
    )
    if tvon.refresh_display and active_hdmi:
        refresh_batocera_output(tvon, active_hdmi)
    restart_emulationstation()
    time.sleep(tvon.es_restart_settle_sec)
    return True


def verify_tv_on(tvon: TvonConfig) -> bool:
    status = tv_power(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
    log(f"TVON: verify tv power={status}")
    return status == "on"


def verify_hdmi_link(tvon: TvonConfig) -> str | None:
    candidates = hdmi_output_candidates(tvon.hdmi_output)
    found = detect_connected_hdmi(candidates)
    statuses = hdmi_drm_statuses(candidates)
    log(f"TVON: verify hdmi link={found or 'none'} status={statuses}")
    return found


def _wayland_ready() -> bool:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/var/run")
    for name in ("wayland-0", "wayland-1"):
        if os.path.exists(os.path.join(runtime, name)):
            return True
    return False


def _drm_output_has_mode(output_name: str) -> bool:
    """True if connector looks driven (enabled and/or non-empty modes)."""
    for status_path in hdmi_connector_paths(output_name):
        conn_dir = os.path.dirname(status_path)
        enabled_path = os.path.join(conn_dir, "enabled")
        modes_path = os.path.join(conn_dir, "modes")
        enabled_ok = True
        modes_ok = True
        try:
            if os.path.isfile(enabled_path):
                with open(enabled_path, encoding="utf-8") as handle:
                    enabled_ok = handle.read().strip().lower() == "enabled"
        except OSError:
            enabled_ok = False
        try:
            if os.path.isfile(modes_path):
                with open(modes_path, encoding="utf-8") as handle:
                    modes_ok = bool(handle.read().strip())
        except OSError:
            modes_ok = False
        if enabled_ok and modes_ok:
            return True
        # Some kernels only expose one of the two.
        if os.path.isfile(enabled_path) and enabled_ok:
            return True
        if os.path.isfile(modes_path) and modes_ok and not os.path.isfile(enabled_path):
            return True
    return False


def verify_picture(output: str | None) -> bool:
    if not output:
        log("TVON: verify picture fail (no output)")
        return False
    if hdmi_link_status(output) != "connected":
        log(f"TVON: verify picture fail ({output} not connected)")
        return False
    mode_ok = _drm_output_has_mode(output)
    wayland_ok = _wayland_ready()
    log(
        f"TVON: verify picture output={output} drm_mode={mode_ok} "
        f"wayland={wayland_ok}"
    )
    # Wayland may lag briefly; require DRM driven. Soft-warn if no Wayland.
    if not mode_ok:
        return False
    if not wayland_ok:
        log("TVON: verify picture: Wayland socket missing (non-fatal if DRM ok)")
    return True


def verify_audio_hdmi(hdmi_output: str | None) -> bool:
    prefer_card = _audio_card_for_output(hdmi_output)
    sink = _pick_hdmi_sink(prefer_card)
    log(f"TVON: verify audio sink={sink or 'none'} card={prefer_card or 'any'}")
    return sink is not None


def stage_tv_power(tvon: TvonConfig) -> bool:
    log("TVON: stage 1 TV power")
    if verify_tv_on(tvon):
        log("TVON: stage 1 ok (already on)")
        return True
    status = wait_tv_power_on(tvon)
    ok = status == "on"
    if ok:
        log("TVON: stage 1 ok")
    else:
        log(f"TVON: stage 1 fail last power={status}")
    return ok


def stage_hdmi_claim(tvon: TvonConfig) -> tuple[bool, str | None, str]:
    """Return (ok, live_hdmi, physical_address). ok requires DRM + stable Active Source."""
    log("TVON: stage 2 HDMI link + CEC claim")
    candidates = hdmi_output_candidates(tvon.hdmi_output)
    live = wait_hdmi_connected(tvon)
    if not live:
        live = detect_connected_hdmi(candidates)
        log(f"TVON: HDMI not confirmed yet live={live}")
        wake_tv(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
        time.sleep(3)
        live = detect_connected_hdmi(candidates) or live

    if not live:
        log("TVON: stage 2 fail (no DRM connected)")
        return False, None, "1.0.0.0"

    if tvon.settle_sec > 0:
        log(f"TVON: settle {tvon.settle_sec}s")
        time.sleep(tvon.settle_sec)

    physical_address = resolve_physical_address(tvon, live)
    # claim + background keeper (TV drops Active Source when cec-client exits)
    source_ok = ensure_active_source(tvon, physical_address)
    live = detect_connected_hdmi(candidates) or live

    if not source_ok:
        log("TVON: stage 2 recover wake+ensure active_source")
        wake_tv(tvon.device, osd_name=tvon.osd_name, debug=tvon.cec_debug)
        time.sleep(tvon.claim_interval_sec)
        live = detect_connected_hdmi(candidates) or live
        physical_address = resolve_physical_address(tvon, live)
        source_ok = ensure_active_source(tvon, physical_address)
        live = detect_connected_hdmi(candidates) or live

    ok = bool(live) and source_ok
    if ok:
        log(f"TVON: stage 2 ok display={live} pa={physical_address} active_source=ok")
    else:
        log(f"TVON: stage 2 fail display={live} active_source={source_ok}")
    return ok, live, physical_address


def stage_picture(
    tvon: TvonConfig,
    live_hdmi: str | None,
    physical_address: str,
) -> str | None:
    log("TVON: stage 3 picture")
    active = live_hdmi
    for attempt in range(1, tvon.picture_retries + 1):
        pic_ok = verify_picture(active)
        src_ok = verify_active_source(tvon, physical_address)
        if pic_ok and src_ok:
            log(f"TVON: stage 3 ok display={active} active_source=ok")
            return active

        log(
            f"TVON: stage 3 recover {attempt}/{tvon.picture_retries} "
            f"picture={pic_ok} active_source={src_ok}"
        )
        if not src_ok:
            ensure_active_source(tvon, physical_address)
        if tvon.refresh_display and (not pic_ok or attempt > 1):
            active = refresh_batocera_output(tvon, active) or active
        if attempt == 2 or attempt == tvon.picture_retries:
            log("TVON: stage 3 recover re-claim")
            stop_active_source_keeper()
            ensure_active_source(tvon, physical_address)
            candidates = hdmi_output_candidates(tvon.hdmi_output)
            active = detect_connected_hdmi(candidates) or active
        if (
            attempt == tvon.picture_retries
            and tvon.restart_es
            and not verify_picture(active)
        ):
            log("TVON: stage 3 last resort ES restart")
            restart_emulationstation()
            time.sleep(tvon.display_retry_sec)
            if tvon.refresh_display:
                active = refresh_batocera_output(tvon, active) or active
            ensure_active_source(tvon, physical_address)

        time.sleep(tvon.display_retry_sec)

    pic_ok = verify_picture(active)
    src_ok = verify_active_source(tvon, physical_address)
    if pic_ok and src_ok:
        log(f"TVON: stage 3 ok display={active}")
        return active
    log(f"TVON: stage 3 fail display={active} picture={pic_ok} active_source={src_ok}")
    return active


def stage_audio(tvon: TvonConfig, active_hdmi: str | None, physical_address: str) -> bool:
    log("TVON: stage 4 audio")
    if not tvon.refresh_audio:
        log("TVON: stage 4 skipped (refresh_audio=false)")
        return True

    def _picture_and_source_ok() -> bool:
        return verify_picture(active_hdmi) and verify_active_source(
            tvon, physical_address
        )

    if verify_audio_hdmi(active_hdmi):
        ok = refresh_batocera_audio(tvon, active_hdmi, soft_only=True)
        if ok and _picture_and_source_ok():
            log("TVON: stage 4 ok (soft)")
            return True
        if ok and not verify_active_source(tvon, physical_address):
            log("TVON: stage 4 active_source lost after soft audio — re-claim")
            ensure_active_source(tvon, physical_address)

    for attempt in range(1, tvon.audio_retries + 1):
        log(f"TVON: stage 4 attempt {attempt}/{tvon.audio_retries}")
        soft_ok = refresh_batocera_audio(tvon, active_hdmi, soft_only=True)
        if soft_ok and verify_audio_hdmi(active_hdmi):
            if not verify_picture(active_hdmi) and tvon.refresh_display:
                log("TVON: stage 4 picture lost after soft audio — setOutput")
                refresh_batocera_output(tvon, active_hdmi)
            if not verify_active_source(tvon, physical_address):
                ensure_active_source(tvon, physical_address)
            if verify_audio_hdmi(active_hdmi) and _picture_and_source_ok():
                log("TVON: stage 4 ok (soft)")
                return True

        if not tvon.audio_hard_restart:
            log("TVON: stage 4 soft fail, hard restart disabled")
            continue

        log("TVON: stage 4 recover hard S06audio restart")
        hard_ok = refresh_batocera_audio(tvon, active_hdmi, soft_only=False)
        if not verify_picture(active_hdmi):
            log("TVON: stage 4 picture lost after hard audio — setOutput")
            if tvon.refresh_display:
                refresh_batocera_output(tvon, active_hdmi)
        if not verify_active_source(tvon, physical_address):
            ensure_active_source(tvon, physical_address)
        if hard_ok and verify_audio_hdmi(active_hdmi) and _picture_and_source_ok():
            log("TVON: stage 4 ok (hard)")
            return True
        time.sleep(tvon.display_retry_sec)

    ok = verify_audio_hdmi(active_hdmi)
    if ok:
        log("TVON: stage 4 ok")
    else:
        log("TVON: stage 4 fail (no HDMI sink)")
    return ok


def post_check(
    tvon: TvonConfig,
    active_hdmi: str | None,
    physical_address: str,
) -> None:
    if tvon.post_check_attempts <= 0:
        return
    log(
        f"TVON: post-check attempts={tvon.post_check_attempts} "
        f"every={tvon.post_check_sec}s"
    )
    for attempt in range(1, tvon.post_check_attempts + 1):
        time.sleep(tvon.post_check_sec)
        pic_ok = verify_picture(active_hdmi)
        src_ok = verify_active_source(tvon, physical_address)
        aud_ok = (not tvon.refresh_audio) or verify_audio_hdmi(active_hdmi)
        log(
            f"TVON: post-check {attempt}/{tvon.post_check_attempts} "
            f"picture={pic_ok} active_source={src_ok} audio={aud_ok}"
        )
        if pic_ok and src_ok and aud_ok:
            continue
        if not src_ok:
            log("TVON: post-check recover active_source")
            ensure_active_source(tvon, physical_address)
        if not pic_ok:
            log("TVON: post-check recover picture")
            if tvon.refresh_display:
                refresh_batocera_output(tvon, active_hdmi)
            stop_active_source_keeper()
            ensure_active_source(tvon, physical_address)
        if not aud_ok and tvon.refresh_audio:
            log("TVON: post-check recover audio soft")
            refresh_batocera_audio(tvon, active_hdmi, soft_only=True)
            if not verify_active_source(tvon, physical_address):
                ensure_active_source(tvon, physical_address)


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
        f"TVON: config device={tvon.device} osd={tvon.osd_name!r} "
        f"pa={tvon.physical_address} hdmi={tvon.hdmi_output} "
        f"initial_delay={tvon.initial_delay_sec}s "
        f"max_wait={tvon.max_wait_sec}s hdmi_wait={tvon.hdmi_wait_sec}s "
        f"settle={tvon.settle_sec}s hold={tvon.claim_hold_sec}s "
        f"active_source_retries={tvon.active_source_retries} "
        f"keep={tvon.active_source_keep_sec}s "
        f"refresh_audio={tvon.refresh_audio} "
        f"audio_hard_restart={tvon.audio_hard_restart} "
        f"restart_es={tvon.restart_es} "
        f"restart_es_if_empty={tvon.restart_es_if_empty} "
        f"picture_retries={tvon.picture_retries} "
        f"audio_retries={tvon.audio_retries} "
        f"ssh_user={ssh_user}"
    )

    clear_stale_cec_clients()
    time.sleep(tvon.initial_delay_sec)

    candidates = hdmi_output_candidates(tvon.hdmi_output)
    statuses = hdmi_drm_statuses(candidates)
    if hdmi_drm_unreliable(statuses):
        log(f"TVON: DRM unreliable {statuses} — continue carefully")

    if not stage_tv_power(tvon):
        log(f"TVON: timeout after {tvon.max_wait_sec}s (stage 1)")
        return 1

    link_ok, live_hdmi, physical_address = stage_hdmi_claim(tvon)
    if not live_hdmi:
        log("TVON: abort — no HDMI link after stage 2")
        return 1
    if not link_ok:
        log("TVON: stage 2 active_source weak — stage 3 will re-claim")

    active_hdmi = stage_picture(tvon, live_hdmi, physical_address)
    audio_ok = stage_audio(tvon, active_hdmi, physical_address)
    post_check(tvon, active_hdmi, physical_address)

    pic_ok = verify_picture(active_hdmi)
    src_ok = verify_active_source(tvon, physical_address)
    # Stages OK + empty settled list at ES launch (slow TV) → one ES kick.
    # Fast TV already had HDMI in the list — do not restart.
    if pic_ok and src_ok and live_hdmi:
        if recover_es_if_empty_outputs(tvon, active_hdmi):
            pic_ok = verify_picture(active_hdmi)
            src_ok = verify_active_source(tvon, physical_address)
    log(
        f"TVON: TV on, display={active_hdmi}, "
        f"picture={pic_ok}, active_source={src_ok}, audio={audio_ok}, done"
    )
    # Process exit kills daemon threads — hold so TV stays on our input, then release.
    if pic_ok and live_hdmi:
        if not source_keeper_alive():
            start_active_source_keeper(tvon, physical_address)
        log(
            f"TVON: holding active_source "
            f"{tvon.active_source_keep_sec}s before exit"
        )
        time.sleep(tvon.active_source_keep_sec)
        stop_active_source_keeper()
    return 0 if (pic_ok and src_ok) else 1


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
