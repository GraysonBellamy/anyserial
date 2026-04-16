"""Standard-baud termios-constant mapping for every POSIX backend.

``termios`` exposes each supported baud rate as a ``Bxxxx`` constant (e.g.,
``termios.B115200 == 0o10002``). Callers pass plain integers on the public
API, so every backend needs to translate between the two. This module builds
the mapping once at import time by scanning ``dir(termios)``.

The platform-extended constants (57600 through 4000000 on Linux) are only
present on platforms that defined them. Linux exposes up through 4 Mbaud via
this mechanism; macOS and BSD typically stop at 230400 and require a
platform-specific ioctl (``IOSSIOSPEED`` on Darwin, ``TCGETS2``/``BOTHER`` on
Linux) for anything else. That ioctl path belongs to the Linux / Darwin
backends; this helper is the generic fallback.
"""

from __future__ import annotations

import termios
from types import MappingProxyType
from typing import Final

from anyserial.exceptions import UnsupportedConfigurationError

_BAUD_CONSTANT_PREFIX_LEN = 2  # len("B9") — smallest ``Bxxxx`` name we accept


def _discover_standard_baud_rates() -> dict[int, int]:
    """Return ``{bps: termios.Bxxxx constant}`` for every rate this termios exposes."""
    mapping: dict[int, int] = {}
    for name in dir(termios):
        if not name.startswith("B") or len(name) < _BAUD_CONSTANT_PREFIX_LEN:
            continue
        if not name[1:].isdigit():
            continue
        bps = int(name[1:])
        if bps == 0:
            # B0 means "hang up"; we never want to request it as a baud rate.
            continue
        value = getattr(termios, name)
        if isinstance(value, int):
            mapping[bps] = value
    return mapping


STANDARD_BAUD_RATES: Final[MappingProxyType[int, int]] = MappingProxyType(
    _discover_standard_baud_rates(),
)
"""Immutable ``{bps: termios constant}`` map for the current platform."""


def baudrate_to_speed(rate: int) -> int:
    """Return the termios speed constant for ``rate``.

    Raises:
        UnsupportedConfigurationError: ``rate`` is not a standard baud
            exposed by this platform's termios module. Platform backends
            that support custom rates (Linux via ``BOTHER``, Darwin via
            ``IOSSIOSPEED``) handle that case themselves before falling
            through to this helper.
    """
    try:
        return STANDARD_BAUD_RATES[rate]
    except KeyError as exc:
        available = sorted(STANDARD_BAUD_RATES)
        msg = (
            f"baud rate {rate} is not a standard termios rate on this platform "
            f"(standard rates: {available})"
        )
        raise UnsupportedConfigurationError(msg) from exc


def is_standard_baud(rate: int) -> bool:
    """Return ``True`` when ``rate`` has a ``termios.Bxxxx`` constant."""
    return rate in STANDARD_BAUD_RATES


__all__ = [
    "STANDARD_BAUD_RATES",
    "baudrate_to_speed",
    "is_standard_baud",
]
