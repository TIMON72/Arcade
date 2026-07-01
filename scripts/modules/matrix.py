"""
Драйвер MAX7219 для Raspberry Pi 5 через luma.led_matrix.

Ближайший аналог GyverMAX7219 + RunningGFX из Arduino-проекта:
  matrix.begin()      -> setup_matrix()
  matrix.setBright()  -> set_brightness()
  matrix.clear()      -> clear()
  matrix.dot(x, y)    -> dot() + flush()
  matrix_run.tick()   -> scroll_tick()
"""

import time

from PIL import Image

from luma.core.interface.serial import noop, spi
from luma.core.legacy import show_message, text
from luma.core.legacy.font import CP437_FONT, proportional
from luma.core.render import canvas
from luma.core.virtual import viewport
from luma.led_matrix.device import max7219

# Пины SPI (BCM) — физ. пины 19, 23, 24
MATRIX_DIN = 10   # MOSI
MATRIX_CLK = 11   # SCLK
MATRIX_CS = 8     # CE0

# Как в Arduino: MAX7219<4, 1, CS, DIO, CLK>
CASCADED_MODULES = 4
BLOCK_ORIENTATION = 0
ROTATE = 0

_device = None
_framebuffer = None
_scroll_runner = None


class _ScrollRunner:
    """Неблокирующая бегущая строка (аналог RunningGFX)."""

    def __init__(self):
        self.virtual = None
        self.offset = 0
        self.active = False
        self.last_tick = 0.0
        self.delay_s = 0.05

    def start(self, message, speed=7):
        if _device is None:
            return
        text_width = max(_device.width, len(message) * 6 + _device.width)
        self.virtual = viewport(_device, width=text_width, height=_device.height)
        with canvas(self.virtual) as draw:
            text(draw, (0, 0), message, fill="white", font=proportional(CP437_FONT))
        self.offset = -_device.width
        self.active = True
        self.last_tick = 0.0
        self.delay_s = max(0.01, 0.12 - speed * 0.007)

    def tick(self):
        if not self.active or self.virtual is None or _device is None:
            return
        now = time.time()
        if self.last_tick and (now - self.last_tick) < self.delay_s:
            return
        self.last_tick = now
        self.offset += 1
        if self.offset > self.virtual.width:
            self.offset = -_device.width
        self.virtual.set_position((self.offset, 0))

    def stop(self):
        self.active = False
        self.virtual = None


def setup_matrix(
    cascaded=CASCADED_MODULES,
    block_orientation=BLOCK_ORIENTATION,
    rotate=ROTATE,
    brightness=7,
):
    """Инициализация матрицы (аналог matrix.begin() + setBright())."""
    global _device, _framebuffer, _scroll_runner
    serial = spi(port=0, device=0, gpio=noop())
    _device = max7219(
        serial,
        cascaded=cascaded,
        block_orientation=block_orientation,
        rotate=rotate,
    )
    _framebuffer = None
    _scroll_runner = _ScrollRunner()
    set_brightness(brightness)
    return _device


def is_ready():
    return _device is not None


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


def show_scrolling_text(message, speed=7):
    """
    Бегущая строка (аналог RunningGFX).
    speed: 1–15, чем больше — тем быстрее (как setSpeed в Arduino).
    """
    if _device is None:
        return
    scroll_delay = max(0.01, 0.12 - speed * 0.007)
    show_message(
        _device,
        message,
        fill="white",
        font=proportional(CP437_FONT),
        scroll_delay=scroll_delay,
    )


def show_static_text(message):
    """Короткий текст без прокрутки."""
    if _device is None:
        return
    stop_scrolling()
    with canvas(_device) as draw:
        text(draw, (0, 0), message, fill="white", font=proportional(CP437_FONT))


def show_time(hours, minutes, seconds):
    """Вывод MM:SS или HH:MM."""
    if _device is None:
        return
    stop_scrolling()
    if hours > 0:
        message = f"{hours:02d}:{minutes:02d}"
    else:
        message = f"{minutes:02d}:{seconds:02d}"
    with canvas(_device) as draw:
        text(draw, (0, 0), message, fill="white", font=proportional(CP437_FONT))


def show_waiting_time(seconds):
    """Режим ожидания: $?SS (аналог matrixPrintWaitingTime)."""
    if _device is None:
        return
    stop_scrolling()
    with canvas(_device) as draw:
        text(draw, (0, 0), f"$?{seconds:02d}", fill="white", font=proportional(CP437_FONT))


def show_countdown(count):
    """Обратный отсчёт перед стартом (аналог matrixPrintStart)."""
    if _device is None:
        return
    stop_scrolling()
    with canvas(_device) as draw:
        text(draw, (0, 0), f"IGRA {count}", fill="white", font=proportional(CP437_FONT))


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
