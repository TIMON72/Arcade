import os
import signal
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_debugpy_process(args_text):
    return "debugpy" in args_text


def _is_stale_project_process(args_text):
    server_py = os.path.join(SCRIPT_DIR, "server.py")
    timer_py = os.path.join(SCRIPT_DIR, "timer.py")
    main_py = os.path.join(SCRIPT_DIR, "main.py")
    if server_py in args_text or timer_py in args_text:
        return True
    if main_py in args_text and not _is_debugpy_process(args_text):
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


def _kill_pids(pids):
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
                if SCRIPT_DIR in cmdline or _is_stale_project_process(cmdline):
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
            _kill_pids(stale_pids)
            time.sleep(0.3)
    except Exception as exc:
        log("Failed to cleanup stale project processes: %s", exc)
