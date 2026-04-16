"""Baud-rate handling for Windows.

Trivial passthrough: ``DCB.BaudRate`` is a raw 32-bit integer per
design-windows-backend.md §6.2. Whether the driver accepts a given value is
driver-specific and surfaces as ``ERROR_INVALID_PARAMETER`` from
``SetCommState`` (translated to
:class:`anyserial.exceptions.UnsupportedConfigurationError` by the backend).

This module exists for parity with :mod:`anyserial._linux.baudrate` and
:mod:`anyserial._darwin.baudrate` so future cross-backend code can call a
uniform helper without branching on platform.
"""

from __future__ import annotations

_MIN_BAUD: int = 1
_MAX_BAUD: int = 0xFFFFFFFF


def validate_baudrate(baudrate: int) -> int:
    """Return ``baudrate`` if it fits a Win32 DWORD; raise otherwise.

    Driver acceptance is verified at apply time. This guard only catches
    obviously-wrong values (negative or >32-bit) before they reach the
    Win32 layer.
    """
    if not (_MIN_BAUD <= baudrate <= _MAX_BAUD):
        msg = f"baudrate {baudrate!r} does not fit a 32-bit DWORD (1..2**32-1)"
        raise ValueError(msg)
    return baudrate


__all__ = ["validate_baudrate"]
