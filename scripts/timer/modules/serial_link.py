"""Обмен таймера Raspberry с Arduino по USB Serial."""

from __future__ import annotations

import os
import re
import time

STATES = {"READY", "PLAYING", "PAUSED", "WAITING", "STOPPING"}


def parse_state_line(line: str) -> dict | None:
    parts = line.strip().split()
    if parts == ["HELLO", "1"]:
        return {"state": "HELLO", "seconds": 0}
    if len(parts) != 3 or parts[0] != "STATE" or parts[1] not in STATES:
        return None
    try:
        seconds = int(parts[2])
    except ValueError:
        return None
    if seconds < 0:
        return None
    return {"state": parts[1], "seconds": seconds}


class SerialLink:
    def __init__(self, device: str, on_state, log_fn):
        self.device = device
        self.on_state = on_state
        self.log = log_fn
        self.fd: int | None = None
        self.buffer = b""
        self.last_attempt = 0.0

    def start(self) -> bool:
        return self._open()

    def _open(self) -> bool:
        self.last_attempt = time.monotonic()
        try:
            import termios
            import tty

            fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                tty.setraw(fd)
                attrs = termios.tcgetattr(fd)
                attrs[4] = termios.B115200
                attrs[5] = termios.B115200
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                termios.tcflush(fd, termios.TCIOFLUSH)
            except Exception:
                os.close(fd)
                raise
            self.fd = fd
            self.buffer = b""
            self.log(f"SERIAL: connected {self.device} at 115200")
            return True
        except OSError as error:
            self.log(f"SERIAL: cannot open {self.device}: {error}")
            return False

    def send(self, command: str) -> bool:
        if self.fd is None or not re.fullmatch(r"[A-Z_]+(?: [0-9]+)?", command):
            return False
        try:
            os.write(self.fd, command.encode("ascii") + b"\n")
            self.log(f"SERIAL: → {command}")
            return True
        except OSError as error:
            self.log(f"SERIAL: write failed: {error}")
            self._close_fd()
            return False

    def poll(self) -> None:
        if self.fd is None:
            if time.monotonic() - self.last_attempt >= 2:
                self._open()
            return
        while self.fd is not None:
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                return
            except OSError as error:
                self.log(f"SERIAL: read failed: {error}")
                self._close_fd()
                return
            if not chunk:
                self._close_fd()
                return
            self.buffer += chunk
            while b"\n" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\n", 1)
                state = parse_state_line(raw.decode("ascii", errors="replace"))
                if state is not None:
                    self.on_state(state)

    def _close_fd(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.buffer = b""

    def close(self) -> None:
        self._close_fd()
