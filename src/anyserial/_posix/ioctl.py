"""Shared ioctl helpers for every POSIX serial backend.

Each helper wraps a single tty ioctl (or termios call) in a narrow sync
function that takes a raw fd and returns a plain Python value. No class
state, no AnyIO coupling, no capability checks — backends consult their
:class:`SerialCapabilities` first and then call these helpers, which simply
let :class:`OSError` escape for the backend to map via
:func:`errno_to_exception`.

DESIGN §25.1 enumerates the ioctls every ``SyncSerialBackend`` exposes:
``TIOCINQ``/``TIOCOUTQ`` for queue depth, ``TIOCMGET``/``TIOCMBIS``/``TIOCMBIC``
for modem and control lines, ``TIOCSBRK``/``TIOCCBRK`` for break assert, and
:func:`termios.tcflush` for the buffer resets.

A note on constant availability: Python's stdlib :mod:`termios` does not
expose every constant the kernel defines. ``TIOCSBRK`` and ``TIOCCBRK`` are
the notable gap — Linux hides them despite the kernel providing them
(``<asm/ioctls.h>``), and Darwin / the BSDs likewise omit them from
Python's termios module even though ``<sys/ttycom.h>`` defines them. We
fall back to the well-known numeric values on each platform family.
Requests on an unrecognized platform still raise
:class:`UnsupportedFeatureError` at call time.
"""

from __future__ import annotations

import fcntl
import struct
import sys
import termios
from typing import Final

from anyserial._types import ControlLines, ModemLines
from anyserial.exceptions import UnsupportedFeatureError

_INT = struct.Struct("i")
"""Native-endian signed 32-bit int — the payload for every ioctl in this module."""


# ---------------------------------------------------------------------------
# Request-code probes (evaluated once at import)
# ---------------------------------------------------------------------------

# TIOCINQ is the tty-specific name; FIONREAD is the generic "bytes readable"
# ioctl. They're the same number on Linux but only one of the two names may be
# surfaced by Python's termios module.
_TIOCINQ: Final[int | None] = getattr(termios, "TIOCINQ", None) or getattr(
    termios,
    "FIONREAD",
    None,
)
_TIOCOUTQ: Final[int | None] = getattr(termios, "TIOCOUTQ", None)

_TIOCMGET: Final[int | None] = getattr(termios, "TIOCMGET", None)
_TIOCMBIS: Final[int | None] = getattr(termios, "TIOCMBIS", None)
_TIOCMBIC: Final[int | None] = getattr(termios, "TIOCMBIC", None)

# TIOCSBRK / TIOCCBRK: stable kernel ABI on every POSIX we target, but
# Python's termios module does not expose either name. Fall back to the
# well-known numeric values per platform family.
#
# Linux values from <asm/ioctls.h>:
_LINUX_TIOCSBRK: Final[int] = 0x5427
_LINUX_TIOCCBRK: Final[int] = 0x5428
# Darwin / FreeBSD / NetBSD / OpenBSD / DragonFly share <sys/ttycom.h>:
#   #define TIOCSBRK _IO('t', 123)
#   #define TIOCCBRK _IO('t', 122)
# ``_IO(g, n)`` expands to ``IOC_VOID (0x20000000) | (g << 8) | n``, so:
_BSD_TIOCSBRK: Final[int] = 0x2000747B
_BSD_TIOCCBRK: Final[int] = 0x2000747A

# Routed through Final bools so mypy doesn't narrow the platform branches
# below and flag unreachable fallthroughs on any single-platform CI run.
_IS_LINUX: Final[bool] = sys.platform.startswith("linux")
_IS_BSD_FAMILY: Final[bool] = sys.platform == "darwin" or "bsd" in sys.platform


def _break_request(*, on: bool) -> int | None:
    """Return the ioctl request code for setting/clearing break, or None."""
    name = "TIOCSBRK" if on else "TIOCCBRK"
    req = getattr(termios, name, None)
    if req is not None:
        return int(req)
    if _IS_LINUX:
        return _LINUX_TIOCSBRK if on else _LINUX_TIOCCBRK
    if _IS_BSD_FAMILY:
        return _BSD_TIOCSBRK if on else _BSD_TIOCCBRK
    return None


# Control-line bit masks. Each ``TIOCM_*`` constant is a flag in the int
# returned by TIOCMGET / consumed by TIOCMBIS / TIOCMBIC.
_TIOCM_RTS: Final[int] = int(getattr(termios, "TIOCM_RTS", 0))
_TIOCM_DTR: Final[int] = int(getattr(termios, "TIOCM_DTR", 0))
_TIOCM_CTS: Final[int] = int(getattr(termios, "TIOCM_CTS", 0))
_TIOCM_DSR: Final[int] = int(getattr(termios, "TIOCM_DSR", 0))
_TIOCM_RI: Final[int] = int(getattr(termios, "TIOCM_RI", 0))
# TIOCM_CAR is the POSIX name; TIOCM_CD is an alias some platforms use.
_TIOCM_CD: Final[int] = int(
    getattr(termios, "TIOCM_CAR", 0) or getattr(termios, "TIOCM_CD", 0),
)


