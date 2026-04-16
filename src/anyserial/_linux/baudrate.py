"""Linux-specific custom baud via ``TCGETS2`` / ``TCSETS2`` / ``BOTHER``.

The legacy ``tcgetattr`` / ``tcsetattr`` API encodes baud rate as one of the
``Bxxxx`` bitflags (see :mod:`anyserial._posix.baudrate`). For non-standard
rates the Linux kernel exposes a newer ioctl pair, ``TCGETS2`` / ``TCSETS2``,
which operates on ``struct termios2`` — identical fields to the old struct
plus explicit 32-bit ``c_ispeed`` / ``c_ospeed`` integers and a ``BOTHER``
flag in ``c_cflag``'s CBAUD slot that says "use the ispeed/ospeed values,
not the bit-encoded rate".

Python's stdlib :mod:`termios` does not wrap any of this. The request
codes and the ``struct termios2`` layout are stable Linux kernel ABI
(``<asm-generic/termbits.h>``), so hardcoding them is safe — pySerial has
relied on the same numbers for years.
"""

from __future__ import annotations

import fcntl
import struct
from dataclasses import dataclass
from typing import Any, Final, Self

# Linux ioctl request codes. Computed via the _IOR / _IOW macros with
# type='T', nr=0x2A/0x2B, size=sizeof(struct termios2)=44. These are stable
# kernel ABI on every Linux arch Python supports.
TCGETS2: Final[int] = 0x802C542A
TCSETS2: Final[int] = 0x402C542B

BOTHER: Final[int] = 0o010000
"""CBAUD-slot value meaning "use c_ispeed / c_ospeed from struct termios2"."""

CBAUD: Final[int] = 0o010017
"""Mask covering every cflag bit the baud-rate slot may occupy.

Bits 0-3 carry the low half of the standard-rate encoding; bit 12 (the high
half, used for extended rates up through 4 Mbaud) plus the BOTHER bit at
the same position are OR'd into the mask so the entire slot clears cleanly.
"""

NCCS2: Final[int] = 19
"""``NCCS`` on Linux for ``struct termios2``. Stable kernel ABI."""

_TERMIOS2_STRUCT: Final[struct.Struct] = struct.Struct(f"@IIIIB{NCCS2}sII")
"""Native-aligned ``struct termios2`` packer: 4x tcflag_t, line (u8), cc (19B), 2x speed_t."""

_EXPECTED_SIZE: Final[int] = 44
"""Size of ``struct termios2`` on every Linux build we target. Asserted at import."""

if _TERMIOS2_STRUCT.size != _EXPECTED_SIZE:  # pragma: no cover — defensive
    msg = (
        f"struct termios2 size mismatch: "
        f"Python struct packer says {_TERMIOS2_STRUCT.size}, kernel ABI is {_EXPECTED_SIZE}"
    )
    raise RuntimeError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class Termios2Attrs:
    """Immutable snapshot of the 8-tuple carried in a ``struct termios2``.

    Mirrors :class:`anyserial._posix.termios_apply.TermiosAttrs` but with the
    extra ``line`` discipline byte and with ``ispeed`` / ``ospeed`` typed as
    arbitrary 32-bit rates (not ``B`` bitflags). The shared builders in
    ``_posix.termios_apply`` produce a ``TermiosAttrs``; the Linux backend
    grafts its ``iflag`` / ``oflag`` / ``cflag`` / ``lflag`` / ``cc`` fields
    onto this struct and drops in its own baud integers.
    """

    iflag: int
    oflag: int
    cflag: int
    lflag: int
    line: int
    cc: bytes
    ispeed: int
    ospeed: int

    @classmethod
    def unpack(cls, buf: bytes | bytearray) -> Self:
        """Parse a packed ``struct termios2`` into typed fields."""
        iflag, oflag, cflag, lflag, line, cc, ispeed, ospeed = _TERMIOS2_STRUCT.unpack(buf)
        return cls(
            iflag=int(iflag),
            oflag=int(oflag),
            cflag=int(cflag),
            lflag=int(lflag),
            line=int(line),
            cc=bytes(cc),
            ispeed=int(ispeed),
            ospeed=int(ospeed),
        )

    def pack(self) -> bytes:
        """Serialize back into a ``struct termios2`` payload for ``TCSETS2``."""
        return _TERMIOS2_STRUCT.pack(
            self.iflag,
            self.oflag,
            self.cflag,
            self.lflag,
            self.line,
            self.cc,
            self.ispeed,
            self.ospeed,
        )

    def with_changes(self, **changes: Any) -> Self:
        """Return a copy with the named fields replaced."""
        import dataclasses  # noqa: PLC0415 — local by design

        return dataclasses.replace(self, **changes)


def read_termios2(fd: int) -> Termios2Attrs:
    """Read the current ``struct termios2`` from ``fd`` via ``TCGETS2``."""
    buf = bytearray(_TERMIOS2_STRUCT.size)
    fcntl.ioctl(fd, TCGETS2, buf, True)
    return Termios2Attrs.unpack(buf)


def write_termios2(fd: int, attrs: Termios2Attrs) -> None:
    """Commit ``attrs`` to ``fd`` via ``TCSETS2`` in one atomic kernel call."""
    fcntl.ioctl(fd, TCSETS2, attrs.pack())


def clear_cbaud(cflag: int) -> int:
    """Return ``cflag`` with every CBAUD-slot bit cleared."""
    return cflag & ~CBAUD


def mark_bother(cflag: int) -> int:
    """Return ``cflag`` with the CBAUD slot set to ``BOTHER``.

    Combine with :func:`clear_cbaud` (``mark_bother(clear_cbaud(cflag))``)
    when the incoming cflag already has legacy B-bits set and you want the
    kernel to read ``c_ispeed`` / ``c_ospeed`` instead.
    """
    return clear_cbaud(cflag) | BOTHER


__all__ = [
    "BOTHER",
    "CBAUD",
    "NCCS2",
    "TCGETS2",
    "TCSETS2",
    "Termios2Attrs",
    "clear_cbaud",
    "mark_bother",
    "read_termios2",
    "write_termios2",
]
