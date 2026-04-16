"""Linux kernel-level RS-485 via ``TIOCGRS485`` / ``TIOCSRS485``.

Kernel RS-485 mode has the tty driver drive RTS around each transmitted
frame so the transceiver auto-switches between TX and RX without user
space having to toggle RTS and time it against the UART FIFO drain. The
ioctls and the ``struct serial_rs485`` layout are stable Linux kernel
ABI (``<linux/serial.h>``, ``<asm-generic/ioctls.h>``) but Python's
stdlib does not wrap them.

Most USB-serial adapters reject ``TIOCSRS485`` with ``ENOTTY`` or
``EINVAL`` — the chip lacks the direction-switching circuitry or the
kernel driver simply doesn't implement it. :class:`LinuxBackend` routes
those ``OSError`` values through :class:`UnsupportedPolicy` so callers
pick RAISE / WARN / IGNORE, matching the low-latency path in
:mod:`anyserial._linux.low_latency`.

This module is the narrow primitive: one value type (:class:`RS485State`),
one config-to-state encoder (:func:`from_config`), and one read / one
write function. Everything is sync, has no AnyIO coupling, and lets
``OSError`` escape unchanged.
"""

from __future__ import annotations

import fcntl
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self

if TYPE_CHECKING:
    from anyserial.config import RS485Config


# Stable kernel ABI from <asm-generic/ioctls.h>. Python's stdlib termios
# does not expose either constant.
TIOCGRS485: Final[int] = 0x542E
TIOCSRS485: Final[int] = 0x542F

# Flag bits from <linux/serial.h>. Kernel ABI; not re-exported by Python.
SER_RS485_ENABLED: Final[int] = 1 << 0
SER_RS485_RTS_ON_SEND: Final[int] = 1 << 1
SER_RS485_RTS_AFTER_SEND: Final[int] = 1 << 2
SER_RS485_RX_DURING_TX: Final[int] = 1 << 4
SER_RS485_TERMINATE_BUS: Final[int] = 1 << 5

# struct serial_rs485 layout (<linux/serial.h>, Linux 4.14+):
#
#   __u32 flags;
#   __u32 delay_rts_before_send;   /* milliseconds */
#   __u32 delay_rts_after_send;    /* milliseconds */
#   union {
#       __u32 padding[5];
#       struct {
#           __u8  addr_recv;
#           __u8  addr_dest;
#           __u8  padding0[2];
#           __u32 padding1[4];
#       };
#   };
#
# Total 32 bytes on every Linux arch Python supports. The ``=`` prefix
# pins native byte order with standard sizes and no alignment padding,
# which matches the kernel's layout on all supported arches.
_STRUCT: Final[struct.Struct] = struct.Struct("=IIIBBBBIIII")
_STRUCT_SIZE: Final[int] = 32

# ``u32`` ms caps at ~49 days; config delays are clamped defensively so a
# float overflow at the user boundary can't become a kernel-side wrap.
_DELAY_MS_MAX: Final[int] = 0xFFFF_FFFF