def _require(req: int | None, name: str) -> int:
    """Return ``req`` or raise :class:`UnsupportedFeatureError` if it is ``None``."""
    if req is None:
        msg = f"{name} is not available on this platform's termios module"
        raise UnsupportedFeatureError(msg)
    return req


# ---------------------------------------------------------------------------
# Queue-depth helpers
# ---------------------------------------------------------------------------


def input_waiting(fd: int) -> int:
    """Return the number of bytes in the kernel's input queue.

    Wraps ``TIOCINQ`` (aka ``FIONREAD``). Used by
    :meth:`SerialPort.input_waiting` and by ``receive_available`` to drain
    the kernel queue in a single ``os.read``.
    """
    req = _require(_TIOCINQ, "TIOCINQ/FIONREAD")
    out = fcntl.ioctl(fd, req, _INT.pack(0))
    return int(_INT.unpack(out)[0])


def output_waiting(fd: int) -> int:
    """Return the number of bytes pending in the kernel's output queue.

    Wraps ``TIOCOUTQ``. Used by :meth:`SerialPort.output_waiting` and by the
    async :meth:`drain` implementation, which polls this to avoid blocking
    on :func:`termios.tcdrain`.
    """
    req = _require(_TIOCOUTQ, "TIOCOUTQ")
    out = fcntl.ioctl(fd, req, _INT.pack(0))
    return int(_INT.unpack(out)[0])


# ---------------------------------------------------------------------------
# Modem / control line helpers
# ---------------------------------------------------------------------------


def _read_tiocm_bits(fd: int) -> int:
    """Return the raw int bitmask from ``TIOCMGET``."""
    req = _require(_TIOCMGET, "TIOCMGET")
    out = fcntl.ioctl(fd, req, _INT.pack(0))
    return int(_INT.unpack(out)[0])


def get_modem_lines(fd: int) -> ModemLines:
    """Read the CTS/DSR/RI/CD input lines via ``TIOCMGET``."""
    bits = _read_tiocm_bits(fd)
    return ModemLines(
        cts=bool(bits & _TIOCM_CTS),
        dsr=bool(bits & _TIOCM_DSR),
        ri=bool(bits & _TIOCM_RI),
        cd=bool(bits & _TIOCM_CD),
    )


def get_control_lines(fd: int) -> ControlLines:
    """Read the current RTS/DTR output levels via ``TIOCMGET``.

    Useful for round-tripping after :func:`set_control_lines` and for
    capability probes — the kernel will report the currently-asserted
    levels on ports that support line control.
    """
    bits = _read_tiocm_bits(fd)
    return ControlLines(
        rts=bool(bits & _TIOCM_RTS),
        dtr=bool(bits & _TIOCM_DTR),
    )


def set_control_lines(
    fd: int,
    *,
    rts: bool | None = None,
    dtr: bool | None = None,
) -> None:
    """Set RTS / DTR output lines.

    ``None`` means "leave unchanged." When both arguments are ``None`` the
    call is a no-op — no syscalls, no capability probing. Non-None values
    are applied via ``TIOCMBIS`` (bits to set) and ``TIOCMBIC`` (bits to
    clear); the two ioctls happen as separate syscalls, but each is a
    microsecond-scale kernel operation.
    """
    set_mask = 0
    clear_mask = 0
    if rts is True:
        set_mask |= _TIOCM_RTS
    elif rts is False:
        clear_mask |= _TIOCM_RTS
    if dtr is True:
        set_mask |= _TIOCM_DTR
    elif dtr is False:
        clear_mask |= _TIOCM_DTR

    if not (set_mask or clear_mask):
        return

    if set_mask:
        req = _require(_TIOCMBIS, "TIOCMBIS")
        fcntl.ioctl(fd, req, _INT.pack(set_mask))
    if clear_mask:
        req = _require(_TIOCMBIC, "TIOCMBIC")
        fcntl.ioctl(fd, req, _INT.pack(clear_mask))


# ---------------------------------------------------------------------------
# Break signalling
# ---------------------------------------------------------------------------


def set_break(fd: int, *, on: bool) -> None:
    """Assert (``on=True``) or de-assert (``on=False``) the break condition.

    The ioctl is instantaneous; callers own the sleep between assert and
    de-assert. This pairs with :meth:`SerialPort.send_break`, which layers
    a cancellable :func:`anyio.sleep` between the two calls and reclaims
    the de-assert in a ``finally`` block if the coroutine is cancelled
    mid-break.
    """
    req = _require(_break_request(on=on), "TIOCSBRK" if on else "TIOCCBRK")
    fcntl.ioctl(fd, req, 0)


# ---------------------------------------------------------------------------
# Buffer flush
# ---------------------------------------------------------------------------


def reset_input_buffer(fd: int) -> None:
    """Discard all pending input bytes via ``tcflush(TCIFLUSH)``."""
    termios.tcflush(fd, termios.TCIFLUSH)


def reset_output_buffer(fd: int) -> None:
    """Discard all pending output bytes via ``tcflush(TCOFLUSH)``."""
    termios.tcflush(fd, termios.TCOFLUSH)


__all__ = [
    "get_control_lines",
    "get_modem_lines",
    "input_waiting",
    "output_waiting",
    "reset_input_buffer",
    "reset_output_buffer",
    "set_break",
    "set_control_lines",
]
