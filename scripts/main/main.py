#!/usr/bin/python3
import os
import sys
import shutil
import subprocess
import importlib
import multiprocessing
import time
import signal
import socket
import tempfile
import platform
import gzip
import tomllib
import threading
from dataclasses import dataclass
from datetime import datetime
from urllib.request import urlretrieve

# stdlib venv: не `import venv` — Pyright путает со каталогом/.venv
venv_module = importlib.import_module("venv")

# ============================================================================
# ВИРТУАЛЬНАЯ СРЕДА И РАЗВЁРТЫВАНИЕ НА BATOCERA
# ============================================================================

# scripts/main/main.py → пакет main; общий корень scripts/ (конфиг, лог, venv)
_MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_MAIN_DIR)
_SCRIPT_DIR = _MAIN_DIR  # каталог пакета main (server/timer/modules)
_BATOCERA_SYSTEM_DIR = "/userdata/system"
_DEPLOYED_SCRIPTS_DIR = os.path.join(_BATOCERA_SYSTEM_DIR, "scripts")
_DEPLOY_MARKER = os.path.join(_BATOCERA_SYSTEM_DIR, ".arcade-deployed")
_ARCADE_CHECKOUT = os.path.join(_BATOCERA_SYSTEM_DIR, "Arcade")
_ARCADE_SERVICES = ("main", "timer", "server", "tvon")
_GIT_INSTALL_DIR = os.path.join(_BATOCERA_SYSTEM_DIR, "git")
_GIT_VENDOR_REL = os.path.join("vendor", "git")
_GIT_LINK_NAMES = (
    "git",
    "git-shell",
    "git-receive-pack",
    "git-upload-pack",
    "git-upload-archive",
)
# Портативный musl git (baulk/git-minimal), офлайн как wheels/
_GIT_RELEASE = "v2.55.0"
_GIT_TARBALL_BY_ARCH = {
    "aarch64": f"git-minimal-musl-{_GIT_RELEASE}-linux-aarch64.tar.xz",
    "arm64": f"git-minimal-musl-{_GIT_RELEASE}-linux-aarch64.tar.xz",
    "x86_64": f"git-minimal-musl-{_GIT_RELEASE}-linux-amd64.tar.xz",
    "amd64": f"git-minimal-musl-{_GIT_RELEASE}-linux-amd64.tar.xz",
}
_GIT_DOWNLOAD_BASE = (
    f"https://github.com/baulk/git-minimal/releases/download/{_GIT_RELEASE}"
)


_CONFIG_FILENAME = "config.toml"
_LUMA_PACKAGE = "luma.led_matrix==1.9.0"
_REQUIREMENTS_FILE_NAME = "requirements.txt"

# Общий лог: на Batocera — /userdata/system/scripts/logs.log;
# при debug из Arcade дублируем ещё в workspace scripts/logs.log.
_LOG_CANDIDATES = []
if os.path.isdir("/userdata/system/scripts"):
    _LOG_CANDIDATES.append("/userdata/system/scripts/logs.log")
_workspace_log = os.path.join(_SCRIPTS_ROOT, "logs.log")
if _workspace_log not in _LOG_CANDIDATES:
    _LOG_CANDIDATES.append(_workspace_log)
LOG_FILE = _LOG_CANDIDATES[0]
CONFIG_PATH = os.path.join(_MAIN_DIR, _CONFIG_FILENAME)
_log_lock = threading.Lock()


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} - INFO - {message}"
    with _log_lock:
        print(line, flush=True)
        for path in _LOG_CANDIDATES:
            try:
                with open(path, "a", encoding="utf-8") as log_file:
                    log_file.write(line + "\n")
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Unix datagram: общий канал команд между сервисами (server → timer, …)
# ---------------------------------------------------------------------------

class CmdListener:
    """Неблокирующий приём строк с Unix datagram socket; poll() в цикле сервиса."""

    def __init__(self, path: str):
        self.path = path
        self._sock = None

    def setup(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(self.path)
        sock.setblocking(False)
        try:
            os.chmod(self.path, 0o666)
        except OSError:
            pass
        self._sock = sock

    def poll(self):
        if self._sock is None:
            return None
        try:
            data, _addr = self._sock.recvfrom(4096)
        except BlockingIOError:
            return None
        except OSError:
            return None
        text = data.decode("utf-8", "replace").strip()
        return text or None

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def cmd_send(path: str, message: str) -> bool:
    """Отправить строку на Unix datagram socket (другой сервис должен слушать)."""
    if not message:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), path)
        return True
    except OSError as e:
        log(f"cmd_send failed ({message!r} → {path}): {e}")
        return False
    finally:
        sock.close()


