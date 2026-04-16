"""Capability reporting for :class:`BsdBackend`.

Per DESIGN §24.3 and §36, the BSD backend is "best-effort" — the three
major variants (FreeBSD, NetBSD, OpenBSD) share enough termios surface
for one backend, but variant-level differences surface at hardware-test
time, which the backend ships without. Capabilities that are only
knowable with a real adapter read ``UNKNOWN`` rather than making a
promise we cannot verify on Linux CI.

Firm answers:

- **Custom baud** is ``SUPPORTED``. The BSDs store ``c_ispeed`` /
  ``c_ospeed`` as integers; :mod:`anyserial._bsd.baudrate` hands the
  rate straight through. Driver-level rejections still surface via
  :class:`UnsupportedConfigurationError`.
- **Break signal** is ``SUPPORTED`` via the ``<sys/ttycom.h>`` numeric
  fallback in :mod:`anyserial._posix.ioctl` — the same values Darwin
  uses, since the BSD family shares the ttycom header.
- **Low latency / RS-485** are ``UNSUPPORTED``. FreeBSD has
  ``TIOCSRS485`` but the implementation is driver-specific and out of
  scope; DESIGN §19.2 routes the request through
  :class:`UnsupportedPolicy`.
- **Port discovery** is ``SUPPORTED`` via the ``/dev`` scan in
  :mod:`anyserial._bsd.discovery`. USB metadata (VID/PID/serial) is
  not populated — use the ``pyserial`` extra for now.

Uncertain-until-hardware:

- **Mark/space parity** → ``UNKNOWN``. Newer FreeBSD exposes
  ``CMSPAR``; NetBSD and OpenBSD do not, and the stdlib
  :mod:`termios` wrapping varies.
- **Modem lines / output waiting** → ``UNKNOWN``. Driver-dependent
  exactly as on Linux.
"""

from __future__ import annotations

from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities


def bsd_capabilities() -> SerialCapabilities:
    """Return the capability snapshot every :class:`BsdBackend` reports."""
    return SerialCapabilities(
        # ``platform="bsd"`` is a deliberate simplification: we dispatch
        # on ``"bsd" in sys.platform`` and ship one backend for every
        # variant, so a single tag here keeps the snapshot comparable
        # across CI runners without papering over variant differences
        # elsewhere in the code. Callers who need the specific variant
        # read :data:`sys.platform` directly.
        platform="bsd",
        backend="bsd",
        # Integer passthrough via _bsd/baudrate.py.
        custom_baudrate=Capability.SUPPORTED,
        # Newer FreeBSD has CMSPAR; older BSDs don't; stdlib may or may
        # not surface it. Caller retries with Parity.MARK / SPACE will
        # raise UnsupportedFeatureError at apply time when absent.
        mark_space_parity=Capability.UNKNOWN,
        # No portable termios bit.
        one_point_five_stop_bits=Capability.UNSUPPORTED,
        xon_xoff=Capability.SUPPORTED,
        # CRTSCTS (FreeBSD) or CCTS_OFLOW | CRTS_IFLOW (older BSDs) —
        # the probe in _posix/termios_apply.py detects either.
        rts_cts=Capability.SUPPORTED,
        # No generic termios bits for DTR/DSR flow control.
        dtr_dsr=Capability.UNSUPPORTED,
        # Whether TIOCMGET responds meaningfully depends on the driver.
        modem_lines=Capability.UNKNOWN,
        # TIOCSBRK/TIOCCBRK reachable via the BSD-family fallback in
        # _posix.ioctl (hardcoded from <sys/ttycom.h>).
        break_signal=Capability.SUPPORTED,
        # flock(LOCK_EX | LOCK_NB) is available on every BSD.
        exclusive_access=Capability.SUPPORTED,
        # DESIGN §18.2: no BSD equivalent for ASYNC_LOW_LATENCY.
        low_latency=Capability.UNSUPPORTED,
        # DESIGN §19.2: RS-485 out of scope for the BSD backend.
        rs485=Capability.UNSUPPORTED,
        input_waiting=Capability.SUPPORTED,
        # TIOCOUTQ exists on every BSD but ICANON behaviour around it
        # varies enough that UNKNOWN is the honest answer until a
        # hardware test confirms it per variant.
        output_waiting=Capability.UNKNOWN,
        # Native /dev scan implemented in _bsd/discovery.py.
        port_discovery=Capability.SUPPORTED,
    )


__all__ = [
    "bsd_capabilities",
]
