"""
Минимальная замена py-spidev через ioctl (без компиляции C).

Используется на Batocera и других системах, где pip install spidev не собирается.
"""

import os
import struct
import fcntl

SPI_IOC_MAGIC = ord("k")


def _IOW(type_: int, nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (type_ << 8) | nr


SPI_IOC_WR_MODE = _IOW(SPI_IOC_MAGIC, 1, 1)
SPI_IOC_WR_BITS_PER_WORD = _IOW(SPI_IOC_MAGIC, 3, 1)
SPI_IOC_WR_MAX_SPEED_HZ = _IOW(SPI_IOC_MAGIC, 4, 4)


class SpiDev:
    def __init__(self):
        self._fd: int | None = None
        self._mode = 0
        self._max_speed_hz = 500000
        self.no_cs = False

    def open(self, bus: int, device: int) -> None:
        path = f"/dev/spidev{bus}.{device}"
        self._fd = os.open(path, os.O_RDWR)
        self.mode = 0
        fcntl.ioctl(self._fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def mode(self) -> int:
        return self._mode

    @mode.setter
    def mode(self, value: int) -> None:
        self._mode = value
        if self._fd is not None:
            fcntl.ioctl(self._fd, SPI_IOC_WR_MODE, struct.pack("B", value & 0xFF))

    @property
    def max_speed_hz(self) -> int:
        return self._max_speed_hz

    @max_speed_hz.setter
    def max_speed_hz(self, value: int) -> None:
        self._max_speed_hz = value
        if self._fd is not None:
            fcntl.ioctl(self._fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", value))

    def writebytes(self, data: list[int] | bytes) -> None:
        if self._fd is None:
            raise OSError("SPI device is not open")
        os.write(self._fd, bytes(data))
