"""
Драйвер MAX7219 для Raspberry Pi 5 через luma.led_matrix (bitbang SPI + lgpio).

Ближайший аналог GyverMAX7219 + RunningGFX из Arduino-проекта.
"""

import os
import time

from PIL import Image
from luma.core.interface.serial import bitbang
from luma.led_matrix.device import max7219

# Пины SPI по умолчанию (BCM) — физ. пины 19, 23, 24 на Pi 4
MATRIX_DIN = 10   # MOSI
MATRIX_CLK = 11   # SCLK
MATRIX_CS = 8     # CE0

# Как в Arduino: MAX7219<4, 1, CS, DIO, CLK>
CASCADED_MODULES = 4
BLOCK_ORIENTATION = 90
ROTATE = 2

_device = None
_framebuffer = None
_scroll_runner = None


def _build_scroll_backing(message: str) -> tuple[Image.Image, int]:
    """Буфер бегущей строки — bitmap font5x8 из GyverGFX (как RunningGFX)."""
    if _device is None:
        raise RuntimeError("Matrix device is not initialized")
    from modules.matrix_font5x8 import draw_message, message_width

    gap = _device.width
    text_width = message_width(message)
    total_width = gap + text_width + gap
    backing = Image.new("1", (total_width, _device.height), 0)
    draw_message(backing, gap, message)
    # Текст въезжает с правого края (пустой экран), уезжает влево, затем пауза.
    max_scroll = gap + text_width
    return backing, max_scroll


class _ScrollRunner:
    """Неблокирующая бегущая строка (bitmap font5x8, кириллица)."""

    def __init__(self):
        self.backing: Image.Image | None = None
        self.scroll_x = 0
        self.max_scroll = 0
        self.active = False
        self.last_tick = 0.0
        self.delay_s = 0.05

    def start(self, message, speed=7):
        if _device is None:
            return
        self.backing, self.max_scroll = _build_scroll_backing(message)
        self.scroll_x = 0
        self.active = True
        self.last_tick = 0.0
        self.delay_s = max(0.01, 0.12 - speed * 0.007)
        self._display_frame()

    def _display_frame(self):
        if _device is None or self.backing is None:
            return
        left = self.scroll_x
        right = left + _device.width
        frame = self.backing.crop((left, 0, right, _device.height))
        _device.display(frame)

    def tick(self):
        if not self.active or self.backing is None or _device is None:
            return
        now = time.time()
        if self.last_tick and (now - self.last_tick) < self.delay_s:
            return
        self.last_tick = now
        self.scroll_x += 1
        if self.scroll_x > self.max_scroll:
            self.scroll_x = 0
        self._display_frame()

    def stop(self):
        self.active = False
        self.backing = None


def setup_matrix(
    cascaded=CASCADED_MODULES,
    block_orientation=BLOCK_ORIENTATION,
    rotate=ROTATE,
    brightness=7,
    din=MATRIX_DIN,
    clk=MATRIX_CLK,
    cs=MATRIX_CS,
    blocks_reverse=False,
):
    """Инициализация матрицы (аналог matrix.begin() + setBright())."""
    global _device, _framebuffer, _scroll_runner, MATRIX_DIN, MATRIX_CLK, MATRIX_CS
    MATRIX_DIN, MATRIX_CLK, MATRIX_CS = din, clk, cs

    from modules.lgpio_gpio import LgpioGPIO

    serial = bitbang(gpio=LgpioGPIO(), SCLK=clk, SDA=din, CE=cs)
    print(f"Matrix: bitbang SPI on DIN={din} CLK={clk} CS={cs}")

    _device = max7219(
        serial,
        cascaded=cascaded,
        block_orientation=block_orientation,
        rotate=rotate,
        blocks_arranged_in_reverse_order=blocks_reverse,
    )
    print(f"Matrix: display {cascaded * 8}x8, orientation={block_orientation}, reverse={blocks_reverse}")
    _framebuffer = None
    _scroll_runner = _ScrollRunner()
    set_brightness(brightness)
    return _device


def run_self_test(hold_s=1.5) -> bool:
    """Кратко зажигает все LED — проверка SPI и MAX7219."""
    if _device is None:
        return False
    try:
        stop_scrolling()
        set_brightness(15)
        on = Image.new("1", _device.size, 255)
        off = Image.new("1", _device.size, 0)
        _device.display(on)
        time.sleep(hold_s)
        _device.display(off)
        time.sleep(0.2)
        print(f"Matrix: self-test sent ({_device.width}x{_device.height}, all pixels ON)")
        return True
    except Exception as error:
        print(f"Matrix: self-test failed: {error}")
        return False


