#!/usr/bin/python3
import os
import sys
import subprocess
import venv as venv_module
import multiprocessing
import time
import logging
import signal

# ============================================================================
# ВИРТУАЛЬНАЯ СРЕДА - Инициализация
# ============================================================================

def setup_venv():
    """Проверяет и создает venv если необходимо"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(script_dir, "venv")
    
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
    requirements_file = os.path.join(script_dir, "requirements.txt")
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
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(script_dir, "venv")
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

import server
import timer

# Пути приложения
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs.log")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)

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


def _kill_pids(pids, reason):
    if not pids:
        return
    unique_pids = sorted({int(pid) for pid in pids if str(pid).isdigit()})
    if not unique_pids:
        return

    for pid in unique_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logging.warning("Failed to SIGTERM pid=%s: %s", pid, exc)

    time.sleep(1)

    for pid in unique_pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            logging.warning("Failed to SIGKILL pid=%s: %s", pid, exc)


def cleanup_stale_project_processes():
    current_pid = os.getpid()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_markers = []
    try:
        for name in os.listdir(script_dir):
            if name.lower().endswith(".py"):
                script_markers.append(os.path.join(script_dir, name))
    except Exception as exc:
        logging.warning("Failed to list python scripts in %s: %s", script_dir, exc)
        return

    if not script_markers:
        return

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True,
            text=True,
            check=False,
        )
        stale_pids = []
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
            if any(marker in args_text for marker in script_markers):
                stale_pids.append(pid)

        _kill_pids(stale_pids, "project scripts")
    except Exception as exc:
        logging.warning("Failed to cleanup stale project processes: %s", exc)


def main():
    # Регистрируем обработчики сигналов
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logging.info("MAIN service STARTED")

    cleanup_stale_project_processes()

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