@dataclass(frozen=True)
class SshConfig:
    user: str = "root"
    password: str = "linux"


def load_ssh_config() -> SshConfig:
    defaults = SshConfig()
    if not os.path.isfile(CONFIG_PATH):
        return defaults
    with open(CONFIG_PATH, "rb") as config_file:
        data = tomllib.load(config_file)
    section = data.get("ssh", {})
    if not isinstance(section, dict):
        raise ValueError("[ssh] section must be a table")
    user = section.get("user", defaults.user)
    password = section.get("password", defaults.password)
    if not isinstance(user, str) or not isinstance(password, str):
        raise ValueError("[ssh] user and password must be strings")
    return SshConfig(user=user, password=password)


def _find_project_root() -> str:
    return _SCRIPTS_ROOT


def _is_deployed_scripts() -> bool:
    return os.path.realpath(_SCRIPTS_ROOT) == os.path.realpath(_DEPLOYED_SCRIPTS_DIR)


def _runtime_scripts_dir() -> str:
    """На Batocera venv и wheels всегда в /userdata/system/scripts/."""
    if _is_batocera_system():
        return _DEPLOYED_SCRIPTS_DIR
    return _SCRIPTS_ROOT


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
    """Каталог .venv (не venv) — иначе Pyright путает его со stdlib-модулем venv."""
    scripts_dir = _runtime_scripts_dir()
    preferred = os.path.join(scripts_dir, ".venv")
    legacy = os.path.join(scripts_dir, "venv")
    if os.path.isdir(preferred):
        return preferred
    if os.path.isdir(legacy):
        return legacy
    return preferred


def _bundle_root() -> str:
    """Корень репозитория: configs/, services/, scripts/, wheels/."""
    source = _deploy_source_root()
    if source:
        return source
    if os.path.basename(_SCRIPTS_ROOT) == "scripts":
        return os.path.dirname(_SCRIPTS_ROOT)
    return _SCRIPTS_ROOT


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


