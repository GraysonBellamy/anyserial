"""Darwin-specific custom baud via ``IOSSIOSPEED``.

macOS provides no equivalent to Linux's ``TCSETS2`` / ``BOTHER`` path
(see :mod:`anyserial._linux.baudrate`). Instead,
``<IOKit/serial/ioss.h>`` exposes a dedicated ioctl, ``IOSSIOSPEED``, that
overrides the UART driver's line speed with an arbitrary integer rate.
Python's stdlib does not wrap it, but the request code is stable kernel
ABI so we hardcode it. pySerial has relied on the same number for years.

Usage pattern (see :class:`DarwinBackend._apply_custom_baud_config`):

1. Apply the rest of :class:`SerialConfig` through :func:`termios.tcsetattr`
   with any standard baud rate as a placeholder — the kernel needs a valid
   ``Bxxxx`` constant in ``c_ispeed`` / ``c_ospeed`` or the ``tcsetattr``
   call refuses the entire attribute set.
2. Call ``ioctl(fd, IOSSIOSPEED, &rate)`` to override the line speed with
   the caller's actual integer rate. Subsequent ``tcgetattr`` calls will
   still report the placeholder — ``IOSSIOSPEED`` affects the hardware, not
   the termios struct. That means a later ``tcsetattr`` re-applies the
   placeholder and reverts the speed unless the caller re-runs
   ``IOSSIOSPEED`` afterwards, which is exactly what the backend does on
   every :meth:`DarwinBackend.configure` call.

Ioctl code derivation (``_IOW('T', 2, speed_t)``):

- macOS ``_IOW(g, n, t)`` = ``IOC_IN (0x80000000)
  | ((sizeof(t) & IOCPARM_MASK (0x1fff)) << 16) | (g << 8) | n``.
- ``speed_t`` on Darwin is ``unsigned long`` → 8 bytes on every supported
  build (macOS has been 64-bit only since Catalina, and ``anyserial``
  requires Python 3.13+).
- ``g = 'T' = 0x54``, ``n = 2`` → ``0x80000000 | 0x80000 | 0x5400 | 0x02``
  = ``0x80085402``.
"""

from __future__ import annotations

import fcntl
import struct
from typing import Final

# <IOKit/serial/ioss.h>: #define IOSSIOSPEED _IOW('T', 2, speed_t).
# Decomposed in the module docstring; stable since IOKit SerialFamily landed.
IOSSIOSPEED: Final[int] = 0x80085402

# speed_t is ``unsigned long`` on macOS, which is 8 bytes on every 64-bit
# Darwin kernel. ``@Q`` is native-byte-order unsigned long long (8 bytes);
# macOS is little-endian only on both x86_64 and arm64.
_SPEED_STRUCT: Final[struct.Struct] = struct.Struct("@Q")


def set_iossiospeed(fd: int, rate: int) -> None:
    """Override ``fd``'s line speed with ``rate`` via ``IOSSIOSPEED``.

    Must be called *after* :func:`termios.tcsetattr` applies the rest of
    the line attributes — a later ``tcsetattr`` call will revert the speed
    to whatever placeholder is encoded in ``c_ispeed`` / ``c_ospeed``, so
    :meth:`DarwinBackend.configure` pairs the two calls on every apply.

    Args:
        fd: Open file descriptor for the serial device.
        rate: Desired baud rate in bits per second. The kernel lets any
            value through; whether the specific adapter honours it is
            driver- and hardware-dependent. A ``EINVAL`` comes back as an
            :class:`OSError` for the backend to route through
            :func:`errno_to_exception` with ``context="configure"``.

    Raises:
        OSError: The kernel rejected the ioctl. Callers (backends) route
            this through their capability / policy machinery.
    """
    fcntl.ioctl(fd, IOSSIOSPEED, _SPEED_STRUCT.pack(rate))


__all__ = [
    "IOSSIOSPEED",
    "set_iossiospeed",
]
