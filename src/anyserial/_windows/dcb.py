"""Translate :class:`SerialConfig` to and from a Win32 ``DCB`` structure.

design-windows-backend.md §6.2 / §6.3 are the source of truth. Invariants
applied to every DCB we ship:

- ``DCBlength = sizeof(DCB)`` (28 bytes).
- ``fBinary = 1`` — Windows documents non-binary mode as unsupported.
- ``fAbortOnError = 0`` — flipping it forces ``ClearCommError`` after every
  error, which is a footgun.
- Parity-related fields are zeroed when parity is :attr:`Parity.NONE`.

The COMMTIMEOUTS policy uses the "wait-for-any" mode from §6.3:
``MAXDWORD / MAXDWORD / 1``. This ensures overlapped reads complete as
soon as any bytes are available, matching ``ByteStream.receive(max_bytes)``
semantics.

DCB construction strategy (§6.2.1): the backend reads the driver's current
DCB via ``GetCommState`` then overlays our fields with :func:`apply_config`.
This preserves driver-specific state in reserved/padding bytes while
deterministically owning every documented field.  :func:`build_dcb` is a
test-only convenience that starts from a zeroed DCB — it must not be used
in the backend hot path.
"""

from __future__ import annotations

from ctypes import sizeof
from typing import TYPE_CHECKING

from anyserial._types import ByteSize, Parity, StopBits
from anyserial._windows import _win32 as w
from anyserial.exceptions import UnsupportedConfigurationError

if TYPE_CHECKING:
    from anyserial.config import SerialConfig


_PARITY_TO_WIN32: dict[Parity, int] = {
    Parity.NONE: w.NOPARITY,
    Parity.ODD: w.ODDPARITY,
    Parity.EVEN: w.EVENPARITY,
    Parity.MARK: w.MARKPARITY,
    Parity.SPACE: w.SPACEPARITY,
}

_WIN32_TO_PARITY: dict[int, Parity] = {v: k for k, v in _PARITY_TO_WIN32.items()}

_STOPBITS_TO_WIN32: dict[StopBits, int] = {
    StopBits.ONE: w.ONESTOPBIT,
    StopBits.ONE_POINT_FIVE: w.ONE5STOPBITS,
    StopBits.TWO: w.TWOSTOPBITS,
}

_WIN32_TO_STOPBITS: dict[int, StopBits] = {v: k for k, v in _STOPBITS_TO_WIN32.items()}

_BYTESIZE_TO_WIN32: dict[ByteSize, int] = {
    ByteSize.FIVE: 5,
    ByteSize.SIX: 6,
    ByteSize.SEVEN: 7,
    ByteSize.EIGHT: 8,
}

_WIN32_TO_BYTESIZE: dict[int, ByteSize] = {v: k for k, v in _BYTESIZE_TO_WIN32.items()}


def apply_config(dcb: w.DCB, config: SerialConfig) -> None:
    """Overlay ``config`` onto an existing :class:`DCB` in place.

    The caller is responsible for populating ``dcb`` first — either via
    ``GetCommState`` (backend) or from a zeroed struct (tests). Every
    documented DCB field is set deterministically; reserved/padding bytes
    the caller pre-populated are left untouched.
    """
    dcb.DCBlength = sizeof(w.DCB)
    dcb.BaudRate = config.baudrate
    dcb.ByteSize = _BYTESIZE_TO_WIN32[config.byte_size]
    dcb.Parity = _PARITY_TO_WIN32[config.parity]
    dcb.StopBits = _STOPBITS_TO_WIN32[config.stop_bits]

    # Mandatory invariants.
    dcb.fBinary = 1
    dcb.fAbortOnError = 0
    dcb.fNull = 0
    dcb.fErrorChar = 0
    dcb.fDsrSensitivity = 0
    dcb.fTXContinueOnXoff = 0

    # Parity bit only set when actually checking parity.
    dcb.fParity = 0 if config.parity is Parity.NONE else 1

    # Flow control. xon_xoff, rts_cts, dtr_dsr are independent booleans on
    # SerialConfig.FlowControl (see DESIGN §8.2); each maps to its own DCB
    # bit set so combinations work as a bitwise OR of the underlying bits.
    flow = config.flow_control
    if flow.xon_xoff:
        dcb.fOutX = 1
        dcb.fInX = 1
        dcb.XonChar = bytes([w.XON_CHAR])
        dcb.XoffChar = bytes([w.XOFF_CHAR])
        # Conservative XonLim / XoffLim — the historical pyserial defaults.
        dcb.XonLim = 2048
        dcb.XoffLim = 512
    else:
        dcb.fOutX = 0
        dcb.fInX = 0

    if flow.rts_cts:
        dcb.fOutxCtsFlow = 1
        dcb.fRtsControl = w.RTS_CONTROL_HANDSHAKE
    else:
        dcb.fOutxCtsFlow = 0
        dcb.fRtsControl = w.RTS_CONTROL_ENABLE

    if flow.dtr_dsr:
        dcb.fOutxDsrFlow = 1
        dcb.fDtrControl = w.DTR_CONTROL_HANDSHAKE
    else:
        dcb.fOutxDsrFlow = 0
        dcb.fDtrControl = w.DTR_CONTROL_ENABLE


