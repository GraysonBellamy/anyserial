"""Unit tests for :func:`bsd_capabilities`.

Platform-agnostic — the snapshot is a pure constructor with no imports
that care about the running OS, so Linux CI exercises the same values a
real BSD runner would. The capability surface is best-effort per
DESIGN §36 (variant-level differences surface at hardware-test time),
and the test mix reflects that: firm ``SUPPORTED``/``UNSUPPORTED``
assertions for the bits we can stand behind, ``UNKNOWN`` assertions for
the bits we explicitly decline to claim.
"""

from __future__ import annotations

from anyserial._bsd.capabilities import bsd_capabilities
from anyserial._types import Capability


class TestBsdCapabilities:
    def test_platform_and_backend_tags(self) -> None:
        caps = bsd_capabilities()
        assert caps.platform == "bsd"
        assert caps.backend == "bsd"

    def test_custom_baudrate_is_supported(self) -> None:
        # BSD stores c_ispeed / c_ospeed as integers; tcsetattr accepts
        # arbitrary rates directly.
        assert bsd_capabilities().custom_baudrate is Capability.SUPPORTED

    def test_mark_space_parity_is_unknown(self) -> None:
        # Newer FreeBSD exposes CMSPAR; older BSDs don't; stdlib may
        # or may not wrap it. UNKNOWN is the honest answer.
        assert bsd_capabilities().mark_space_parity is Capability.UNKNOWN

    def test_low_latency_and_rs485_are_unsupported(self) -> None:
        caps = bsd_capabilities()
        # No BSD equivalent to ASYNC_LOW_LATENCY; kernel RS-485 is out
        # of scope for the BSD backend per DESIGN §19.2.
        assert caps.low_latency is Capability.UNSUPPORTED
        assert caps.rs485 is Capability.UNSUPPORTED

    def test_break_signal_is_supported(self) -> None:
        # Shared _posix/ioctl.py carries the BSD-family numeric fallback
        # for TIOCSBRK / TIOCCBRK — BSDs share <sys/ttycom.h>.
        assert bsd_capabilities().break_signal is Capability.SUPPORTED

    def test_rts_cts_is_supported(self) -> None:
        # _posix/termios_apply.py detects CRTSCTS (FreeBSD) or
        # CCTS_OFLOW | CRTS_IFLOW (older BSDs).
        assert bsd_capabilities().rts_cts is Capability.SUPPORTED

    def test_port_discovery_is_supported(self) -> None:
        # Native /dev-scan enumerator implemented in _bsd/discovery.py.
        assert bsd_capabilities().port_discovery is Capability.SUPPORTED

    def test_output_waiting_is_unknown(self) -> None:
        # Marked UNKNOWN pending hardware validation (§36 best-effort).
        assert bsd_capabilities().output_waiting is Capability.UNKNOWN

    def test_input_waiting_is_supported(self) -> None:
        assert bsd_capabilities().input_waiting is Capability.SUPPORTED

    def test_exclusive_access_is_supported(self) -> None:
        # flock(LOCK_EX | LOCK_NB) is POSIX-portable across BSDs.
        assert bsd_capabilities().exclusive_access is Capability.SUPPORTED
