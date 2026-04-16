"""Unit tests for :func:`linux_capabilities`."""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux.capabilities import linux_capabilities
from anyserial._types import Capability


class TestLinuxCapabilities:
    def test_backend_and_platform_tags(self) -> None:
        caps = linux_capabilities()
        assert caps.platform == "linux"
        assert caps.backend == "linux"

    def test_features_linux_definitely_supports(self) -> None:
        caps = linux_capabilities()
        assert caps.custom_baudrate is Capability.SUPPORTED
        assert caps.xon_xoff is Capability.SUPPORTED
        assert caps.rts_cts is Capability.SUPPORTED
        assert caps.break_signal is Capability.SUPPORTED
        assert caps.exclusive_access is Capability.SUPPORTED
        assert caps.input_waiting is Capability.SUPPORTED
        assert caps.output_waiting is Capability.SUPPORTED
        # low-latency reachable via TIOCSSERIAL + ASYNC_LOW_LATENCY,
        # mark/space parity via the hardcoded CMSPAR fallback.
        assert caps.low_latency is Capability.SUPPORTED
        assert caps.mark_space_parity is Capability.SUPPORTED
        # Native sysfs walk implemented in _linux/discovery.py.
        assert caps.port_discovery is Capability.SUPPORTED
        # TIOCSRS485 reachable via _linux.rs485. Whether a specific
        # driver actually accepts the ioctl is runtime-dependent and
        # surfaces via UnsupportedPolicy at apply time.
        assert caps.rs485 is Capability.SUPPORTED

    def test_device_dependent_features_are_unknown(self) -> None:
        caps = linux_capabilities()
        # Whether a specific driver answers TIOCMGET meaningfully is only
        # knowable at operation time — Capability.UNKNOWN is the right answer.
        assert caps.modem_lines is Capability.UNKNOWN