def _looks_like_arcade_root(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    return all(
        os.path.isdir(os.path.join(path, name)) for name in ("configs", "services", "scripts")
    ) and os.path.isfile(os.path.join(path, "batocera.conf"))


def _arcade_checkout_root() -> str | None:
    """Фиксированный путь /userdata/system/Arcade — источник для авто-deploy при reboot."""
    if _looks_like_arcade_root(_ARCADE_CHECKOUT):
        return os.path.realpath(_ARCADE_CHECKOUT)
    return None


def _deploy_source_root() -> str | None:
    """Корень проекта с configs/, services/, scripts/ — Arcade checkout или обход вверх от main.py."""
    checkout = _arcade_checkout_root()
    if checkout is not None:
        return checkout

    deployed_scripts = os.path.realpath(os.path.join(_BATOCERA_SYSTEM_DIR, "scripts"))
    if os.path.realpath(_SCRIPTS_ROOT) == deployed_scripts:
        return None

    current = os.path.realpath(_MAIN_DIR)
    while True:
        if _looks_like_arcade_root(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _arcade_fingerprint(source_root: str) -> str:
    """Идентификатор версии checkout: git HEAD или mtime дерева scripts/services."""
    git_dir = os.path.join(source_root, ".git")
    if os.path.isdir(git_dir):
        result = subprocess.run(
            ["git", "-C", source_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            rev = result.stdout.strip()
            if rev:
                return f"git:{rev}"
    latest = 0.0
    for folder in ("scripts", "services"):
        root = os.path.join(source_root, folder)
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".pyc") or name == "logs.log":
                    continue
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    return f"mtime:{latest:.0f}"


def _read_deploy_marker() -> tuple[str | None, str | None]:
    if not os.path.isfile(_DEPLOY_MARKER):
        return None, None
    try:
        with open(_DEPLOY_MARKER, encoding="utf-8") as marker:
            lines = [line.strip() for line in marker.readlines() if line.strip()]
    except OSError:
        return None, None
    source = lines[0] if lines else None
    fingerprint = lines[1] if len(lines) > 1 else None
    return source, fingerprint


def _write_deploy_marker(source_root: str, fingerprint: str) -> None:
    with open(_DEPLOY_MARKER, "w", encoding="utf-8") as marker:
        marker.write(f"{source_root}\n{fingerprint}\n")


def _chmod_services(services_dir: str) -> None:
    if not os.path.isdir(services_dir):
        return
    for name in os.listdir(services_dir):
        path = os.path.join(services_dir, name)
        if not os.path.isfile(path):
            continue
        _fix_shell_line_endings(path)
        try:
            os.chmod(path, 0o755)
        except OSError:
            pass


def _strip_scripts_exec_bits(scripts_dir: str) -> None:
    if not os.path.isdir(scripts_dir):
        return
    for root, _dirs, files in os.walk(scripts_dir):
        parts = set(root.split(os.sep))
        if ".venv" in parts or "venv" in parts:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                mode = os.stat(path).st_mode
                if mode & 0o111:
                    os.chmod(path, mode & ~0o111)
            except OSError:
                pass


def _deploy_copy(src: str, dst: str, *, follow_symlinks: bool = True) -> None:
    shutil.copy2(src, dst, follow_symlinks=follow_symlinks)
    if src.endswith(".py"):
        try:
            mode = os.stat(dst).st_mode
            if mode & 0o111:
                os.chmod(dst, mode & ~0o111)
        except OSError:
            pass


def _ensure_batocera_services_enabled() -> None:
    """system.services=main timer server tvon + batocera-services enable."""
    conf_path = os.path.join(_BATOCERA_SYSTEM_DIR, "batocera.conf")
    wanted = " ".join(_ARCADE_SERVICES)
    if os.path.isfile(conf_path):
        try:
            with open(conf_path, encoding="utf-8") as conf_file:
                lines = conf_file.readlines()
            out: list[str] = []
            found = False
            for line in lines:
                if line.startswith("system.services="):
                    out.append(f"system.services={wanted}\n")
                    found = True
                else:
                    out.append(line)
            if not found:
                out.append(f"\nsystem.services={wanted}\n")
            with open(conf_path, "w", encoding="utf-8") as conf_file:
                conf_file.writelines(out)
        except OSError as error:
            print(f"⚠ cannot update batocera.conf services: {error}")

    for name in _ARCADE_SERVICES:
        subprocess.run(
            ["batocera-services", "enable", name],
            capture_output=True,
            text=True,
            check=False,
        )


def _start_sibling_services() -> None:
    for name in ("timer", "server", "tvon"):
        subprocess.run(
            ["batocera-services", "start", name],
            capture_output=True,
            text=True,
            check=False,
        )


def _stop_sibling_services() -> None:
    for name in ("timer", "server", "tvon"):
        subprocess.run(
            ["batocera-services", "stop", name],
            capture_output=True,
            text=True,
            check=False,
        )


def _running_under_debugpy() -> bool:
    if "debugpy" in sys.modules:
        return True
    return any("debugpy" in arg for arg in sys.argv)


def deploy_to_batocera(force: bool = False, source_root: str | None = None) -> bool:
    """Развёртывание Arcade → /userdata/system/. force=True или смена fingerprint."""
    if not _is_batocera_system():
        return False

    if source_root is None:
        source_root = _deploy_source_root()
    if source_root is None:
        return False
    source_root = os.path.realpath(source_root)
    if not _looks_like_arcade_root(source_root):
        return False

    fingerprint = _arcade_fingerprint(source_root)
    _marker_source, marker_fp = _read_deploy_marker()
    if os.path.isfile(_DEPLOY_MARKER) and not force and marker_fp == fingerprint:
        return False

    action = "re-deploy" if force or marker_fp else "first-time deploy"
    print("=" * 60)
    print(f"Batocera: {action} to /userdata/system/ (overwrite existing)...")
    print(f"  source: {source_root}")
    print(f"  fingerprint: {fingerprint}")
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
            copy_function=_deploy_copy,
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

    _chmod_services(os.path.join(_BATOCERA_SYSTEM_DIR, "services"))
    _strip_scripts_exec_bits(scripts_dest)
    _strip_scripts_exec_bits(os.path.join(source_root, "scripts"))
    _ensure_batocera_services_enabled()

    install_portable_git(source_root)
    install_cursor_extensions(source_root)

    _write_deploy_marker(source_root, fingerprint)

    print("✓ Batocera deployment complete")
    print(f"  marker: {_DEPLOY_MARKER}")
    print(f"  Services: {' '.join(_ARCADE_SERVICES)}")
    print("=" * 60)
    return True


def sync_arcade_checkout() -> bool:
    """
    Если есть /userdata/system/Arcade — задеплоить при смене версии (reboot / старт main).
    Возвращает True, если копирование выполнялось.
    """
    source = _arcade_checkout_root() or _deploy_source_root()
    if source is None:
        return False
    return deploy_to_batocera(force=False, source_root=source)


def _deploy_ignore(dirpath: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        if name in {"venv", ".venv", "__pycache__", ".lgd-nfy0", "wheels"}:
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


_PROJECT_ROOT = _find_project_root()
_VENV_DIR = _resolve_venv_dir()
_WHEELS_DIR = _resolve_wheels_dir()


def _host_arch() -> str:
    return platform.machine().lower()


def _git_tarball_name() -> str | None:
    return _GIT_TARBALL_BY_ARCH.get(_host_arch())


def _find_git_tarball(source_root: str) -> str | None:
    vendor_dir = os.path.join(source_root, _GIT_VENDOR_REL)
    if not os.path.isdir(vendor_dir):
        return None
    preferred = _git_tarball_name()
    if preferred:
        path = os.path.join(vendor_dir, preferred)
        if os.path.isfile(path):
            return path
    # запасной поиск по суффиксу архитектуры в имени файла
    arch = _host_arch()
    aliases = {
        "aarch64": ("aarch64", "arm64"),
        "arm64": ("aarch64", "arm64"),
        "x86_64": ("amd64", "x86_64"),
        "amd64": ("amd64", "x86_64"),
    }.get(arch, (arch,))
    for name in sorted(os.listdir(vendor_dir)):
        if not name.endswith(".tar.xz"):
            continue
        lower = name.lower()
        if any(token in lower for token in aliases):
            return os.path.join(vendor_dir, name)
    return None


def link_portable_git() -> bool:
    """Симлинки /usr/bin/git* и CA bundle (нужны после reboot без overlay).

    Важно: линкуем cmd/git (launcher), а не bin/git — иначе exec-path
    указывает на /usr/local/libexec/git-core и ломается remote-https.
    """
    git_cmd = os.path.join(_GIT_INSTALL_DIR, "cmd", "git")
    git_bin = os.path.join(_GIT_INSTALL_DIR, "bin", "git")
    if os.path.exists(git_cmd):
        launcher_dir = os.path.join(_GIT_INSTALL_DIR, "cmd")
    elif os.path.isfile(git_bin) and os.access(git_bin, os.X_OK):
        launcher_dir = os.path.join(_GIT_INSTALL_DIR, "bin")
    else:
        return False

    for name in _GIT_LINK_NAMES:
        src = os.path.join(launcher_dir, name)
        if not os.path.exists(src):
            src = os.path.join(_GIT_INSTALL_DIR, "bin", name)
        if not os.path.exists(src):
            continue
        dst = os.path.join("/usr/bin", name)
        try:
            if os.path.islink(dst) or os.path.isfile(dst):
                os.remove(dst)
            os.symlink(src, dst)
        except OSError as error:
            print(f"⚠ Cannot link {dst}: {error}")
            return False

    # CA для HTTPS: бинарник ждёт /usr/local/share/git-minimal/curl-ca-bundle.crt
    ca_src = os.path.join(_GIT_INSTALL_DIR, "share", "git-minimal")
    ca_dst_parent = "/usr/local/share"
    ca_dst = os.path.join(ca_dst_parent, "git-minimal")
    if os.path.isdir(ca_src):
        try:
            os.makedirs(ca_dst_parent, exist_ok=True)
            if os.path.islink(ca_dst) or os.path.isfile(ca_dst):
                os.remove(ca_dst)
            elif os.path.isdir(ca_dst) and not os.path.islink(ca_dst):
                pass
            else:
                os.symlink(ca_src, ca_dst)
        except OSError as error:
            print(f"⚠ Cannot link git CA bundle: {error}")

    return True


def install_portable_git(source_root: str | None = None) -> bool:
    """Распаковать vendor/git/*.tar.xz в /userdata/system/git и прописать /usr/bin."""
    root = source_root or _bundle_root()
    tarball = _find_git_tarball(root)
    if tarball is None:
        print("⚠ vendor/git: no matching tarball for this arch — git not installed")
        print(f"  expected arch={_host_arch()}, run: python3 scripts/main/main.py vendor-git")
        return False

    print(f"Installing portable git from {os.path.basename(tarball)}...")
    os.makedirs(_BATOCERA_SYSTEM_DIR, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="arcade-git-") as tmp:
        # На Batocera в Python часто нет модуля lzma — используем системный tar.
        extract = subprocess.run(
            ["tar", "-xJf", tarball, "-C", tmp],
            capture_output=True,
            text=True,
            check=False,
        )
        if extract.returncode != 0:
            detail = (extract.stderr or extract.stdout or "").strip()
            print(f"✗ ERROR: tar extract failed: {detail or extract.returncode}")
            return False
        extracted = [
            os.path.join(tmp, name)
            for name in os.listdir(tmp)
            if os.path.isdir(os.path.join(tmp, name))
        ]
        if len(extracted) != 1:
            print("✗ ERROR: unexpected git archive layout")
            return False
        if os.path.isdir(_GIT_INSTALL_DIR):
            shutil.rmtree(_GIT_INSTALL_DIR)
        shutil.move(extracted[0], _GIT_INSTALL_DIR)

    git_bin = os.path.join(_GIT_INSTALL_DIR, "bin", "git")
    os.chmod(git_bin, 0o755)
    if not link_portable_git():
        print("✗ ERROR: git extracted but /usr/bin links failed")
        return False

    version = subprocess.run(
        [git_bin, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (version.stdout or version.stderr or "").strip()
    print(f"✓ Portable git installed: {_GIT_INSTALL_DIR} ({detail or 'ok'})")
    return True


def vendor_git() -> int:
    """Скачать tarball git в vendor/git/ (нужен интернет; для коммита в репозиторий)."""
    name = _git_tarball_name()
    if name is None:
        print(f"✗ ERROR: unsupported arch for vendor-git: {_host_arch()}")
        return 1
    vendor_dir = os.path.join(_bundle_root(), _GIT_VENDOR_REL)
    os.makedirs(vendor_dir, exist_ok=True)
    dest = os.path.join(vendor_dir, name)
    url = f"{_GIT_DOWNLOAD_BASE}/{name}"
    print(f"Downloading {url}")
    print(f"  -> {dest}")
    try:
        urlretrieve(url, dest)
    except Exception as error:
        print(f"✗ ERROR: download failed: {error}")
        return 1
    print(f"✓ Saved {os.path.getsize(dest)} bytes")
    print("  Commit vendor/git/ so deploy works offline on each machine.")
    return 0


_CURSOR_EXTENSIONS = (
    "ms-python.python",
    "ms-python.debugpy",
)
_CURSOR_VSIX_VENDOR_REL = os.path.join("vendor", "vscode")
_CURSOR_VSIX_DOWNLOADS = {
    "ms-python.python": (
        "https://marketplace.visualstudio.com/_apis/public/gallery/"
        "publishers/ms-python/vsextensions/python/2025.4.0/vspackage"
    ),
    "ms-python.debugpy": (
        "https://marketplace.visualstudio.com/_apis/public/gallery/"
        "publishers/ms-python/vsextensions/debugpy/2026.6.0/vspackage"
    ),
}


def _find_cursor_cli() -> str | None:
    """CLI remote Cursor: .../cursor-server/bin/<arch>/<hash>/bin/remote-cli/cursor"""
    candidates = []
    for base in (
        "/userdata/system/.cursor-server/bin",
        os.path.expanduser("~/.cursor-server/bin"),
    ):
        if not os.path.isdir(base):
            continue
        for arch in sorted(os.listdir(base)):
            arch_dir = os.path.join(base, arch)
            if not os.path.isdir(arch_dir):
                continue
            for build in sorted(os.listdir(arch_dir), reverse=True):
                cli = os.path.join(arch_dir, build, "bin", "remote-cli", "cursor")
                if os.path.isfile(cli) and os.access(cli, os.X_OK):
                    candidates.append(cli)
    return candidates[0] if candidates else None


def _cursor_extensions_dir() -> str | None:
    for path in (
        "/userdata/system/.cursor-server/extensions",
        os.path.expanduser("~/.cursor-server/extensions"),
    ):
        if os.path.isdir(path):
            return path
    return None


def _cursor_extension_installed(ext_id: str) -> bool:
    ext_dir = _cursor_extensions_dir()
    if ext_dir is None:
        return False
    prefix = ext_id.lower() + "-"
    try:
        return any(name.lower().startswith(prefix) for name in os.listdir(ext_dir))
    except OSError:
        return False


def _find_extension_vsix(source_root: str, ext_id: str) -> str | None:
    vendor_dir = os.path.join(source_root, _CURSOR_VSIX_VENDOR_REL)
    if not os.path.isdir(vendor_dir):
        return None
    # Точное имя ms-python.python.vsix или с версией в имени
    exact = os.path.join(vendor_dir, f"{ext_id}.vsix")
    if os.path.isfile(exact):
        return exact
    prefix = ext_id.lower()
    for name in sorted(os.listdir(vendor_dir)):
        lower = name.lower()
        if lower.startswith(prefix) and lower.endswith(".vsix"):
            return os.path.join(vendor_dir, name)
    return None


def install_cursor_extensions(source_root: str | None = None) -> bool:
    """Ставит Python/debugpy в remote Cursor (офлайн из vendor/vscode или marketplace)."""
    root = source_root or _bundle_root()
    cli = _find_cursor_cli()
    if cli is None:
        print("⚠ Cursor CLI not found — skip IDE extensions (open project via SSH once)")
        return False

    print("Installing Cursor IDE extensions (Python / debugpy)...")
    all_ok = True
    for ext_id in _CURSOR_EXTENSIONS:
        if _cursor_extension_installed(ext_id):
            print(f"  ✓ {ext_id} already installed")
            continue
        vsix = _find_extension_vsix(root, ext_id)
        target = vsix if vsix else ext_id
        label = os.path.basename(vsix) if vsix else ext_id
        print(f"  Installing {label}...")
        result = subprocess.run(
            [cli, "--install-extension", target, "--force"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and not _cursor_extension_installed(ext_id):
            detail = (result.stderr or result.stdout or "").strip()
            print(f"  ✗ {ext_id}: {detail or result.returncode}")
            all_ok = False
        else:
            print(f"  ✓ {ext_id}")
    return all_ok


def vendor_extensions() -> int:
    """Скачать VSIX в vendor/vscode/ (нужен интернет; для офлайн-deploy)."""
    vendor_dir = os.path.join(_bundle_root(), _CURSOR_VSIX_VENDOR_REL)
    os.makedirs(vendor_dir, exist_ok=True)

    for ext_id, url in _CURSOR_VSIX_DOWNLOADS.items():
        dest = os.path.join(vendor_dir, f"{ext_id}.vsix")
        print(f"Downloading {ext_id}...")
        print(f"  {url}")
        try:
            tmp_path, headers = urlretrieve(url)
        except Exception as error:
            print(f"✗ ERROR: {error}")
            return 1
        try:
            with open(tmp_path, "rb") as src:
                data = src.read()
            encoding = ""
            if hasattr(headers, "get"):
                encoding = (headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            with open(dest, "wb") as out:
                out.write(data)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        print(f"  ✓ {dest} ({os.path.getsize(dest)} bytes)")
    print("Commit vendor/vscode/ for offline installs on each machine.")
    return 0


def _venv_pip() -> str:
    return os.path.join(_VENV_DIR, "bin", "pip")


def _venv_python() -> str:
    return os.path.join(_VENV_DIR, "bin", "python")


def _venv_is_usable() -> bool:
    return os.path.isfile(_venv_python())


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
        return os.path.join(_DEPLOYED_SCRIPTS_DIR, "main", "main.py")
    return os.path.join(_MAIN_DIR, "main.py")


def _reexec_into_runtime() -> None:
    """Дочерние процессы multiprocessing должны стартовать через venv/bin/python."""
    venv_python = _venv_python()
    if not os.path.isfile(venv_python):
        return
    if os.path.realpath(sys.executable) == os.path.realpath(venv_python):
        return
    main_script = _entry_script()
    os.execv(venv_python, [venv_python, main_script, *sys.argv[1:]])


def _pip_cmd() -> list[str] | None:
    """Команда pip: bin/pip или python -m pip."""
    pip_path = _venv_pip()
    if os.path.isfile(pip_path):
        return [pip_path]
    python = _venv_python()
    if os.path.isfile(python):
        return [python, "-m", "pip"]
    return None


def _install_dependencies() -> bool:
    pip_cmd = _pip_cmd()
    if not pip_cmd:
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
            *pip_cmd,
            "install",
            "--no-index",
            f"--find-links={_WHEELS_DIR}",
            _LUMA_PACKAGE,
        ]
    else:
        print(f"⚠ wheels/ not found — trying pip over the network ({_LUMA_PACKAGE})...")
        cmd = [*pip_cmd, "install", _LUMA_PACKAGE]

    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("✓ Dependencies installed")
        return True

    print("✗ ERROR: Failed to install dependencies")
    if not wheels:
        print("  Put aarch64 wheels into wheels/ or run: python scripts/main/main.py vendor-wheels")
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
    """Создаёт .venv и ставит luma (офлайн из wheels/, если есть)."""
    global _VENV_DIR
    scripts_dir = _runtime_scripts_dir()
    preferred = os.path.join(scripts_dir, ".venv")
    legacy = os.path.join(scripts_dir, "venv")
    # Старый каталог venv ломает анализ import venv в IDE — переименовываем.
    if os.path.isdir(legacy) and not os.path.isdir(preferred):
        print(f"Migrating virtualenv: {legacy} -> {preferred}")
        os.rename(legacy, preferred)
    _VENV_DIR = _resolve_venv_dir()

    # Обломок .venv (например только lib64) блокировал create — пересоздаём
    if os.path.isdir(_VENV_DIR) and not _venv_is_usable():
        print(f"⚠ Broken virtualenv at {_VENV_DIR} — recreating...")
        try:
            shutil.rmtree(_VENV_DIR)
        except OSError as error:
            print(f"✗ ERROR: Cannot remove broken venv: {error}")
            sys.exit(1)

    venv_python = _venv_python()
    if os.path.isdir(_VENV_DIR) and _deps_installed(venv_python):
        _link_workspace_venv()
        return _VENV_DIR

    if not os.path.isdir(_VENV_DIR):
        print("=" * 60)
        print("Virtual environment not found. Creating...")
        print("=" * 60)
        try:
            venv_module.create(_VENV_DIR, with_pip=True, system_site_packages=True)
            print(f"✓ Virtual environment created: {_VENV_DIR}")
        except Exception as error:
            print(f"⚠ venv with_pip failed ({error}), retry without pip...")
            try:
                if os.path.isdir(_VENV_DIR):
                    shutil.rmtree(_VENV_DIR)
                venv_module.create(_VENV_DIR, with_pip=False, system_site_packages=True)
                subprocess.run(
                    [_venv_python(), "-m", "ensurepip", "--upgrade"],
                    check=False,
                )
                print(f"✓ Virtual environment created: {_VENV_DIR}")
            except Exception as error2:
                print(f"✗ ERROR: Failed to create venv: {error2}")
                sys.exit(1)

    if not _venv_is_usable():
        print(f"✗ ERROR: venv python missing at {_venv_python()}")
        sys.exit(1)

    if not _deps_installed(_venv_python()):
        if not _install_dependencies():
            sys.exit(1)

    _link_workspace_venv()
    print("=" * 60)
    return _VENV_DIR


def _link_workspace_venv() -> None:
    """Симлинк <repo>/.venv → /userdata/system/scripts/.venv для Cursor/VS Code."""
    if not _is_batocera_system():
        return
    source_root = _deploy_source_root() or (
        os.path.dirname(_SCRIPTS_ROOT) if os.path.basename(_SCRIPTS_ROOT) == "scripts" else None
    )
    if not source_root or not os.path.isdir(_VENV_DIR):
        return
    # Не линкуем, если правим уже развёрнутый /userdata/system/scripts
    if os.path.realpath(source_root) == os.path.realpath(_BATOCERA_SYSTEM_DIR):
        return
    if os.path.realpath(source_root) == os.path.realpath(_DEPLOYED_SCRIPTS_DIR):
        return
    link_path = os.path.join(source_root, ".venv")
    try:
        if os.path.islink(link_path) or os.path.isfile(link_path):
            os.remove(link_path)
        elif os.path.isdir(link_path) and not os.path.islink(link_path):
            # Реальный каталог .venv в репо не трогаем
            return
        if not os.path.exists(link_path):
            os.symlink(_VENV_DIR, link_path)
            print(f"✓ IDE venv link: {link_path} -> {_VENV_DIR}")
    except OSError as error:
        print(f"⚠ Cannot link workspace .venv: {error}")


# Инициализируем venv на старте
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("gameStart", "gameStop"):
        raise SystemExit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "vendor-wheels":
        raise SystemExit(vendor_wheels())
    if len(sys.argv) > 1 and sys.argv[1] == "vendor-git":
        raise SystemExit(vendor_git())
    if len(sys.argv) > 1 and sys.argv[1] == "vendor-extensions":
        raise SystemExit(vendor_extensions())
    if len(sys.argv) > 1 and sys.argv[1] == "install-git":
        raise SystemExit(0 if install_portable_git() else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "install-extensions":
        raise SystemExit(0 if install_cursor_extensions() else 1)
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

SCRIPT_DIR = _MAIN_DIR
SCRIPTS_ROOT = _SCRIPTS_ROOT
PROJECT_ROOT = _find_project_root()

try:
    ssh_config = load_ssh_config()
except (tomllib.TOMLDecodeError, ValueError, OSError) as error:
    print(f"ERROR: Failed to load SSH config from {CONFIG_PATH}: {error}", file=sys.stderr)
    sys.exit(1)


def _is_debugpy_process(args_text):
    return "debugpy" in args_text


def _is_stale_project_process(args_text):
    """Старые пути multiprocessing main→server/timer + текущие сервисы."""
    paths = [
        os.path.join(SCRIPT_DIR, "main.py"),
        os.path.join(SCRIPTS_ROOT, "main", "main.py"),
        os.path.join(SCRIPTS_ROOT, "main.py"),  # legacy
        os.path.join(SCRIPTS_ROOT, "server.py"),
        os.path.join(SCRIPTS_ROOT, "timer.py"),
        os.path.join(SCRIPTS_ROOT, "server", "server.py"),
        os.path.join(SCRIPTS_ROOT, "timer", "timer.py"),
    ]
    for path in paths:
        if path in args_text:
            if path.endswith("main.py") and _is_debugpy_process(args_text):
                continue
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


def cleanup_stale_project_processes(current_pid=None, log_fn=None):
    """Чистит только legacy-оркестратор scripts/main.py (не timer/server/tvon)."""
    if current_pid is None:
        current_pid = os.getpid()
    if log_fn is None:
        log_fn = lambda *a, **k: None
    legacy_main = os.path.join(SCRIPTS_ROOT, "main.py")
    try:
        for attempt in range(3):
            stale_pids = []
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
                if pid == current_pid or _is_debugpy_process(args_text):
                    continue
                # Только старый файл scripts/main.py (не scripts/main/main.py)
                if legacy_main in args_text and f"{os.sep}main{os.sep}main.py" not in args_text:
                    stale_pids.append(pid)

            stale_pids = sorted({pid for pid in stale_pids if pid != current_pid})
            if not stale_pids:
                break
            log_fn("Cleaning stale project processes (attempt %d): %s", attempt + 1, stale_pids)
            _kill_stale_pids(stale_pids)
            time.sleep(0.3)
    except Exception as exc:
        log_fn("Failed to cleanup stale project processes: %s", exc)


def signal_handler(signum, frame):
    log(f"Received signal {signum}, initiating graceful shutdown...")
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    log("MAIN service STARTED (deploy/venv/ssh hub)")
    try:
        if sync_arcade_checkout():
            log("MAIN: Arcade checkout synced to /userdata/system/")
            _refresh_paths()
            setup_venv()
        else:
            source = _arcade_checkout_root()
            if source:
                log(f"MAIN: Arcade checkout up to date ({source})")
            _ensure_batocera_services_enabled()
        _strip_scripts_exec_bits(os.path.join(_BATOCERA_SYSTEM_DIR, "scripts"))
        arcade = _arcade_checkout_root()
        if arcade:
            _strip_scripts_exec_bits(os.path.join(arcade, "scripts"))
        if _running_under_debugpy():
            # Debug Arcade/Main: не поднимать batocera timer/server — иначе два процесса,
            # GPIO busy и сокет у «пустого» timer (RF: skip click).
            _stop_sibling_services()
            log("MAIN: debugpy — batocera timer/server/tvon stopped (use launch Arcade)")
        else:
            _start_sibling_services()
            log("MAIN: sibling services start requested (timer server tvon)")
    except Exception as e:
        log(f"MAIN: arcade sync/start warning: {e}")

    log(f"SSH config: user={ssh_config.user} (password hidden)")
    log(
        "Sibling services: "
        f"timer={os.path.join(SCRIPTS_ROOT, 'timer', 'timer.py')} "
        f"server={os.path.join(SCRIPTS_ROOT, 'server', 'server.py')} "
        f"tvon={os.path.join(SCRIPTS_ROOT, 'tvon', 'tvon.py')}"
    )
    cleanup_stale_project_processes(log_fn=lambda fmt, *a: log(fmt % a if a else fmt))

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log("MAIN service received SIGINT")
    except Exception as e:
        log(f"Error in MAIN service: {e}")
    finally:
        log("MAIN service IS STOPPED")


if __name__ == "__main__":
    main()
