#!/usr/bin/python3
import os
import sys
import shutil
import subprocess
import venv as venv_module
import multiprocessing
import time
import logging
import signal
import tomllib
from dataclasses import dataclass

# ============================================================================
# ВИРТУАЛЬНАЯ СРЕДА И РАЗВЁРТЫВАНИЕ НА BATOCERA
# ============================================================================

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_BATOCERA_SYSTEM_DIR = "/userdata/system"
_DEPLOYED_SCRIPTS_DIR = os.path.join(_BATOCERA_SYSTEM_DIR, "scripts")
_DEPLOY_MARKER = os.path.join(_BATOCERA_SYSTEM_DIR, ".arcade-deployed")


_CONFIG_FILENAME = "config_main.toml"
_LUMA_PACKAGE = "luma.led_matrix==1.9.0"
_REQUIREMENTS_FILE_NAME = "requirements.txt"


def _find_project_root() -> str:
    return _SCRIPT_DIR


def _is_deployed_scripts() -> bool:
    return os.path.realpath(_SCRIPT_DIR) == os.path.realpath(_DEPLOYED_SCRIPTS_DIR)


def _runtime_scripts_dir() -> str:
    """На Batocera venv и wheels всегда в /userdata/system/scripts/."""
    if _is_batocera_system():
        return _DEPLOYED_SCRIPTS_DIR
    return _SCRIPT_DIR


def _resolve_wheels_dir() -> str:
    """wheels/ в корне репозитория; после deploy — в /userdata/system/scripts/wheels/."""
    candidates = []
    if _is_batocera_system():
        candidates.append(os.path.join(_DEPLOYED_SCRIPTS_DIR, "wheels"))
    candidates.append(os.path.join(_bundle_root(), "wheels"))
    for path in candidates:
        if os.path.isdir(path) and any(name.endswith(".whl") for name in os.listdir(path)):
            return path
    return candidates[0] if candidates else os.path.join(_bundle_root(), "wheels")


def _resolve_venv_dir() -> str:
    return os.path.join(_runtime_scripts_dir(), "venv")


def _bundle_root() -> str:
    """Корень репозитория: configs/, services/, scripts/, wheels/."""
    source = _deploy_source_root()
    if source:
        return source
    if os.path.basename(_SCRIPT_DIR) == "scripts":
        return os.path.dirname(_SCRIPT_DIR)
    return _SCRIPT_DIR


def _requirements_file() -> str:
    return os.path.join(_bundle_root(), _REQUIREMENTS_FILE_NAME)


def _refresh_paths() -> None:
    global _PROJECT_ROOT, _VENV_DIR, _WHEELS_DIR
    _PROJECT_ROOT = _find_project_root()
    _VENV_DIR = _resolve_venv_dir()
    _WHEELS_DIR = _resolve_wheels_dir()


def _is_batocera_system() -> bool:
    return os.path.isdir(_BATOCERA_SYSTEM_DIR) and (
        os.path.isfile(os.path.join(_BATOCERA_SYSTEM_DIR, "batocera.conf"))
        or os.path.isfile("/boot/batocera")
    )


