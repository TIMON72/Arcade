"""Payment backend. v1 = stub; later replace with AQSI Cube VEND client."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class PaymentResult:
    ok: bool
    status: str
    message: str = ""
    amount: float = 0.0


def pay(amount: float, currency: int = 643, stub_delay_sec: float = 1.5) -> PaymentResult:
    """Charge `amount` (RUB). Stub always succeeds after a short delay."""
    logger.info("payment stub: amount=%.2f currency=%s", amount, currency)
    time.sleep(stub_delay_sec)
    return PaymentResult(
        ok=True,
        status="ok",
        message="stub approved",
        amount=amount,
    )


def pay_package(price: float, minutes: int, stub_delay_sec: float = 1.5) -> PaymentResult:
    result = pay(price, stub_delay_sec=stub_delay_sec)
    if result.ok:
        logger.info("payment stub approved for %s min / %.0f RUB", minutes, price)
    return result
