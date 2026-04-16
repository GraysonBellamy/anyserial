"""Unit tests for :func:`darwin_capabilities`.

Platform-agnostic — the function is a pure constructor with no imports
that care about the running OS, so Linux CI exercises the same values
that a Darwin runner would.
"""

from __future__ import annotations

from anyserial._darwin.capabilities import darwin_capabilities
from anyserial._types import Capability


class TestDarwinCapabilities:
    def test_platform_and_backend_tags(self) -> None:
        caps = darwin_capabilities()
        assert caps.platform == "darwin"
        assert caps.backend == "darwin"

    def test_custom_baudrate_is_supported(self) -> None:
        # IOSSIOSPEED is available on every Darwin build since the IOKit
        # SerialFamily shipped; capability must reflect that.
        assert darwin_capabilities().custom_baudrate is Capability.SUPPORTED

    def test_mark_space_parity_is_unsupported(self) -> None:
        # Darwin never defined CMSPAR; raising at apply time is honest,
        # but the capability surface should warn callers ahead of time.
        assert darwin_capabilities().mark_space_parity is Capability.UNSUPPORTED

    def test_low_latency_and_rs485_are_unsupported(self) -> None:
        caps = darwin_capabilities()
        assert caps.low_latency is Capability.UNSUPPORTED
        assert caps.rs485 is Capability.UNSUPPORTED

    def test_break_signal_is_supported(self) -> None:
        # Shared _posix/ioctl.py carries the BSD-family numeric fallback
        # for TIOCSBRK / TIOCCBRK — Darwin shares <sys/ttycom.h>.
        assert darwin_capabilities().break_signal is Capability.SUPPORTED

    def test_rts_cts_is_supported(self) -> None:
        # _posix/termios_apply.py detects CCTS_OFLOW | CRTS_IFLOW on Darwin.
        assert darwin_capabilities().rts_cts is Capability.SUPPORTED

    def test_port_discovery_is_supported(self) -> None:
        # Native IOKit enumeration lives in _darwin/discovery.py.
        assert darwin_capabilities().port_discovery is Capability.SUPPORTED

    def test_queue_depth_ioctls_supported(self) -> None:
        caps = darwin_capabilities()
        assert caps.input_waiting is Capability.SUPPORTED
        assert caps.output_waiting is Capability.SUPPORTED
