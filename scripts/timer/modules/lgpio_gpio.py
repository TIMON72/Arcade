"""Обёртка lgpio с API, совместимым с luma bitbang (вместо RPi.GPIO)."""

import lgpio

_chip: int | None = None
_claimed_pins: list[int] = []


class LgpioGPIO:
    OUT = 1
    IN = 0
    HIGH = 1
    LOW = 0

    def setup(self, pin: int, mode: int, initial=None) -> None:
        global _chip, _claimed_pins
        if _chip is None:
            _chip = lgpio.gpiochip_open(0)
        if pin in _claimed_pins:
            return
        lgpio.gpio_claim_output(_chip, pin, lgpio.SET_PULL_NONE)
        _claimed_pins.append(pin)
        if initial is not None:
            self.output(pin, initial)

    def output(self, pin: int, value: int) -> None:
        if _chip is None:
            raise RuntimeError("GPIO chip is not open")
        lgpio.gpio_write(_chip, pin, 1 if value else 0)

    def cleanup(self, pins=None) -> None:
        global _chip, _claimed_pins
        if _chip is None:
            return
        released = set(pins or _claimed_pins)
        for pin in released:
            try:
                lgpio.gpio_free(_chip, pin)
            except Exception:
                pass
        _claimed_pins = [pin for pin in _claimed_pins if pin not in released]
        if not _claimed_pins and _chip is not None:
            try:
                lgpio.gpiochip_close(_chip)
            except Exception:
                pass
            _chip = None
