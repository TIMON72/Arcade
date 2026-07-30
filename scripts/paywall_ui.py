"""Paywall overlay — gameStart gate and WAITING extension menu."""

from __future__ import annotations

import json
import logging
import os
import queue
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

import payment
import session

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGES_PATH = SCRIPT_DIR / "packages.json"
ES_INPUT_CANDIDATES = [
    Path("/userdata/system/configs/emulationstation/es_last_input.cfg"),
    Path("/userdata/system/configs/emulationstation/es_input.cfg"),
]

COLS, ROWS = 3, 2
BG = (12, 14, 20)
CARD = (28, 32, 44)
CARD_FOCUS = (40, 52, 78)
ACCENT = (70, 160, 255)
TEXT = (235, 240, 250)
MUTED = (140, 150, 170)
WARN = (240, 180, 70)

HAT_CENTER = (0, 0)
AXIS_DEADZONE = 0.55
NAV_COOLDOWN_SEC = 0.22
IDLE_CANCEL_SEC = 60
PLAYER1_JOY_INDEX = 0
PAY_STUB_SEC = 5.0


class UiState(Enum):
    IDLE = auto()  # no window — ES attract / menu / gameplay free
    LOCKED = auto()
    PAYING = auto()


@dataclass
class PadMap:
    btn_confirm: int = 0
    btn_cancel: int = 1
    axis_x: int = 1
    axis_y: int = 0
    left_sign: int = 1
    right_sign: int = -1
    up_sign: int = -1
    down_sign: int = 1


def setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(SCRIPT_DIR / "logs.log", encoding="utf-8")],
    )


def load_packages() -> list[dict[str, Any]]:
    data = json.loads(PACKAGES_PATH.read_text(encoding="utf-8"))
    if len(data) != 6:
        raise ValueError("expected 6 packages")
    return data


def load_pad_map() -> PadMap:
    pad = PadMap()
    for path in ES_INPUT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            cfg = ET.parse(path).getroot().find("inputConfig")
            if cfg is None:
                continue
            by_name = {inp.get("name"): inp for inp in cfg.findall("input")}
            for dir_name, attr_axis, attr_sign in (
                ("left", "axis_x", "left_sign"),
                ("right", "axis_x", "right_sign"),
                ("up", "axis_y", "up_sign"),
                ("down", "axis_y", "down_sign"),
            ):
                node = by_name.get(dir_name)
                if node is not None and node.get("type") == "axis":
                    setattr(pad, attr_axis, int(node.get("id")))
                    setattr(pad, attr_sign, int(float(node.get("value", "1"))))
            override = SCRIPT_DIR / "pad_buttons.json"
            if override.is_file():
                data = json.loads(override.read_text(encoding="utf-8"))
                pad.btn_confirm = int(data.get("confirm", pad.btn_confirm))
                pad.btn_cancel = int(data.get("cancel", pad.btn_cancel))
            logger.info("Pad map confirm=%s cancel=%s", pad.btn_confirm, pad.btn_cancel)
            return pad
        except Exception as exc:
            logger.warning("pad map parse %s: %s", path, exc)
    return pad


def inherit_display_env() -> None:
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        if not os.environ.get("SDL_VIDEODRIVER"):
            os.environ["SDL_VIDEODRIVER"] = (
                "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"
            )
        return
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="ignore"
            )
            if "emulationstation" not in cmdline.lower():
                continue
            for item in (entry / "environ").read_bytes().split(b"\x00"):
                if b"=" not in item:
                    continue
                key, _, value = item.partition(b"=")
                ks = key.decode("utf-8", errors="ignore")
                if ks in (
                    "WAYLAND_DISPLAY",
                    "DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "XDG_SESSION_TYPE",
                    "SDL_VIDEODRIVER",
                ):
                    os.environ.setdefault(ks, value.decode("utf-8", errors="ignore"))
            break
    except Exception as exc:
        logger.warning("inherit display: %s", exc)
    if not os.environ.get("SDL_VIDEODRIVER"):
        if os.environ.get("WAYLAND_DISPLAY"):
            os.environ["SDL_VIDEODRIVER"] = "wayland"
        elif os.environ.get("DISPLAY"):
            os.environ["SDL_VIDEODRIVER"] = "x11"


