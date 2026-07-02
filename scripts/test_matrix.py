#!/usr/bin/env python3
"""Проверка MAX7219 без запуска всего сервиса."""

import os
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import main as app_main
from modules import matrix

cfg = app_main.matrix_config
print("Matrix config:", cfg)

matrix.setup_matrix(
    cascaded=cfg.cascaded,
    block_orientation=cfg.block_orientation,
    rotate=cfg.rotate,
    brightness=15,
    spi_port=cfg.spi_port,
    spi_device=cfg.spi_device,
    din=cfg.din,
    clk=cfg.clk,
    cs=cfg.cs,
    interface=cfg.interface,
    blocks_reverse=cfg.blocks_reverse,
)

print("Self-test: all LEDs ON for 2 seconds...")
matrix.run_self_test(2.0)

print("Scrolling:", cfg.text_display)
matrix.start_scrolling_text(cfg.text_display, speed=10)
for _ in range(200):
    matrix.scroll_tick()
    time.sleep(0.05)

print("Done.")