def start_scrolling_text(message, speed=7):
    if _scroll_runner is None:
        return
    _scroll_runner.start(message, speed)


def scroll_tick():
    if _scroll_runner is None:
        return
    _scroll_runner.tick()


def stop_scrolling():
    if _scroll_runner is None:
        return
    _scroll_runner.stop()


def set_brightness(level):
    """Яркость 0–15, как setBright() в GyverMAX7219."""
    if _device is None:
        return
    _device.contrast(min(15, max(0, level)) * 16)


def _ensure_framebuffer():
    global _framebuffer
    if _device is not None and _framebuffer is None:
        _framebuffer = Image.new("1", _device.size)


def clear():
    global _framebuffer
    if _device is None:
        return
    _ensure_framebuffer()
    _framebuffer.paste(0, (0, 0, _device.width, _device.height))


def flush():
    """Отправить буфер на матрицу (аналог matrix.update())."""
    if _device is None:
        return
    _ensure_framebuffer()
    _device.display(_framebuffer)


def dot(x, y, on=True):
    """Поставить точку. Для нескольких точек вызывайте flush() в конце."""
    if _device is None:
        return
    _ensure_framebuffer()
    _framebuffer.putpixel((x, y), 1 if on else 0)


def _blit_glyph(glyph, offset_x, offset_y):
    for row, line in enumerate(glyph):
        for col, pixel in enumerate(line):
            if pixel:
                dot(offset_x + col, offset_y + row)


def print_time(hours, minutes, seconds):
    """matrixPrintTime() из Arduino."""
    if _device is None:
        return
    stop_scrolling()
    from modules.matrix_glyphs import (
        COLON,
        DIGITS,
        ZONE_COLON,
        ZONE_NUMBER1,
        ZONE_NUMBER2,
        ZONE_NUMBER3,
        ZONE_NUMBER4,
    )

    clear()
    if hours > 0:
        n1, n2, n3, n4 = hours // 10, hours % 10, minutes // 10, minutes % 10
    else:
        n1, n2, n3, n4 = minutes // 10, minutes % 10, seconds // 10, seconds % 10

    _blit_glyph(DIGITS[n1], ZONE_NUMBER1, 0)
    _blit_glyph(DIGITS[n2], ZONE_NUMBER2, 0)
    if hours > 0:
        if seconds % 2 == 0:
            _blit_glyph(COLON, ZONE_COLON, 0)
    else:
        _blit_glyph(COLON, ZONE_COLON, 0)
    _blit_glyph(DIGITS[n3], ZONE_NUMBER3, 0)
    _blit_glyph(DIGITS[n4], ZONE_NUMBER4, 0)
    flush()


def print_waiting_time(seconds):
    """matrixPrintWaitingTime() из Arduino."""
    if _device is None:
        return
    stop_scrolling()
    from modules.matrix_glyphs import (
        DIGITS,
        SYMBOLS,
        ZONE_NUMBER3,
        ZONE_NUMBER4,
        ZONE_QUESTION,
        ZONE_RUBLE,
    )

    clear()
    _blit_glyph(SYMBOLS["$"], ZONE_RUBLE, 0)
    _blit_glyph(SYMBOLS["?"], ZONE_QUESTION, 0)
    _blit_glyph(DIGITS[seconds // 10], ZONE_NUMBER3, 0)
    _blit_glyph(DIGITS[seconds % 10], ZONE_NUMBER4, 0)
    flush()


def print_text(label):
    """matrixPrintText() из Arduino."""
    if _device is None:
        return
    stop_scrolling()
    from modules.matrix_glyphs import TEXTS

    bitmap = TEXTS.get(label)
    if bitmap is None:
        return
    clear()
    for row, line in enumerate(bitmap):
        for col, pixel in enumerate(line):
            if pixel:
                dot(col, row)
    flush()


def print_start(time_start=5):
    """matrixPrintStart() из Arduino."""
    if _device is None:
        return
    stop_scrolling()
    from modules.matrix_glyphs import DIGITS, TEXTS, ZONE_NUMBER4

    text_bitmap = TEXTS["ИГРА"]
    for counter in range(time_start, 0, -1):
        clear()
        for row, line in enumerate(text_bitmap):
            for col, pixel in enumerate(line):
                if pixel:
                    dot(col, row)
        _blit_glyph(DIGITS[counter], ZONE_NUMBER4, 0)
        flush()
        time.sleep(1)