def _deploy_source_root() -> str | None:
    """Корень проекта с configs/, services/, scripts/ — может быть где угодно."""
    deployed_scripts = os.path.realpath(os.path.join(_BATOCERA_SYSTEM_DIR, "scripts"))
    if os.path.realpath(_SCRIPT_DIR) == deployed_scripts:
        return None

    current = os.path.realpath(_SCRIPT_DIR)
    while True:
        has_bundle = all(
            os.path.isdir(os.path.join(current, name))
            for name in ("configs", "services", "scripts")
        )
        if has_bundle and os.path.isfile(os.path.join(current, "batocera.conf")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


_PROJECT_ROOT = _find_project_root()
_VENV_DIR = _resolve_venv_dir()
_WHEELS_DIR = _resolve_wheels_dir()


def _deploy_ignore(dirpath: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in {"venv", "__pycache__", ".lgd-nfy0", "wheels"}:
            ignored.add(name)
            continue
        full = os.path.join(dirpath, name)
        if not os.path.isfile(full) and not os.path.isdir(full) and not os.path.islink(full):
            ignored.add(name)
    return ignored


def _fix_shell_line_endings(path: str) -> None:
    """CRLF в shebang ломает запуск: cannot execute: required file not found."""
    with open(path, "rb") as script_file:
        data = script_file.read()
    if b"\r" not in data:
        return
    with open(path, "wb") as script_file:
        script_file.write(data.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def deploy_to_batocera(force: bool = False) -> bool:
    """Первое развёртывание в /userdata/system/ с перезаписью; далее — пропуск (см. .arcade-deployed)."""
    if not _is_batocera_system():
        return False

    source_root = _deploy_source_root()
    if source_root is None:
        return False

    if os.path.isfile(_DEPLOY_MARKER) and not force:
        return False

    action = "re-deploy" if force else "first-time deploy"
    print("=" * 60)
    print(f"Batocera: {action} to /userdata/system/ (overwrite existing)...")
    print(f"  source: {source_root}")
    print("=" * 60)

    for folder in ("configs", "services", "scripts"):
        src = os.path.join(source_root, folder)
        dst = os.path.join(_BATOCERA_SYSTEM_DIR, folder)
        print(f"  {folder}/ -> {dst}")
        shutil.copytree(
            src,
            dst,
            dirs_exist_ok=True,
            ignore=_deploy_ignore,
            copy_function=shutil.copy2,
        )

    scripts_dest = os.path.join(_BATOCERA_SYSTEM_DIR, "scripts")

    wheels_src = os.path.join(source_root, "wheels")
    wheels_dst = os.path.join(scripts_dest, "wheels")
    if os.path.isdir(wheels_src):
        shutil.copytree(
            wheels_src,
            wheels_dst,
            dirs_exist_ok=True,
            copy_function=shutil.copy2,
        )
        print(f"  wheels/ -> {wheels_dst}")

    project_conf = os.path.join(source_root, "batocera.conf")
    dest_conf = os.path.join(_BATOCERA_SYSTEM_DIR, "batocera.conf")
    if os.path.isfile(project_conf):
        shutil.copy2(project_conf, dest_conf)
        print(f"  batocera.conf -> {dest_conf}")

    service_main = os.path.join(_BATOCERA_SYSTEM_DIR, "services", "main")
    if os.path.isfile(service_main):
        _fix_shell_line_endings(service_main)
        os.chmod(service_main, 0o755)

    with open(_DEPLOY_MARKER, "w", encoding="utf-8") as marker:
        marker.write(f"{source_root}\n")

    print("✓ Batocera deployment complete")
    print(f"  marker: {_DEPLOY_MARKER}")
    print("  Service: /userdata/system/services/main {start|stop|status}")
    print("=" * 60)
    return True


def _venv_pip() -> str:
    return os.path.join(_VENV_DIR, "bin", "pip")


def _venv_python() -> str:
    return os.path.join(_VENV_DIR, "bin", "python")


def _deps_installed(python_path: str | None = None) -> bool:
    python = python_path or _venv_python()
    if not os.path.isfile(python):
        return False
    result = subprocess.run(
        [python, "-c", "import luma.led_matrix"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _entry_script() -> str:
    if _is_batocera_system() and not _is_deployed_scripts():
        return os.path.join(_DEPLOYED_SCRIPTS_DIR, "main.py")
    return os.path.join(_SCRIPT_DIR, "main.py")


def _reexec_into_runtime() -> None:
    """Дочерние процессы multiprocessing должны стартовать через venv/bin/python."""
    venv_python = _venv_python()
    if not os.path.isfile(venv_python):
        return
    if os.path.realpath(sys.executable) == os.path.realpath(venv_python):
        return
    main_script = _entry_script()
    os.execv(venv_python, [venv_python, main_script, *sys.argv[1:]])


def _install_dependencies() -> bool:
    pip_path = _venv_pip()
    if not os.path.isfile(pip_path):
        print("✗ ERROR: pip not found in venv")
        return False

    wheels = (
        [name for name in os.listdir(_WHEELS_DIR) if name.endswith(".whl")]
        if os.path.isdir(_WHEELS_DIR)
        else []
    )
    if wheels:
        print(f"Installing {_LUMA_PACKAGE} from {len(wheels)} local wheels (offline)...")
        cmd = [
            pip_path,
            "install",
            "--no-index",
            f"--find-links={_WHEELS_DIR}",
            _LUMA_PACKAGE,
        ]
    else:
        print(f"⚠ wheels/ not found — trying pip over the network ({_LUMA_PACKAGE})...")
        cmd = [pip_path, "install", _LUMA_PACKAGE]

    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("✓ Dependencies installed")
        return True

    print("✗ ERROR: Failed to install dependencies")
    if not wheels:
        print("  Put aarch64 wheels into wheels/ or run: python scripts/main.py vendor-wheels")
    return False


def vendor_wheels() -> int:
    """Скачать wheel-файлы в wheels/ (только для разработки, не разворачивается на Batocera)."""
    wheels_dir = os.path.join(_bundle_root(), "wheels")
    requirements = _requirements_file()
    os.makedirs(wheels_dir, exist_ok=True)
    for name in os.listdir(wheels_dir):
        if name.endswith(".whl"):
            os.remove(os.path.join(wheels_dir, name))

    if not os.path.isfile(requirements):
        print(f"✗ ERROR: {requirements} not found")
        return 1

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    python = _venv_python() if os.path.isfile(_venv_python()) else sys.executable
    cmd = [
        python,
        "-m",
        "pip",
        "download",
        "-r",
        requirements,
        "-d",
        wheels_dir,
        "--python-version",
        python_version,
        "--platform",
        "manylinux2014_aarch64",
        "--only-binary=:all:",
    ]
    print("Downloading wheels for offline install...")
    print(" ".join(cmd))
    return subprocess.call(cmd)


def setup_venv():
    """Создаёт venv и ставит luma (офлайн из wheels/, если есть)."""
    venv_python = _venv_python()
    if os.path.isdir(_VENV_DIR) and _deps_installed(venv_python):
        return _VENV_DIR

    if not os.path.isdir(_VENV_DIR):
        print("=" * 60)
        print("Virtual environment not found. Creating...")
        print("=" * 60)
        try:
            venv_module.create(_VENV_DIR, with_pip=True, system_site_packages=True)
            print(f"✓ Virtual environment created: {_VENV_DIR}")
        except Exception as error:
            print(f"✗ ERROR: Failed to create venv: {error}")
            sys.exit(1)

    if not _deps_installed(venv_python):
        if not _install_dependencies():
            sys.exit(1)

    print("=" * 60)
    return _VENV_DIR


# Инициализируем venv на старте
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "vendor-wheels":
        raise SystemExit(vendor_wheels())
    if len(sys.argv) > 1 and sys.argv[1] == "deploy":
        ok = deploy_to_batocera(force=True)
        if ok:
            _refresh_paths()
            setup_venv()
        raise SystemExit(0 if ok else 1)
    if deploy_to_batocera():
        _refresh_paths()
    _refresh_paths()
    print("Initializing virtual environment...")
    setup_venv()
    _reexec_into_runtime()
    print("Ready to import dependencies\n")

# ============================================================================
# ОСНОВНОЙ КОД
# ============================================================================

SCRIPT_DIR = _SCRIPT_DIR
PROJECT_ROOT = _find_project_root()
LOG_FILE = os.path.join(PROJECT_ROOT, "logs.log")
CONFIG_PATH = os.path.join(SCRIPT_DIR, _CONFIG_FILENAME)


@dataclass(frozen=True)
class ServerConfig:
    port: int = 5000


@dataclass(frozen=True)
class TimerConfig:
    time_step: int = 5
    time_wait: int = 60
    time_reset: int = 5


@dataclass(frozen=True)
class GpioConfig:
    rf_increase: int = 5
    rf_playpause: int = 6
    rf_stop: int = 13
    r_buttons: int = 17
    r_playpause: int = 27
    r_stop: int = 22
    relay_active_low: bool = True


@dataclass(frozen=True)
class MatrixConfig:
    enabled: bool = True
    brightness: int = 7
    scroll_speed: int = 7
    text_display: str = "АРЕНДА: т. +79233549295"
    din: int = 10
    clk: int = 11
    cs: int = 8
    cascaded: int = 4
    block_orientation: int = 90
    rotate: int = 2
    blocks_reverse: bool = True
    test_on_start: bool = True


def _read_positive_int(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value <= 0:
        raise ValueError(f"{key} must be greater than 0")
    return value


def _read_string(section: dict, key: str, default: str) -> str:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _read_bool(section: dict, key: str, default: bool) -> bool:
    if key not in section:
        return default
    value = section[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _read_brightness(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0 or value > 15:
        raise ValueError(f"{key} must be between 0 and 15")
    return value


def _read_bcm_pin(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < 0 or value > 53:
        raise ValueError(f"{key} must be a BCM pin number between 0 and 53")
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


def _read_block_orientation(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value not in (0, 90, -90, 180):
        raise ValueError(f"{key} must be one of 0, 90, -90, 180")
    return value


def _read_rotate(section: dict, key: str, default: int) -> int:
    if key not in section:
        return default
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value not in (0, 1, 2, 3):
        raise ValueError(f"{key} must be 0, 1, 2, or 3 (x90°)")
    return value


def load_server_config() -> ServerConfig:
    defaults = ServerConfig()
    if not os.path.isfile(CONFIG_PATH):
        return defaults

    with open(CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)

    server_section = data.get("server", {})
    if not isinstance(server_section, dict):
        raise ValueError("[server] section must be a table")

    return ServerConfig(
        port=_read_positive_int(server_section, "port", defaults.port),
    )


def load_gpio_config() -> GpioConfig:
    defaults = GpioConfig()
    if not os.path.isfile(CONFIG_PATH):
        return defaults

    with open(CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)

    gpio_section = data.get("gpio", {})
    if not isinstance(gpio_section, dict):
        raise ValueError("[gpio] section must be a table")

    return GpioConfig(
        rf_increase=_read_bcm_pin(gpio_section, "rf_increase", defaults.rf_increase),
        rf_playpause=_read_bcm_pin(gpio_section, "rf_playpause", defaults.rf_playpause),
        rf_stop=_read_bcm_pin(gpio_section, "rf_stop", defaults.rf_stop),
        r_buttons=_read_bcm_pin(gpio_section, "r_buttons", defaults.r_buttons),
        r_playpause=_read_bcm_pin(gpio_section, "r_playpause", defaults.r_playpause),
        r_stop=_read_bcm_pin(gpio_section, "r_stop", defaults.r_stop),
        relay_active_low=_read_bool(gpio_section, "relay_active_low", defaults.relay_active_low),
    )


def load_timer_config() -> TimerConfig:
    defaults = TimerConfig()
    if not os.path.isfile(CONFIG_PATH):
        return defaults

    with open(CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)

    timer_section = data.get("timer", {})
    if not isinstance(timer_section, dict):
        raise ValueError("[timer] section must be a table")

    return TimerConfig(
        time_step=_read_positive_int(timer_section, "time_step", defaults.time_step),
        time_wait=_read_positive_int(timer_section, "time_wait", defaults.time_wait),
        time_reset=_read_positive_int(timer_section, "time_reset", defaults.time_reset),
    )


def load_matrix_config() -> MatrixConfig:
    defaults = MatrixConfig()
    if not os.path.isfile(CONFIG_PATH):
        return defaults

    with open(CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)

    matrix_section = data.get("matrix", {})
    if not isinstance(matrix_section, dict):
        raise ValueError("[matrix] section must be a table")

    return MatrixConfig(
        enabled=_read_bool(matrix_section, "enabled", defaults.enabled),
        brightness=_read_brightness(matrix_section, "brightness", defaults.brightness),
        scroll_speed=_read_positive_int(matrix_section, "scroll_speed", defaults.scroll_speed),
        text_display=_read_string(matrix_section, "text_display", defaults.text_display),
        din=_read_bcm_pin(matrix_section, "din", defaults.din),
        clk=_read_bcm_pin(matrix_section, "clk", defaults.clk),
        cs=_read_bcm_pin(matrix_section, "cs", defaults.cs),
        cascaded=_read_positive_int(matrix_section, "cascaded", defaults.cascaded),
        block_orientation=_read_block_orientation(
            matrix_section, "block_orientation", defaults.block_orientation
        ),
        rotate=_read_rotate(matrix_section, "rotate", defaults.rotate),
        blocks_reverse=_read_bool(matrix_section, "blocks_reverse", defaults.blocks_reverse),
        test_on_start=_read_bool(matrix_section, "test_on_start", defaults.test_on_start),
    )


try:
    server_config = load_server_config()
    timer_config = load_timer_config()
    gpio_config = load_gpio_config()
    matrix_config = load_matrix_config()
except (tomllib.TOMLDecodeError, ValueError, OSError) as error:
    print(f"ERROR: Failed to load config from {CONFIG_PATH}: {error}", file=sys.stderr)
    sys.exit(1)


def _is_debugpy_process(args_text):
    return "debugpy" in args_text


def _is_stale_project_process(args_text):
    server_py = os.path.join(SCRIPT_DIR, "server.py")
    timer_py = os.path.join(SCRIPT_DIR, "timer.py")
    main_py = os.path.join(SCRIPT_DIR, "main.py")
    legacy_main_py = os.path.join(PROJECT_ROOT, "main.py")
    legacy_server_py = os.path.join(PROJECT_ROOT, "server.py")
    legacy_timer_py = os.path.join(PROJECT_ROOT, "timer.py")
    if server_py in args_text or timer_py in args_text:
        return True
    if main_py in args_text and not _is_debugpy_process(args_text):
        return True
    if legacy_server_py in args_text or legacy_timer_py in args_text:
        return True
    if legacy_main_py in args_text and not _is_debugpy_process(args_text):
        return True
    return False


def _process_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _gpio_holder_pids(chip="/dev/gpiochip0"):
    if not os.path.exists(chip):
        return []
    result = subprocess.run(
        ["fuser", chip],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = []
    for token in (result.stdout + result.stderr).replace(f"{chip}:", "").split():
        if token.isdigit():
            pids.append(int(token))
    return pids


def _kill_stale_pids(pids):
    unique_pids = sorted({int(pid) for pid in pids if str(pid).isdigit()})
    if not unique_pids:
        return

    for pid in unique_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass

    time.sleep(0.5)

    for pid in unique_pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass


def cleanup_stale_project_processes(current_pid=None, log=None):
    if current_pid is None:
        current_pid = os.getpid()
    if log is None:
        log = lambda *args, **kwargs: None

    try:
        for attempt in range(3):
            stale_pids = []

            for pid in _gpio_holder_pids():
                if pid == current_pid:
                    continue
                cmdline = _process_cmdline(pid)
                if not cmdline:
                    continue
                if SCRIPT_DIR in cmdline or PROJECT_ROOT in cmdline or _is_stale_project_process(cmdline):
                    stale_pids.append(pid)

            result = subprocess.run(
                ["ps", "-eo", "pid,args"],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in result.stdout.splitlines()[1:]:
                parts = line.strip().split(None, 1)
                if len(parts) < 2:
                    continue
                pid_text, args_text = parts
                if not pid_text.isdigit():
                    continue
                pid = int(pid_text)
                if pid == current_pid:
                    continue
                if _is_debugpy_process(args_text):
                    continue
                if _is_stale_project_process(args_text):
                    stale_pids.append(pid)

            stale_pids = sorted({pid for pid in stale_pids if pid != current_pid})
            if not stale_pids:
                break

            log("Cleaning stale project processes (attempt %d): %s", attempt + 1, stale_pids)
            _kill_stale_pids(stale_pids)
            time.sleep(0.3)
    except Exception as exc:
        log("Failed to cleanup stale project processes: %s", exc)


def cleanup_processes(server_process, timer_process, timeout=5):
    """Gracefully terminate all child processes"""
    processes = [
        ("Server", server_process),
        ("Timer", timer_process)
    ]
    
    # Сначала отправляем SIGTERM
    for name, proc in processes:
        if proc.is_alive():
            logging.info("Sending SIGTERM to %s process (PID: %d)", name, proc.pid)
            proc.terminate()
    
    # Ждём завершения с таймаутом
    start_time = time.time()
    while time.time() - start_time < timeout:
        alive_procs = [p for _, p in processes if p.is_alive()]
        if not alive_procs:
            logging.info("All child processes terminated gracefully")
            return
        time.sleep(0.5)
    
    # Если ещё живы - убиваем SIGKILL
    for name, proc in processes:
        if proc.is_alive():
            logging.warning("Force killing %s process (PID: %d)", name, proc.pid)
            proc.kill()
    
    # Финальный join
    for name, proc in processes:
        proc.join(timeout=2)
        if proc.is_alive():
            logging.error("%s process still alive after kill!", name)

def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logging.warning("Received signal %d, initiating graceful shutdown...", signum)
    sys.exit(0)


def main():
    import server
    import timer

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stderr),
        ]
    )

    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logging.info("MAIN service STARTED")
    logging.info("Server config: port=%d", server_config.port)
    logging.info(
        "Timer config: time_step=%d min, time_wait=%d sec, time_reset=%d min",
        timer_config.time_step,
        timer_config.time_wait,
        timer_config.time_reset,
    )
    logging.info(
        "GPIO config: inputs +/play/stop=%d/%d/%d relays buttons/play/stop=%d/%d/%d active_low=%s",
        gpio_config.rf_increase,
        gpio_config.rf_playpause,
        gpio_config.rf_stop,
        gpio_config.r_buttons,
        gpio_config.r_playpause,
        gpio_config.r_stop,
        gpio_config.relay_active_low,
    )
    logging.info(
        "Matrix config: enabled=%s brightness=%d din/clk/cs=%d/%d/%d cascaded=%d",
        matrix_config.enabled,
        matrix_config.brightness,
        matrix_config.din,
        matrix_config.clk,
        matrix_config.cs,
        matrix_config.cascaded,
    )

    cleanup_stale_project_processes(log=logging.info)

    try:
        multiprocessing.set_start_method("spawn")
    except RuntimeError:
        pass

    queue_main = multiprocessing.Queue()
    server_process = None
    timer_process = None
    
    try:
        # Запускаем server.py в отдельном процессе
        server_process = multiprocessing.Process(
            target=server.server_start_async,
            args=(queue_main, "0.0.0.0", server_config.port),
            daemon=False
        )
        server_process.start()
        logging.info("'server.py' started (PID: %d)", server_process.pid)
        
        # Запускаем timer.py в отдельном процессе
        timer_process = multiprocessing.Process(
            target=timer.loop, 
            args=(queue_main,),
            daemon=False  # Явно отмечаем как non-daemon
        )
        timer_process.start()
        logging.info("'timer.py' started (PID: %d)", timer_process.pid)
        
        # Основной цикл main.py
        while True:
            # Проверяем живы ли процессы
            if not server_process.is_alive():
                logging.error("'server.py' died unexpectedly!")
                break
            if not timer_process.is_alive():
                logging.error("'timer.py' died unexpectedly!")
                break
            time.sleep(10)
            
    except KeyboardInterrupt:
        logging.warning("MAIN service received SIGINT")
    except Exception as e:
        logging.error("Error in MAIN service: %s", str(e), exc_info=True)
    finally:
        logging.info("Shutting down child processes...")
        if server_process and timer_process:
            cleanup_processes(server_process, timer_process)
        logging.info("MAIN service IS STOPPED")

if __name__ == "__main__":
    main()