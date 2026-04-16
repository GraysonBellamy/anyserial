"""BSD-specific custom baud via direct integer ispeed/ospeed.

The BSDs (FreeBSD, NetBSD, OpenBSD, DragonFly) all share a ``struct
termios`` layout in which ``c_ispeed`` and ``c_ospeed`` are plain
integer fields, not ``B*`` bitflag encodings. Their ``tcsetattr``
accepts arbitrary rates directly — no dedicated ioctl (like Linux's
``TCSETS2``) or framework override (like Darwin's ``IOSSIOSPEED``) is
required.

The ``Bxxxx`` constants still exist for source compatibility with legacy
code, but on the BSDs they are simply the integer rate values (i.e.
``B9600 == 9600`` on FreeBSD 12+, NetBSD 9+, OpenBSD 6.8+). Standard
rates therefore take the inherited ``PosixBackend`` path through
:func:`anyserial._posix.baudrate.baudrate_to_speed`; non-standard rates
get the same termios builder pipeline with the integer rate dropped
directly into ``ispeed`` / ``ospeed``.

Whether a specific driver honours a specific custom rate is hardware-
dependent (§36 risk register: "Subtle differences across BSD variants").
The capability is advertised as ``SUPPORTED`` because the kernel accepts
any integer; driver rejections surface as
:class:`UnsupportedConfigurationError` via the usual errno mapping.
"""

from __future__ import annotations


def passthrough_rate(rate: int) -> int:
    """Return ``rate`` unchanged.

    Exists as a named helper so the backend's custom-baud path reads
    symmetrically to :func:`anyserial._linux.baudrate.mark_bother` and
    :func:`anyserial._darwin.baudrate.set_iossiospeed`. The function
    also serves as the documentation anchor for the "BSD takes the rate
    verbatim" contract.
    """
    return rate


__all__ = [
    "passthrough_rate",
]