@dataclass(frozen=True, slots=True)
class RS485State:
    """Pure-Python mirror of ``struct serial_rs485``.

    Carries every field we either set or round-trip. ``flags`` holds the
    raw ``SER_RS485_*`` bitmask, delays are in milliseconds, and the
    9-bit-address bytes (``addr_recv`` / ``addr_dest``) are preserved so
    a read-modify-write cycle doesn't clobber them on drivers that use
    the address mode.
    """

    flags: int = 0
    delay_rts_before_send: int = 0
    delay_rts_after_send: int = 0
    addr_recv: int = 0
    addr_dest: int = 0

    def to_bytes(self) -> bytes:
        """Encode to the 32-byte payload the kernel expects."""
        return _STRUCT.pack(
            self.flags,
            self.delay_rts_before_send,
            self.delay_rts_after_send,
            self.addr_recv,
            self.addr_dest,
            0,  # padding0[0]
            0,  # padding0[1]
            0,  # padding1[0]
            0,  # padding1[1]
            0,  # padding1[2]
            0,  # padding1[3]
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> Self:
        """Decode a 32-byte ``struct serial_rs485`` payload.

        Raises :class:`ValueError` on a short buffer; extra trailing
        bytes are ignored so a future kernel struct extension does not
        break the read path.
        """
        if len(payload) < _STRUCT_SIZE:
            msg = f"payload is {len(payload)} bytes; expected at least {_STRUCT_SIZE}"
            raise ValueError(msg)
        flags, before, after, recv, dest, *_ = _STRUCT.unpack_from(payload, 0)
        return cls(
            flags=flags,
            delay_rts_before_send=before,
            delay_rts_after_send=after,
            addr_recv=recv,
            addr_dest=dest,
        )

    @property
    def enabled(self) -> bool:
        """Whether ``SER_RS485_ENABLED`` is set in :attr:`flags`."""
        return bool(self.flags & SER_RS485_ENABLED)

    def with_flags_from(self, config: RS485Config) -> RS485State:
        """Return a copy with only the config-owned flag bits replaced.

        Preserves ``TERMINATE_BUS`` and any future flag bits the kernel
        reported via :func:`read_rs485`, so a driver that advertises bus
        termination via ``/sys/class/.../rs485_supported`` keeps its
        setting through a config apply. Delays and address bytes come
        from the config (delays) and the original state (addresses).
        """
        encoded = from_config(config)
        owned_mask = (
            SER_RS485_ENABLED
            | SER_RS485_RTS_ON_SEND
            | SER_RS485_RTS_AFTER_SEND
            | SER_RS485_RX_DURING_TX
        )
        merged_flags = (self.flags & ~owned_mask) | (encoded.flags & owned_mask)
        return RS485State(
            flags=merged_flags,
            delay_rts_before_send=encoded.delay_rts_before_send,
            delay_rts_after_send=encoded.delay_rts_after_send,
            addr_recv=self.addr_recv,
            addr_dest=self.addr_dest,
        )


def _seconds_to_ms(seconds: float) -> int:
    """Convert seconds (float) to kernel milliseconds (``u32``).

    Config delays are floats so callers can write ``0.001`` for one ms
    without unit-mismatch surprises. The kernel struct stores
    ``delay_rts_*`` as ``__u32`` milliseconds; we clamp to the ``u32``
    range so an oversized delay becomes "max" rather than wrapping.
    """
    if seconds <= 0:
        return 0
    ms = round(seconds * 1000)
    if ms < 0:
        return 0
    if ms > _DELAY_MS_MAX:
        return _DELAY_MS_MAX
    return int(ms)


def from_config(config: RS485Config) -> RS485State:
    """Encode a :class:`RS485Config` into a kernel :class:`RS485State`.

    Only the four flags exposed by :class:`RS485Config` are mapped:
    ``ENABLED`` / ``RTS_ON_SEND`` / ``RTS_AFTER_SEND`` / ``RX_DURING_TX``.
    ``TERMINATE_BUS`` and the 9-bit-address fields are not part of the
    public config and stay zero here; callers that want to preserve
    driver-reported values round-trip via :meth:`RS485State.with_flags_from`.
    """
    flags = 0
    if config.enabled:
        flags |= SER_RS485_ENABLED
    if config.rts_on_send:
        flags |= SER_RS485_RTS_ON_SEND
    if config.rts_after_send:
        flags |= SER_RS485_RTS_AFTER_SEND
    if config.rx_during_tx:
        flags |= SER_RS485_RX_DURING_TX
    return RS485State(
        flags=flags,
        delay_rts_before_send=_seconds_to_ms(config.delay_before_send),
        delay_rts_after_send=_seconds_to_ms(config.delay_after_send),
    )


def read_rs485(fd: int) -> RS485State:
    """Read ``struct serial_rs485`` for ``fd`` via ``TIOCGRS485``.

    Lets :class:`OSError` escape unchanged — ``ENOTTY`` on drivers that
    don't implement the ioctl, ``EINVAL`` on adapters that recognise it
    but refuse. The orchestrator decides whether to honour
    :class:`UnsupportedPolicy`.
    """
    payload = fcntl.ioctl(fd, TIOCGRS485, b"\x00" * _STRUCT_SIZE)
    return RS485State.from_bytes(payload)


def write_rs485(fd: int, state: RS485State) -> None:
    """Write ``struct serial_rs485`` for ``fd`` via ``TIOCSRS485``.

    Same error contract as :func:`read_rs485`: ``OSError`` escapes, the
    caller owns the policy decision.
    """
    fcntl.ioctl(fd, TIOCSRS485, state.to_bytes())


__all__ = [
    "SER_RS485_ENABLED",
    "SER_RS485_RTS_AFTER_SEND",
    "SER_RS485_RTS_ON_SEND",
    "SER_RS485_RX_DURING_TX",
    "SER_RS485_TERMINATE_BUS",
    "TIOCGRS485",
    "TIOCSRS485",
    "RS485State",
    "from_config",
    "read_rs485",
    "write_rs485",
]
