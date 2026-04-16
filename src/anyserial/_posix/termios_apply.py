"""Pure termios builders shared by every POSIX backend.

Each :func:`apply_*` takes an immutable :class:`TermiosAttrs` snapshot and
returns a new snapshot with the requested change baked in. No I/O, no hardware,
no AnyIO — the functions are plain synchronous transformations and are fully
unit-testable on any POSIX host.

The backend orchestrator (DESIGN §16) reads the current attrs with
:func:`termios.tcgetattr`, threads them through these builders, then commits
the result atomically with :func:`termios.tcsetattr`. Capability-driven
decisions (which features to request, which to skip) live at the orchestrator
layer; the builders here only know how to translate a request into flag bits,
and raise :class:`UnsupportedFeatureError` when the running platform's
:mod:`termios` module lacks a required constant (for example, ``CMSPAR`` on
macOS, or separate ``CCTS_OFLOW``/``CRTS_IFLOW`` where ``CRTSCTS`` is absent).
"""

from __future__ import annotations

import dataclasses
import sys
import termios
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self

from anyserial._types import ByteSize, Parity, StopBits
from anyserial.exceptions import UnsupportedFeatureError

if TYPE_CHECKING:
    from anyserial.config import FlowControl

type CC = tuple[bytes | int, ...]
"""Control-characters tuple — an immutable form of ``termios.tcgetattr()[6]``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TermiosAttrs:
    """Immutable snapshot of the 7-tuple returned by :func:`termios.tcgetattr`.

    Use :meth:`from_list` to convert a stdlib result into this form and
    :meth:`to_list` to hand one back to :func:`termios.tcsetattr`. The
    ``apply_*`` functions in this module return new instances via
    :func:`dataclasses.replace`.
    """

    iflag: int
    oflag: int
    cflag: int
    lflag: int
    ispeed: int
    ospeed: int
    cc: CC

    @classmethod
    def from_list(cls, attrs: list[Any]) -> Self:
        """Wrap the 7-element list that :func:`termios.tcgetattr` returns."""
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        return cls(
            iflag=int(iflag),
            oflag=int(oflag),
            cflag=int(cflag),
            lflag=int(lflag),
            ispeed=int(ispeed),
            ospeed=int(ospeed),
            cc=tuple(cc),
        )

    def to_list(self) -> list[Any]:
        """Convert back to the mutable 7-list :func:`termios.tcsetattr` expects."""
        return [
            self.iflag,
            self.oflag,
            self.cflag,
            self.lflag,
            self.ispeed,
            self.ospeed,
            list(self.cc),
        ]

    def with_changes(self, **changes: Any) -> Self:
        """Return a copy with the named fields replaced."""
        return dataclasses.replace(self, **changes)


# ---------------------------------------------------------------------------
# Platform-feature probes
# ---------------------------------------------------------------------------

# CMSPAR is the Linux/BSD "mark-or-space parity" cflag bit. macOS does not
# define it; `getattr(..., 0)` lets the helpers detect that at import time.
# Python's stdlib `termios` does not expose CMSPAR even on Linux, where the
# kernel ABI guarantees the bit at 0o10000000000 (see <bits/termios.h>).
# Falling back to the hardcoded value lets MARK/SPACE parity work on every
# Linux build without requiring a CPython-side change.
_LINUX_CMSPAR: Final[int] = 0o10000000000
_CMSPAR: int = int(
    getattr(termios, "CMSPAR", 0) or (_LINUX_CMSPAR if sys.platform.startswith("linux") else 0)
)


def _rts_cts_mask() -> int:
    """Return the cflag bitmask that turns on hardware RTS/CTS, or 0 if absent.

    Linux and newer BSDs expose a single ``CRTSCTS`` constant. macOS and older
    BSDs split it into ``CCTS_OFLOW`` (output-gated-by-CTS) and ``CRTS_IFLOW``
    (input-gated-by-RTS); both bits must be set together. Returning 0 signals
    that the current platform's termios lacks any form of hardware handshake.
    """
    crtscts = int(getattr(termios, "CRTSCTS", 0))
    if crtscts:
        return crtscts
    ccts_oflow = int(getattr(termios, "CCTS_OFLOW", 0))
    crts_iflow = int(getattr(termios, "CRTS_IFLOW", 0))
    if ccts_oflow and crts_iflow:
        return ccts_oflow | crts_iflow
    return 0


_RTS_CTS_MASK: int = _rts_cts_mask()


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def apply_raw_mode(attrs: TermiosAttrs) -> TermiosAttrs:
    """Put ``attrs`` into serial-raw mode.

    Equivalent to :func:`termios.cfmakeraw` plus the two cflag bits every
    serial backend needs: ``CREAD`` (enable the receiver) and ``CLOCAL``
    (ignore modem-status lines so ``open`` does not block waiting for DCD).
    Clears canonical processing, echo, signal characters, input mapping,
    output post-processing, and 8-bit stripping; sets ``VMIN=1`` and
    ``VTIME=0`` so the kernel returns as soon as one byte is available.

    Call :func:`apply_byte_size` afterwards to override the default ``CS8``
    if the caller wants a different character size.
    """
    iflag_clear = (
        termios.IGNBRK
        | termios.BRKINT
        | termios.PARMRK
        | termios.ISTRIP
        | termios.INLCR
        | termios.IGNCR
        | termios.ICRNL
        | termios.IXON
    )
    lflag_clear = termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
    cflag_clear = termios.CSIZE | termios.PARENB
    cflag_set = termios.CS8 | termios.CREAD | termios.CLOCAL

    cc = list(attrs.cc)
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0

    return attrs.with_changes(
        iflag=attrs.iflag & ~iflag_clear,
        oflag=attrs.oflag & ~termios.OPOST,
        lflag=attrs.lflag & ~lflag_clear,
        cflag=(attrs.cflag & ~cflag_clear) | cflag_set,
        cc=tuple(cc),
    )


_BYTE_SIZE_BITS: dict[ByteSize, int] = {
    ByteSize.FIVE: termios.CS5,
    ByteSize.SIX: termios.CS6,
    ByteSize.SEVEN: termios.CS7,
    ByteSize.EIGHT: termios.CS8,
}


def apply_byte_size(attrs: TermiosAttrs, byte_size: ByteSize) -> TermiosAttrs:
    """Set the number of data bits per character by masking ``CSIZE``."""
    return attrs.with_changes(
        cflag=(attrs.cflag & ~termios.CSIZE) | _BYTE_SIZE_BITS[byte_size],
    )


def apply_parity(attrs: TermiosAttrs, parity: Parity) -> TermiosAttrs:
    """Set the parity mode.

    Clears the parity-related cflag bits unconditionally, then sets the
    combination for the requested mode. ``MARK`` and ``SPACE`` rely on the
    ``CMSPAR`` extension, which is absent on macOS — the call raises
    :class:`UnsupportedFeatureError` in that case.

    Raises:
        UnsupportedFeatureError: ``parity`` is ``MARK`` or ``SPACE`` and the
            platform's :mod:`termios` does not define ``CMSPAR``.
    """
    cflag = attrs.cflag & ~(termios.PARENB | termios.PARODD)
    if _CMSPAR:
        cflag &= ~_CMSPAR

    match parity:
        case Parity.NONE:
            pass
        case Parity.EVEN:
            cflag |= termios.PARENB
        case Parity.ODD:
            cflag |= termios.PARENB | termios.PARODD
        case Parity.MARK:
            if not _CMSPAR:
                msg = "MARK parity requires termios.CMSPAR, which is not defined on this platform"
                raise UnsupportedFeatureError(msg)
            cflag |= termios.PARENB | termios.PARODD | _CMSPAR
        case Parity.SPACE:
            if not _CMSPAR:
                msg = "SPACE parity requires termios.CMSPAR, which is not defined on this platform"
                raise UnsupportedFeatureError(msg)
            cflag |= termios.PARENB | _CMSPAR

    return attrs.with_changes(cflag=cflag)


def apply_stop_bits(attrs: TermiosAttrs, stop_bits: StopBits) -> TermiosAttrs:
    """Set the stop-bit count via the ``CSTOPB`` cflag bit.

    ``ONE_POINT_FIVE`` has no dedicated termios flag. A few drivers produce
    1.5 stop bits when ``CSTOPB`` is set alongside ``CS5``, but the behaviour
    is too driver-specific to encode at the shared POSIX layer; backends that
    know the trick works for their device opt in themselves.

    Raises:
        UnsupportedFeatureError: ``stop_bits`` is ``ONE_POINT_FIVE``.
    """
    match stop_bits:
        case StopBits.ONE:
            return attrs.with_changes(cflag=attrs.cflag & ~termios.CSTOPB)
        case StopBits.TWO:
            return attrs.with_changes(cflag=attrs.cflag | termios.CSTOPB)
        case StopBits.ONE_POINT_FIVE:
            msg = "1.5 stop bits is not representable in standard POSIX termios"
            raise UnsupportedFeatureError(msg)


def apply_flow_control(attrs: TermiosAttrs, flow: FlowControl) -> TermiosAttrs:
    """Apply XON/XOFF and hardware RTS/CTS flow control.

    XON/XOFF toggles the ``IXON`` and ``IXOFF`` iflag bits together (software
    flow control is symmetric). ``IXANY`` is always cleared — resuming output
    on any received character is rarely what a binary protocol wants and is
    trivial for callers to re-enable via an extra builder if needed.

    RTS/CTS toggles whichever cflag bits the platform exposes
    (``CRTSCTS`` on Linux / newer BSDs, ``CCTS_OFLOW | CRTS_IFLOW`` on macOS
    and older BSDs). DTR/DSR is not configurable via generic termios; backends
    that support it apply platform-specific bits themselves.

    Raises:
        UnsupportedFeatureError: ``flow.rts_cts`` is set but the platform
            exposes no hardware-handshake bits, or ``flow.dtr_dsr`` is set
            (always unsupported at this layer).
    """
    iflag = attrs.iflag & ~(termios.IXON | termios.IXOFF | termios.IXANY)
    if flow.xon_xoff:
        iflag |= termios.IXON | termios.IXOFF

    cflag = attrs.cflag & ~_RTS_CTS_MASK if _RTS_CTS_MASK else attrs.cflag
    if flow.rts_cts:
        if not _RTS_CTS_MASK:
            msg = "RTS/CTS flow control is not available on this platform's termios"
            raise UnsupportedFeatureError(msg)
        cflag |= _RTS_CTS_MASK

    if flow.dtr_dsr:
        msg = "DTR/DSR flow control is not configurable via generic POSIX termios"
        raise UnsupportedFeatureError(msg)

    return attrs.with_changes(iflag=iflag, cflag=cflag)


def apply_hangup(attrs: TermiosAttrs, hangup_on_close: bool) -> TermiosAttrs:
    """Toggle ``HUPCL`` — whether the kernel drops DTR/RTS when the fd closes."""
    if hangup_on_close:
        return attrs.with_changes(cflag=attrs.cflag | termios.HUPCL)
    return attrs.with_changes(cflag=attrs.cflag & ~termios.HUPCL)


__all__ = [
    "CC",
    "TermiosAttrs",
    "apply_byte_size",
    "apply_flow_control",
    "apply_hangup",
    "apply_parity",
    "apply_raw_mode",
    "apply_stop_bits",
]
