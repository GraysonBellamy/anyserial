"""Capability reporting for :class:`DarwinBackend`.

Darwin's termios differs from Linux in several important ways that drive
the tri-state values here:

- **Custom baud** is ``SUPPORTED`` via :data:`IOSSIOSPEED`
  (:mod:`anyserial._darwin.baudrate`); unlike Linux's ``TCSETS2`` path
  the ioctl is also reachable on every Darwin build since the IOKit
  SerialFamily API has been stable for over a decade.
- **Mark/space parity** is ``UNSUPPORTED`` — Darwin never shipped the
  ``CMSPAR`` cflag bit. :mod:`anyserial._posix.termios_apply` already
  raises :class:`UnsupportedFeatureError` in that case; the capability
  just surfaces the truth up front.
- **Break signal** is ``SUPPORTED`` via the ``<sys/ttycom.h>`` numeric
  fallback carried in :mod:`anyserial._posix.ioctl` (Python's stdlib
  termios omits ``TIOCSBRK`` / ``TIOCCBRK`` on Darwin too).
- **Low latency** and **RS-485** are ``UNSUPPORTED`` — no Darwin
  equivalent for ``ASYNC_LOW_LATENCY`` / ``TIOCSRS485``. DESIGN §18.2 /
  §19.2 route the request through :class:`UnsupportedPolicy`.
- **Port discovery** is ``SUPPORTED`` — native IOKit enumeration lives
  in :mod:`anyserial._darwin.discovery` and mirrors what the Linux sysfs
  walker reports (``/dev/cu.*`` paths with USB VID / PID / serial /
  manufacturer / product metadata where applicable).
"""

from __future__ import annotations

from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities


def darwin_capabilities() -> SerialCapabilities:
    """Return the capability snapshot every :class:`DarwinBackend` reports."""
    return SerialCapabilities(
        platform="darwin",
        backend="darwin",
        # IOSSIOSPEED path implemented in _darwin/baudrate.py.
        custom_baudrate=Capability.SUPPORTED,
        # Darwin has never defined CMSPAR; _posix.termios_apply already
        # raises UnsupportedFeatureError for MARK / SPACE on this platform.
        mark_space_parity=Capability.UNSUPPORTED,
        # No portable termios bit; skipped at the shared POSIX builder layer.
        one_point_five_stop_bits=Capability.UNSUPPORTED,
        xon_xoff=Capability.SUPPORTED,
        # Darwin uses CCTS_OFLOW | CRTS_IFLOW, not CRTSCTS. The builder
        # in _posix.termios_apply probes both spellings at import time.
        rts_cts=Capability.SUPPORTED,
        # No generic termios bits for DTR/DSR flow control.
        dtr_dsr=Capability.UNSUPPORTED,
        # TIOCMGET is available but honouring it depends on the driver.
        modem_lines=Capability.UNKNOWN,
        # TIOCSBRK/TIOCCBRK reachable via the BSD-family fallback in
        # _posix.ioctl (hardcoded from <sys/ttycom.h>).
        break_signal=Capability.SUPPORTED,
        # flock(LOCK_EX | LOCK_NB) is available on Darwin.
        exclusive_access=Capability.SUPPORTED,
        # No Darwin equivalent to ASYNC_LOW_LATENCY; DESIGN §18.2 routes
        # the request through UnsupportedPolicy at apply time.
        low_latency=Capability.UNSUPPORTED,
        # No Darwin equivalent to TIOCSRS485; DESIGN §19.2 same policy.
        rs485=Capability.UNSUPPORTED,
        input_waiting=Capability.SUPPORTED,
        output_waiting=Capability.SUPPORTED,
        # Native IOKit enumeration implemented in _darwin/discovery.py.
        port_discovery=Capability.SUPPORTED,
    )


__all__ = [
    "darwin_capabilities",
]
