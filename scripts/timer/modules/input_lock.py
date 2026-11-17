"""Фильтр USB-контроллеров: без оплаты доступны навигация и громкость."""

from __future__ import annotations

import fcntl
import os
import re
import struct
import time
from pathlib import Path

EVIOCGRAB = 0x40044590
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
SYN_REPORT = 0
ABS_X = 0x00
ABS_Y = 0x01
KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106
BTN_BASE5 = 0x12A
BTN_BASE6 = 0x12B
BUS_USB = 0x03
INPUT_EVENT = struct.Struct("qqHHi")
UINPUT_SETUP = struct.Struct("HHHH80sI")
NAVIGATION_KEYS = (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT)
VOLUME_KEYS = (BTN_BASE5, BTN_BASE6)


def navigation_key(axis: int, value: int) -> tuple[int, bool] | None:
    if axis == ABS_X:
        key = KEY_LEFT
        opposite = KEY_RIGHT
    elif axis == ABS_Y:
        key = KEY_UP
        opposite = KEY_DOWN
    else:
        return None
    if value <= 63:
        return key, True
    if value >= 192:
        return opposite, True
    return (KEY_LEFT if axis == ABS_X else KEY_UP), False


def discover_input_devices(proc_path: str = "/proc/bus/input/devices") -> list[tuple[str, str]]:
    text = Path(proc_path).read_text(encoding="utf-8")
    found: list[tuple[str, str]] = []
    for block in text.strip().split("\n\n"):
        name_match = re.search(r'^N: Name="(.*)"$', block, re.MULTILINE)
        handlers_match = re.search(r"^H: Handlers=(.*)$", block, re.MULTILINE)
        if not name_match or not handlers_match:
            continue
        name = name_match.group(1).lower()
        handlers = handlers_match.group(1).split()
        is_controller = any(handler.startswith("js") for handler in handlers)
        if is_controller or any(word in name for word in ("joystick", "gamepad", "dragonrise")):
            for handler in handlers:
                if re.fullmatch(r"event\d+", handler):
                    found.append((handler, name_match.group(1)))
    return found


class InputLocker:
    def __init__(
        self,
        log_fn,
        devices_path: str = "/proc/bus/input/devices",
        uinput_path: str = "/dev/uinput",
    ):
        self.log = log_fn
        self.devices_path = devices_path
        self.uinput_path = uinput_path
        self.locked = False
        self.fds: dict[str, int] = {}
        self.uinput_fd: int | None = None
        self.pressed_keys: set[int] = set()
        self.last_scan = 0.0

    def lock(self) -> None:
        self.locked = True
        if self.uinput_fd is None and not self._open_navigation():
            self._release_controllers()
            return
        self._grab_new_devices()

    def unlock(self) -> None:
        had_lock = self.locked or bool(self.fds) or self.uinput_fd is not None
        self.locked = False
        self._release_keys()
        self._release_controllers()
        self._close_navigation()
        if had_lock:
            self.log("INPUT: controllers unlocked")

    def close(self) -> None:
        self.unlock()

    def poll(self) -> None:
        if not self.locked:
            return
        if self.uinput_fd is None:
            self._release_controllers()
            if not self._open_navigation():
                return
        self._forward_events()
        if time.monotonic() - self.last_scan >= 2:
            self._grab_new_devices()

    def _open_navigation(self) -> bool:
        try:
            fd = os.open(self.uinput_path, os.O_WRONLY | os.O_NONBLOCK)
            try:
                fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
                for key in NAVIGATION_KEYS + VOLUME_KEYS:
                    fcntl.ioctl(fd, UI_SET_KEYBIT, key)
                setup = UINPUT_SETUP.pack(
                    BUS_USB, 0x0001, 0x0001, 1, b"Arcade navigation", 0
                )
                fcntl.ioctl(fd, UI_DEV_SETUP, setup)
                fcntl.ioctl(fd, UI_DEV_CREATE, 0)
            except Exception:
                os.close(fd)
                raise
            self.uinput_fd = fd
            time.sleep(0.2)
            self.log("INPUT: navigation and volume enabled, game buttons locked")
            return True
        except OSError as error:
            self.log(f"INPUT: cannot create navigation device: {error}")
            return False

    def _close_navigation(self) -> None:
        if self.uinput_fd is None:
            return
        try:
            fcntl.ioctl(self.uinput_fd, UI_DEV_DESTROY, 0)
        except OSError:
            pass
        try:
            os.close(self.uinput_fd)
        except OSError:
            pass
        self.uinput_fd = None
        self.pressed_keys.clear()

    def _release_controllers(self) -> None:
        for path, fd in list(self.fds.items()):
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
            self.fds.pop(path, None)

    def _release_keys(self) -> None:
        for key in list(self.pressed_keys):
            self._emit(EV_KEY, key, 0)
        self.pressed_keys.clear()

    def _emit(self, event_type: int, code: int, value: int) -> None:
        if self.uinput_fd is None:
            return
        try:
            event = INPUT_EVENT.pack(0, 0, event_type, code, value)
            sync = INPUT_EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0)
            os.write(self.uinput_fd, event + sync)
        except OSError as error:
            self.log(f"INPUT: navigation device lost: {error}")
            self._close_navigation()
            self._release_controllers()

    def _forward_event(self, event_type: int, code: int, value: int) -> None:
        if event_type == EV_ABS:
            mapped = navigation_key(code, value)
            if mapped is None:
                return
            key, pressed = mapped
            if pressed:
                self.pressed_keys.add(key)
                self._emit(EV_KEY, key, 1)
            else:
                keys = (KEY_LEFT, KEY_RIGHT) if code == ABS_X else (KEY_UP, KEY_DOWN)
                for arrow in keys:
                    self.pressed_keys.discard(arrow)
                    self._emit(EV_KEY, arrow, 0)
        elif event_type == EV_KEY and code in VOLUME_KEYS:
            self._emit(EV_KEY, code, value)

    def _forward_events(self) -> None:
        for path, fd in list(self.fds.items()):
            while self.locked and self.uinput_fd is not None:
                try:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                except BlockingIOError:
                    break
                except OSError:
                    self._drop_controller(path)
                    break
                if not data:
                    self._drop_controller(path)
                    break
                for offset in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                    _sec, _usec, event_type, code, value = INPUT_EVENT.unpack_from(data, offset)
                    self._forward_event(event_type, code, value)

    def _drop_controller(self, path: str) -> None:
        fd = self.fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _grab_new_devices(self) -> None:
        self.last_scan = time.monotonic()
        if self.uinput_fd is None:
            return
        try:
            devices = discover_input_devices(self.devices_path)
        except OSError as error:
            self.log(f"INPUT: cannot list controllers: {error}")
            return
        current = {f"/dev/input/{handler}" for handler, _name in devices}
        for path in list(self.fds):
            if path not in current:
                self._drop_controller(path)
        for handler, name in devices:
            path = f"/dev/input/{handler}"
            if path in self.fds:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                self.fds[path] = fd
                self.log(f"INPUT: locked {name} ({handler})")
            except OSError as error:
                self.log(f"INPUT: cannot lock {name} ({handler}): {error}")