class PaywallApp:
    def __init__(
        self,
        queue_main=None,
        queue_to_ui=None,
        queue_from_ui=None,
    ):
        self.queue_main = queue_main
        self.queue_to_ui = queue_to_ui
        self.queue_from_ui = queue_from_ui
        self.packages = load_packages()
        self.pad = load_pad_map()
        self.state = UiState.IDLE
        self.focus = 0
        self.paying_package = None
        self.paying_started_at = None
        self.last_activity_at = time.monotonic()
        self._nav_ready_at = 0.0
        self._axis_latched = False
        self._player1 = None
        self._display_up = False
        self.screen = None
        self._show_reason = "launch"

        inherit_display_env()
        import pygame

        self.pygame = pygame
        pygame.init()
        pygame.joystick.init()

        pygame.display.init()
        info = pygame.display.Info()
        self.width = info.current_w or 1920
        self.height = info.current_h or 1080
        pygame.display.quit()

        self.font_title = self.font_big = self.font_mid = self.font_small = None
        self.clock = pygame.time.Clock()
        self.running = True
        self._init_player1()
        logger.info("Paywall IDLE — wait for gameStart (ES free)")

    def _touch(self) -> None:
        self.last_activity_at = time.monotonic()

    def _init_player1(self) -> None:
        self._player1 = None
        count = self.pygame.joystick.get_count()
        for i in range(count):
            joy = self.pygame.joystick.Joystick(i)
            joy.init()
        if count > PLAYER1_JOY_INDEX:
            self._player1 = self.pygame.joystick.Joystick(PLAYER1_JOY_INDEX)
            self._player1.init()
            logger.info("PLAYER1: %s", self._player1.get_name())

    def _is_player1_event(self, event: Any) -> bool:
        joy_index = getattr(event, "joy", None)
        return joy_index is not None and int(joy_index) == PLAYER1_JOY_INDEX

    def _close_window(self) -> None:
        pygame = self.pygame
        try:
            pygame.event.set_grab(False)
        except Exception:
            pass
        if self._display_up:
            try:
                pygame.display.quit()
            except Exception:
                pass
        self.screen = None
        self._display_up = False
        # Full re-init next open — Wayland/SDL often breaks after display.quit().
        try:
            pygame.joystick.quit()
        except Exception:
            pass
        try:
            pygame.quit()
        except Exception:
            pass
        try:
            inherit_display_env()
            pygame.init()
            pygame.joystick.init()
            self.clock = pygame.time.Clock()
        except Exception as exc:
            logger.warning("pygame re-init after close: %s", exc)
        self._init_player1()

    def _open_window(self) -> None:
        pygame = self.pygame
        inherit_display_env()
        if not pygame.get_init():
            pygame.init()
        if not pygame.joystick.get_init():
            pygame.joystick.init()
        try:
            pygame.display.init()
        except Exception:
            pass
        self.screen = None
        errors: list[str] = []
        for flags in (
            pygame.FULLSCREEN,
            pygame.FULLSCREEN | getattr(pygame, "SCALED", 0),
            0,
        ):
            try:
                size = (self.width, self.height) if flags else (0, 0)
                self.screen = pygame.display.set_mode(size, flags)
                break
            except Exception as exc:
                errors.append(str(exc))
                self.screen = None
        if self.screen is None:
            raise RuntimeError("display open failed: " + " | ".join(errors))
        pygame.display.set_caption("Arcade Paywall")
        self.font_title = pygame.font.SysFont("DejaVu Sans", 54, bold=True)
        self.font_big = pygame.font.SysFont("DejaVu Sans", 48, bold=True)
        self.font_mid = pygame.font.SysFont("DejaVu Sans", 36)
        self.font_small = pygame.font.SysFont("DejaVu Sans", 24)
        try:
            pygame.event.set_grab(True)
        except Exception:
            pass
        pygame.mouse.set_visible(False)
        try:
            pygame.event.clear()
        except Exception:
            pass
        self._display_up = True
        self._init_player1()

    def _reply(self, payload: dict[str, Any]) -> None:
        if self.queue_from_ui is not None:
            self.queue_from_ui.put(payload)

    def _enter_idle(self) -> None:
        self.state = UiState.IDLE
        self.paying_package = None
        self.paying_started_at = None
        self.focus = 0
        self._close_window()
        logger.info("IDLE — pads released")

    def _enter_locked(self, meta: Optional[dict] = None) -> None:
        self.state = UiState.LOCKED
        self.paying_package = None
        self.paying_started_at = None
        self.focus = 0
        self._show_reason = (meta or {}).get("reason") or "launch"
        self._touch()
        try:
            self._open_window()
        except Exception as exc:
            logger.error("Failed to open paywall window: %s", exc)
            self._reply({"status": "cancelled", "message": f"display:{exc}"})
            self.state = UiState.IDLE
            self.screen = None
            self._display_up = False
            return
        self._axis_latched = True
        logger.info("LOCKED — paywall %s %s", self._show_reason, meta or {})

    def _finish_cancelled(self) -> None:
        logger.info("Paywall cancelled")
        self._reply({"status": "cancelled"})
        self._enter_idle()

    def _finish_ok(self, minutes: int, price: float) -> None:
        rem = session.add_minutes(minutes)
        if self.queue_main is not None:
            self.queue_main.put(f"ADD_{minutes}")
            logger.info("Queued ADD_%s (session %.0fs)", minutes, rem)
        self._reply(
            {
                "status": "ok",
                "minutes": minutes,
                "price": price,
                "remaining_sec": int(rem),
            }
        )
        self._enter_idle()

    def _confirm(self) -> None:
        if self.state != UiState.LOCKED:
            return
        pkg = self.packages[self.focus]
        self.paying_package = pkg
        self.state = UiState.PAYING
        self.paying_started_at = time.monotonic()
        self._touch()
        logger.info("Paying for %s min / %s", pkg["minutes"], pkg["price"])

    def _cancel_paying(self) -> None:
        if self.state != UiState.PAYING:
            return
        self.state = UiState.LOCKED
        self.paying_package = None
        self.paying_started_at = None
        self._touch()

    def _complete_payment(self) -> None:
        pkg = self.paying_package
        if not pkg:
            self._finish_cancelled()
            return
        result = payment.pay_package(float(pkg["price"]), int(pkg["minutes"]), stub_delay_sec=0)
        if not result.ok:
            self.state = UiState.LOCKED
            self.paying_package = None
            self._touch()
            return
        self._finish_ok(int(pkg["minutes"]), float(pkg["price"]))

    def _card_rect(self, index: int):
        margin_x = int(self.width * 0.06)
        margin_y = int(self.height * 0.18)
        gap = int(self.width * 0.02)
        usable_w = self.width - 2 * margin_x - 2 * gap
        usable_h = self.height - margin_y - int(self.height * 0.12) - gap
        card_w = usable_w // COLS
        card_h = usable_h // ROWS
        col = index % COLS
        row = index // COLS
        return self.pygame.Rect(
            margin_x + col * (card_w + gap),
            margin_y + row * (card_h + gap),
            card_w,
            card_h,
        )

    def _move_focus(self, dx: int, dy: int) -> None:
        now = time.monotonic()
        if now < self._nav_ready_at:
            return
        col = max(0, min(COLS - 1, self.focus % COLS + dx))
        row = max(0, min(ROWS - 1, self.focus // COLS + dy))
        self.focus = row * COLS + col
        self._nav_ready_at = now + NAV_COOLDOWN_SEC
        self._touch()

    def _axis_to_dirs(self, joy) -> tuple[int, int]:
        pad = self.pad
        if joy.get_numaxes() <= max(pad.axis_x, pad.axis_y):
            return 0, 0
        ax, ay = joy.get_axis(pad.axis_x), joy.get_axis(pad.axis_y)
        dx = dy = 0
        if (pad.left_sign > 0 and ax >= AXIS_DEADZONE) or (
            pad.left_sign < 0 and ax <= -AXIS_DEADZONE
        ):
            dx = -1
        if (pad.right_sign > 0 and ax >= AXIS_DEADZONE) or (
            pad.right_sign < 0 and ax <= -AXIS_DEADZONE
        ):
            dx = 1
        if (pad.up_sign > 0 and ay >= AXIS_DEADZONE) or (
            pad.up_sign < 0 and ay <= -AXIS_DEADZONE
        ):
            dy = -1
        if (pad.down_sign > 0 and ay >= AXIS_DEADZONE) or (
            pad.down_sign < 0 and ay <= -AXIS_DEADZONE
        ):
            dy = 1
        return dx, dy

    def _handle_nav(self) -> None:
        if self.state != UiState.LOCKED or self._player1 is None:
            return
        joy = self._player1
        if joy.get_numhats() > 0:
            hx, hy = joy.get_hat(0)
            if (hx, hy) != HAT_CENTER:
                self._move_focus(hx, -hy)
                return
        dx, dy = self._axis_to_dirs(joy)
        if dx or dy:
            if not self._axis_latched:
                self._move_focus(dx, dy)
                self._axis_latched = True
            return
        self._axis_latched = False

    def _poll_commands(self) -> None:
        if self.queue_to_ui is None:
            return
        try:
            while True:
                msg = self.queue_to_ui.get_nowait()
                cmd = (msg or {}).get("cmd")
                if cmd == "show" and self.state == UiState.IDLE:
                    self._enter_locked(msg)
                elif cmd == "cancel" and self.state != UiState.IDLE:
                    self._finish_cancelled()
        except queue.Empty:
            pass

    def _handle_events(self) -> None:
        if self.state == UiState.IDLE or not self._display_up:
            return
        pygame = self.pygame
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self._finish_cancelled()
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_b):
                    if self.state == UiState.PAYING:
                        self._cancel_paying()
                    elif self.state == UiState.LOCKED:
                        self._finish_cancelled()
                elif event.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_a):
                    if self.state == UiState.LOCKED:
                        self._confirm()
                elif self.state == UiState.LOCKED:
                    self._touch()
                    if event.key in (pygame.K_LEFT, pygame.K_h):
                        self._move_focus(-1, 0)
                    elif event.key in (pygame.K_RIGHT, pygame.K_l):
                        self._move_focus(1, 0)
                    elif event.key in (pygame.K_UP, pygame.K_k):
                        self._move_focus(0, -1)
                    elif event.key in (pygame.K_DOWN, pygame.K_j):
                        self._move_focus(0, 1)
            elif event.type == pygame.JOYBUTTONDOWN and self._is_player1_event(event):
                self._touch()
                if event.button == self.pad.btn_confirm and self.state == UiState.LOCKED:
                    self._confirm()
                elif event.button == self.pad.btn_cancel:
                    if self.state == UiState.PAYING:
                        self._cancel_paying()
                    elif self.state == UiState.LOCKED:
                        self._finish_cancelled()
            elif event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                self._init_player1()
        self._handle_nav()

    def _draw(self) -> None:
        if not self._display_up or self.screen is None:
            return
        pygame = self.pygame
        if self.state == UiState.LOCKED:
            self.screen.fill(BG)
            title_text = (
                "Продление оплаты"
                if self._show_reason == "extend"
                else "Оплата для запуска игры"
            )
            title = self.font_title.render(title_text, True, TEXT)
            self.screen.blit(
                title, title.get_rect(center=(self.width // 2, int(self.height * 0.08)))
            )
            left = max(0, int(IDLE_CANCEL_SEC - (time.monotonic() - self.last_activity_at)))
            hint = self.font_small.render(
                f"A — оплатить  ·  B — отмена  ·  {left}с",
                True,
                MUTED,
            )
            self.screen.blit(hint, hint.get_rect(center=(self.width // 2, int(self.height * 0.94))))
            for i, pkg in enumerate(self.packages):
                rect = self._card_rect(i)
                focused = i == self.focus
                pygame.draw.rect(
                    self.screen, CARD_FOCUS if focused else CARD, rect, border_radius=18
                )
                if focused:
                    pygame.draw.rect(self.screen, ACCENT, rect, width=4, border_radius=18)
                time_s = self.font_big.render(pkg["label"], True, TEXT)
                price_s = self.font_mid.render(
                    f"{int(pkg['price'])} ₽", True, ACCENT if focused else TEXT
                )
                self.screen.blit(
                    time_s, time_s.get_rect(center=(rect.centerx, rect.centery - 24))
                )
                self.screen.blit(
                    price_s, price_s.get_rect(center=(rect.centerx, rect.centery + 36))
                )
            pygame.display.flip()
        elif self.state == UiState.PAYING:
            self.screen.fill(BG)
            pkg = self.paying_package or {}
            title = self.font_title.render("Оплата", True, WARN)
            self.screen.blit(
                title, title.get_rect(center=(self.width // 2, self.height // 2 - 80))
            )
            detail = self.font_mid.render(
                f"{pkg.get('label', '')} — {int(pkg.get('price', 0))} ₽", True, TEXT
            )
            self.screen.blit(detail, detail.get_rect(center=(self.width // 2, self.height // 2)))
            tip = self.font_small.render("B — назад", True, MUTED)
            self.screen.blit(
                tip, tip.get_rect(center=(self.width // 2, self.height // 2 + 70))
            )
            pygame.display.flip()

    def _update(self) -> None:
        now = time.monotonic()
        if self.state in (UiState.LOCKED, UiState.PAYING):
            if now - self.last_activity_at >= IDLE_CANCEL_SEC:
                self._finish_cancelled()
                return
        if self.state == UiState.PAYING and self.paying_started_at is not None:
            if now - self.paying_started_at >= PAY_STUB_SEC:
                self._complete_payment()

    def run(self) -> None:
        pygame = self.pygame
        while self.running:
            try:
                self._poll_commands()
                self._handle_events()
                self._update()
                if self.state != UiState.IDLE and self._display_up:
                    self._draw()
                    try:
                        self.clock.tick(30)
                    except Exception:
                        time.sleep(0.03)
                else:
                    time.sleep(0.05)
            except pygame.error as exc:
                logger.warning("pygame recover: %s", exc)
                if self.state != UiState.IDLE:
                    self._finish_cancelled()
                else:
                    self._close_window()
                time.sleep(0.5)
            except Exception as exc:
                logger.exception("paywall loop error: %s", exc)
                if self.state != UiState.IDLE:
                    try:
                        self._finish_cancelled()
                    except Exception:
                        self._reply({"status": "cancelled", "message": "ui_error"})
                        self.state = UiState.IDLE
                time.sleep(0.5)
        self._close_window()
        logger.info("Paywall stopped")


def loop(queue_main=None, queue_to_ui=None, queue_from_ui=None) -> None:
    setup_logging()
    time.sleep(5)
    app = PaywallApp(queue_main, queue_to_ui, queue_from_ui)
    app.run()


if __name__ == "__main__":
    loop()
