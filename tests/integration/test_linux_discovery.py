"""Integration tests for :func:`enumerate_ports` against the real sysfs.

Pure smoke coverage: the unit suite already exercises every branch of the
walker against a synthetic tree, so the only thing left to verify on CI is
that the real ``/sys/class/tty`` layout doesn't trip the walker. Hardware
adapters are not assumed to exist — every assertion holds on a vanilla
GitHub Actions Linux runner with zero USB-serial devices attached.
"""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only sysfs walk", allow_module_level=True)

from anyserial._linux.discovery import enumerate_ports
from anyserial.discovery import PortInfo, list_serial_ports

pytestmark = pytest.mark.anyio


class TestRealSysfs:
    def test_enumerate_ports_returns_list_without_raising(self) -> None:
        ports = enumerate_ports()
        assert isinstance(ports, list)
        assert all(isinstance(p, PortInfo) for p in ports)

    def test_no_virtual_consoles_returned(self) -> None:
        # tty0..tty63, console, ptmx etc. all live under
        # /sys/devices/virtual/tty and must be filtered out — they would
        # otherwise dominate the listing on every Linux box.
        ports = enumerate_ports()
        virtual_names = {f"tty{i}" for i in range(64)} | {"console", "ptmx", "tty"}
        assert virtual_names.isdisjoint({p.name for p in ports})

    def test_every_port_has_a_dev_path(self) -> None:
        for port in enumerate_ports():
            assert port.device.startswith("/dev/")
            assert port.name is not None
            assert port.device.endswith(port.name)

    def test_usb_ports_carry_vid_pid_pairing(self) -> None:
        # Either both vid and pid are populated, or neither — the walker
        # never reports a half-resolved USB ancestor.
        for port in enumerate_ports():
            assert (port.vid is None) == (port.pid is None)
            if port.vid is not None:
                assert port.hwid is not None and port.hwid.startswith("USB VID:PID=")

    async def test_public_async_api_round_trips_against_real_sysfs(self) -> None:
        # End-to-end through list_serial_ports — exercises the to_thread path.
        ports = await list_serial_ports()
        assert isinstance(ports, list)
        assert all(isinstance(p, PortInfo) for p in ports)
