#!/usr/bin/python3
import os
import sys
import subprocess
import venv as venv_module
import multiprocessing
import time
import logging
import signal
import tomllib
from dataclasses import dataclass

# ============================================================================
# ВИРТУАЛЬНАЯ СРЕДА - Инициализация
# ============================================================================

def setup_venv():
    """Проверяет и создает venv если необходимо"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_dir = os.path.join(project_root, "venv")
    
    # Если venv уже существует - всё ок
    if os.path.isdir(venv_dir):
        return venv_dir
    
    print("=" * 60)
    print("Virtual environment not found. Creating...")
    print("=" * 60)
    
    try:
        # Создаём venv
        venv_module.create(venv_dir, with_pip=True)
        print(f"✓ Virtual environment created: {venv_dir}")
    except Exception as e:
        print(f"✗ ERROR: Failed to create venv: {e}")
        sys.exit(1)
    
    # Получаем путь к pip в venv
    pip_path = os.path.join(venv_dir, "bin", "pip")
    
    # Обновляем pip
    print("Upgrading pip...")
    subprocess.run([pip_path, "install", "--upgrade", "pip", "setuptools", "wheel"], 
                   capture_output=True)
    
    # Устанавливаем зависимости
    requirements_file = os.path.join(project_root, "requirements.txt")
    if os.path.isfile(requirements_file):
        print("Installing dependencies...")
        result = subprocess.run([pip_path, "install", "-r", requirements_file])
        if result.returncode == 0:
            print("✓ Dependencies installed")
        else:
            print("✗ WARNING: Some dependencies failed to install")
    else:
        print("⚠ requirements.txt not found")
    
    print("=" * 60)
    return venv_dir

def activate_venv():
    """Активирует venv путём обновления PATH"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_dir = os.path.join(project_root, "venv")
    bin_dir = os.path.join(venv_dir, "bin")
    
    # Обновляем PATH чтобы использовать venv Python
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["VIRTUAL_ENV"] = venv_dir
        print(f"✓ Virtual environment activated: {venv_dir}")
    else:
        print("⚠ WARNING: venv bin directory not found")

# Инициализируем venv на старте
if __name__ == "__main__":
    # Проверяем что мы НЕ уже внутри venv
    if "VIRTUAL_ENV" not in os.environ:
        print("Initializing virtual environment...")
        venv_dir = setup_venv()
        activate_venv()
        print("Ready to import dependencies\n")

# ============================================================================
# ОСНОВНОЙ КОД
# ============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
LOG_FILE = os.path.join(PROJECT_ROOT, "logs.log")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.toml")


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
    spi_port: int = 10
    spi_device: int = 0
    din: int = 10
    clk: int = 11
    cs: int = 8
    cascaded: int = 4
    block_orientation: int = 90
    rotate: int = 2
    blocks_reverse: bool = True
    test_on_start: bool = True
    interface: str = "bitbang"


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
        spi_port=_read_non_negative_int(matrix_section, "spi_port", defaults.spi_port),
        spi_device=_read_non_negative_int(matrix_section, "spi_device", defaults.spi_device),
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
        interface=_read_string(matrix_section, "interface", defaults.interface),
    )


try:
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
            logging.FileHandler(LOG_FILE, encoding='utf-8')
        ]
    )

    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logging.info("MAIN service STARTED")
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
        "Matrix config: enabled=%s brightness=%d spi=%d:%d din/clk/cs=%d/%d/%d cascaded=%d",
        matrix_config.enabled,
        matrix_config.brightness,
        matrix_config.spi_port,
        matrix_config.spi_device,
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
            args=(queue_main,),
            daemon=False  # Явно отмечаем как non-daemon
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