def build_dcb(config: SerialConfig) -> w.DCB:
    """Return a fully-populated :class:`DCB` for ``config``.

    Starts from a zeroed struct — suitable for tests where no real handle
    is available. The backend must not use this: it should ``GetCommState``
    into a DCB first, then call :func:`apply_config` to preserve
    driver-specific reserved fields (design-windows-backend.md §6.2.1).
    """
    dcb = w.DCB()
    apply_config(dcb, config)
    return dcb


def read_dcb(dcb: w.DCB) -> dict[str, object]:
    """Decode a :class:`DCB` back into a comparable dict.

    Used by :mod:`tests.unit.test_windows_dcb` to assert round-trip parity
    over every flow-control combination — the inverse of :func:`apply_config`,
    not a public API.
    """
    parity = _WIN32_TO_PARITY.get(dcb.Parity)
    if parity is None:  # pragma: no cover — defensive against driver writes
        msg = f"DCB.Parity has unknown value {dcb.Parity!r}"
        raise UnsupportedConfigurationError(msg)
    stop_bits = _WIN32_TO_STOPBITS.get(dcb.StopBits)
    if stop_bits is None:  # pragma: no cover
        msg = f"DCB.StopBits has unknown value {dcb.StopBits!r}"
        raise UnsupportedConfigurationError(msg)
    byte_size = _WIN32_TO_BYTESIZE.get(dcb.ByteSize)
    if byte_size is None:  # pragma: no cover
        msg = f"DCB.ByteSize has unknown value {dcb.ByteSize!r}"
        raise UnsupportedConfigurationError(msg)
    return {
        "baudrate": int(dcb.BaudRate),
        "byte_size": byte_size,
        "parity": parity,
        "stop_bits": stop_bits,
        "xon_xoff": bool(dcb.fOutX) and bool(dcb.fInX),
        "rts_cts": bool(dcb.fOutxCtsFlow) and dcb.fRtsControl == w.RTS_CONTROL_HANDSHAKE,
        "dtr_dsr": bool(dcb.fOutxDsrFlow) and dcb.fDtrControl == w.DTR_CONTROL_HANDSHAKE,
        "f_binary": int(dcb.fBinary),
        "f_abort_on_error": int(dcb.fAbortOnError),
        "f_parity": int(dcb.fParity),
    }


def build_read_any_timeouts() -> w.COMMTIMEOUTS:
    """Return the "wait-for-any" COMMTIMEOUTS policy from §6.3.

    The ``MAXDWORD / MAXDWORD / positive-constant`` triple is a documented
    special case: the read waits up to ``ReadTotalTimeoutConstant`` ms for
    the **first byte**, then returns immediately with whatever bytes are
    available. This matches ``ByteStream.receive(max_bytes)`` semantics.

    Write timeouts remain zero: writes complete when the kernel has
    accepted the bytes, and ``drain()`` uses ``FlushFileBuffers``.
    """
    timeouts = w.COMMTIMEOUTS()
    timeouts.ReadIntervalTimeout = w.MAXDWORD
    timeouts.ReadTotalTimeoutMultiplier = w.MAXDWORD
    timeouts.ReadTotalTimeoutConstant = 1  # ms — tuneable per §6.3
    timeouts.WriteTotalTimeoutMultiplier = 0
    timeouts.WriteTotalTimeoutConstant = 0
    return timeouts


__all__ = [
    "apply_config",
    "build_dcb",
    "build_read_any_timeouts",
    "read_dcb",
]
