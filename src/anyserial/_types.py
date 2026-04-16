"""Core enums and small data types used across the public API.

These have no runtime dependencies on any other ``anyserial`` module and are
safe to import from anywhere in the package.
"""

from __future__ import annotations

from collections.abc import Buffer
from dataclasses import dataclass
from enum import StrEnum

type BytesLike = Buffer
"""Any object exposing the :pep:`688` buffer protocol.

Accepts ``bytes``, ``bytearray``, ``memoryview``, ``array.array``, and NumPy
arrays that expose the buffer protocol. Used on zero-copy write paths.
"""


class ByteSize(StrEnum):
    """Number of data bits per character."""

    FIVE = "5"
    SIX = "6"
    SEVEN = "7"
    EIGHT = "8"


class Parity(StrEnum):
    """Parity check mode."""

    NONE = "none"
    ODD = "odd"
    EVEN = "even"
    MARK = "mark"
    SPACE = "space"


class StopBits(StrEnum):
    """Number of stop bits between characters."""

    ONE = "1"
    ONE_POINT_FIVE = "1.5"
    TWO = "2"


class Capability(StrEnum):
    """Tri-state describing whether a feature is available.

    - ``SUPPORTED``: the stack definitely supports the feature on this platform
      and backend combination.
    - ``UNSUPPORTED``: the stack definitely does not support it.
    - ``UNKNOWN``: the platform can advertise the feature, but whether a
      specific driver or device accepts it is only knowable at operation time.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class UnsupportedPolicy(StrEnum):
    """How the backend should respond to an optional unsupported feature.

    Applies only to *optional* features (e.g., ``low_latency=True`` on a
    kernel without the relevant ioctl). Core configuration errors (invalid
    baud, impossible flow-control combination) always raise regardless.
    """

    RAISE = "raise"
    WARN = "warn"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModemLines:
    """Snapshot of the input modem-status lines.

    Attributes:
        cts: Clear To Send.
        dsr: Data Set Ready.
        ri: Ring Indicator.
        cd: Carrier Detect.
    """

    cts: bool
    dsr: bool
    ri: bool
    cd: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlLines:
    """Snapshot of the output control lines driven by the host.

    Attributes:
        rts: Request To Send.
        dtr: Data Terminal Ready.
    """

    rts: bool
    dtr: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class CommEvent:
    """Result of a ``WaitCommEvent`` completion.

    Reports which modem-line transitions and error conditions were detected
    since the last call. Each field is ``True`` when the corresponding event
    fired. Windows-only; see ``design-windows-backend.md`` §6.4.

    Attributes:
        cts_changed: CTS line changed state.
        dsr_changed: DSR line changed state.
        rlsd_changed: RLSD (carrier detect) line changed state.
        ring: Ring indicator was detected.
        error: A framing, overrun, or parity error occurred.
        break_received: A break condition was received.
    """

    cts_changed: bool = False
    dsr_changed: bool = False
    rlsd_changed: bool = False
    ring: bool = False
    error: bool = False
    break_received: bool = False
