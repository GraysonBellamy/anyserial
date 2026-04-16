"""Capability reporting for :class:`LinuxBackend`.

The Linux backend has firm answers for most fields that generic POSIX can
only say "unknown" to: custom baud is always reachable via ``TCSETS2``,
software and hardware flow control have first-class termios bits, break
signalling works through the hardcoded Linux ``TIOCSBRK`` / ``TIOCCBRK``
numbers, the kernel queue-depth ioctls always return sensible values, and
``ASYNC_LOW_LATENCY`` plus the FTDI sysfs ``latency_timer`` are reachable
on every Linux build via :mod:`anyserial._linux.low_latency`.

Mark/space parity is also ``SUPPORTED``: Python's stdlib ``termios`` does
not surface ``CMSPAR``, so :mod:`anyserial._posix.termios_apply` carries
a hardcoded fallback to the kernel-ABI value (``0o10000000000``).

``rs485`` is ``SUPPORTED`` on every Linux kernel the package targets —
``TIOCSRS485`` is reachable via :mod:`anyserial._linux.rs485`. Whether a
specific driver honours the ioctl (most USB-serial adapters do not) is
still runtime-dependent and surfaces through :class:`UnsupportedPolicy`
at apply time.
"""

from __future__ import annotations

from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities


def linux_capabilities() -> SerialCapabilities:
    """Return the capability snapshot every :class:`LinuxBackend` reports."""
    return SerialCapabilities(
        platform="linux",
        backend="linux",
        # TCSETS2 + BOTHER path implemented in _linux/baudrate.py.
        custom_baudrate=Capability.SUPPORTED,
        # Stdlib termios omits CMSPAR; _posix.termios_apply falls back to
        # the hardcoded kernel-ABI value on Linux, so MARK/SPACE both work.
        mark_space_parity=Capability.SUPPORTED,
        # No portable termios bit; skipped at the shared POSIX builder layer.
        one_point_five_stop_bits=Capability.UNSUPPORTED,
        xon_xoff=Capability.SUPPORTED,
        rts_cts=Capability.SUPPORTED,
        # Linux has no termios DTR/DSR flow-control bits; rejected at builder.
        dtr_dsr=Capability.UNSUPPORTED,
        # Whether TIOCMGET responds meaningfully depends on the driver.
        modem_lines=Capability.UNKNOWN,
        # TIOCSBRK/TIOCCBRK reached via the Linux fallback in _posix.ioctl.
        break_signal=Capability.SUPPORTED,
        # flock(LOCK_EX | LOCK_NB) is always available.
        exclusive_access=Capability.SUPPORTED,
        # TIOCSSERIAL + ASYNC_LOW_LATENCY + FTDI sysfs path: reachable on
        # every Linux build via _linux.low_latency. Whether a specific
        # driver honours the ioctl is policy-controlled at apply time.
        low_latency=Capability.SUPPORTED,
        # TIOCSRS485 reachable via _linux.rs485. Whether a specific
        # driver honours the ioctl is policy-controlled at apply time.
        rs485=Capability.SUPPORTED,
        input_waiting=Capability.SUPPORTED,
        output_waiting=Capability.SUPPORTED,
        # Native sysfs walk implemented in _linux/discovery.py.
        port_discovery=Capability.SUPPORTED,
    )


__all__ = [
    "linux_capabilities",
]
