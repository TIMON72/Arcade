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
    refresh_display: bool = True
    refresh_audio: bool = True
    restart_es: bool = True
    display_retries: int = 5
    display_retry_sec: int = 3
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
        refresh_display=_read_bool(section, "refresh_display", defaults.refresh_display),
        refresh_audio=_read_bool(section, "refresh_audio", defaults.refresh_audio),
        restart_es=_read_bool(section, "restart_es", defaults.restart_es),
        display_retries=_read_positive_int(section, "display_retries", defaults.display_retries),
        display_retry_sec=_read_positive_int(
            section, "display_retry_sec", defaults.display_retry_sec
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
    cec_cmd(f"on {device}", osd_name=osd_name, debug=debug)


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


def wait_hdmi_connected(tvon: TvonConfig) -> str | None:
    candidates = hdmi_output_candidates(tvon.hdmi_output)
    log(f"TVON: hdmi-wait candidates={','.join(candidates)} timeout={tvon.hdmi_wait_sec}s")
    deadline = time.monotonic() + tvon.hdmi_wait_sec
    while time.monotonic() < deadline:
        found = detect_connected_hdmi(candidates)
        if found:
            log(f"TVON: hdmi-wait ok output={found}")
            return found
        statuses = {name: hdmi_link_status(name) for name in candidates}
        log(f"TVON: hdmi-wait status={statuses}")
        time.sleep(min(2, tvon.interval_sec))
    last = {name: hdmi_link_status(name) for name in candidates}
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


def refresh_batocera_audio(tvon: TvonConfig, hdmi_output: str | None) -> bool:
    """
    Если HDMI не было при старте PipeWire — остаётся Dummy (auto_null).
    Профиль/sink выбираем под живой порт (card0=A-1, card1=A-2).
    """
    prefer_card = _audio_card_for_output(hdmi_output)
    log(f"TVON: refresh audio prefer={hdmi_output or 'any'} card={prefer_card or 'any'}")
    _run_init_script("S06audio", "restart", timeout=90)
    time.sleep(2)
    _run_init_script("S27audioconfig", "restart", timeout=90)

    tool = shutil.which("batocera-audio")
    if not tool:
        log("TVON: batocera-audio not found")
        return False

    env = _display_env()
    profile = _pick_hdmi_profile(prefer_card)
    if profile:
        log(f"TVON: set-profile {profile}")
        subprocess.run(
            [tool, "set-profile", profile],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )

    sink = _pick_hdmi_sink(prefer_card)
    if sink:
        log(f"TVON: set sink {sink}")
        result = subprocess.run(
            [tool, "set", sink],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        if result.returncode == 0:
            log("TVON: audio HDMI ok")
            return True
        err = ((result.stdout or "") + (result.stderr or "")).strip()[:200]
        log(f"TVON: audio set failed rc={result.returncode} {err}")
    else:
        log("TVON: no HDMI sink in batocera-audio list yet")
    return False


def restart_emulationstation() -> None:
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
        f"refresh_audio={tvon.refresh_audio} restart_es={tvon.restart_es} "
        f"ssh_user={ssh_user}"
    )

    time.sleep(tvon.initial_delay_sec)

    status = wait_tv_power_on(tvon)
    if status != "on":
        log(f"TVON: timeout after {tvon.max_wait_sec}s, last power={status}")
        return 1

    live_hdmi = wait_hdmi_connected(tvon)
    if not live_hdmi:
        live_hdmi = detect_connected_hdmi(hdmi_output_candidates(tvon.hdmi_output))
        log(f"TVON: HDMI not confirmed — will try candidates (live={live_hdmi})")
    if tvon.settle_sec > 0:
        log(f"TVON: settle {tvon.settle_sec}s")
        time.sleep(tvon.settle_sec)

    physical_address = resolve_physical_address(tvon, live_hdmi)
    claimed = claim_with_retries(tvon, physical_address)

    active_hdmi = live_hdmi
    if tvon.refresh_display:
        active_hdmi = refresh_batocera_output(tvon, live_hdmi) or live_hdmi
    if tvon.refresh_audio:
        refresh_batocera_audio(tvon, active_hdmi)
    if tvon.restart_es:
        restart_emulationstation()

    if claimed:
        log(f"TVON: TV on, claimed, display={active_hdmi}, done")
    else:
        log(
            f"TVON: TV on, claim weak — display/audio refreshed "
            f"(display={active_hdmi})"
        )
    return 0


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
