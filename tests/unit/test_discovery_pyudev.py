# pyright: reportPrivateUsage=false
"""Unit tests for the optional pyudev discovery backend.

``pyudev`` is a system-package wrapper around ``libudev`` and may not be
installed in the test environment. These tests stub a fake ``pyudev``
module into ``sys.modules`` so the import path is exercised without a
real dependency. The integration coverage that exercises the real
``libudev`` lives in ``tests/integration/test_linux_discovery_pyudev.py``
behind ``pytest.importorskip``.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from anyserial._discovery import pyudev as discovery_pyudev
from anyserial.discovery import PortInfo
from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import Iterable


class _FakeProperties(dict[str, str]):
    """Mapping that mimics the ``pyudev.Device.properties`` interface."""


def _fake_device(
    *,
    device_node: str | None,
    sys_name: str | None = None,
    sys_path: str = "",
    properties: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Build a minimal stand-in for ``pyudev.Device``."""
    return SimpleNamespace(
        device_node=device_node,
        sys_name=sys_name,
        sys_path=sys_path,
        properties=_FakeProperties(properties or {}),
    )


def _install_fake_pyudev(monkeypatch: pytest.MonkeyPatch, devices: list[Any]) -> ModuleType:
    """Stub a ``pyudev`` module that yields ``devices`` from ``Context().list_devices``."""
    fake = ModuleType("pyudev")

    class _Context:
        def list_devices(self, *, subsystem: str) -> Iterable[Any]:
            assert subsystem == "tty"
            return iter(devices)

    fake.Context = _Context  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyudev", fake)
    return fake


@pytest.fixture
def linux_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``sys.platform == "linux"`` so the platform guard passes on macOS CI."""
    monkeypatch.setattr(sys, "platform", "linux")


class TestPlatformGuard:
    def test_non_linux_raises_before_importing_pyudev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drop pyudev from sys.modules so a successful import would succeed —
        # the guard must fire first regardless.
        monkeypatch.setitem(sys.modules, "pyudev", ModuleType("pyudev"))
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(UnsupportedPlatformError, match="Linux-only"):
            discovery_pyudev.enumerate_ports()


class TestMissingExtra:
    def test_missing_pyudev_raises_with_install_hint(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        # Force the lazy import to fail by removing the cached module and
        # blocking re-import via a finder hack.
        monkeypatch.delitem(sys.modules, "pyudev", raising=False)

        class _Blocker:
            def find_spec(self, name: str, *args: object, **kwargs: object) -> None:
                if name == "pyudev":
                    raise ImportError("blocked for test")

        sys.meta_path.insert(0, _Blocker())
        try:
            with pytest.raises(ImportError, match="anyserial\\[discovery-pyudev\\]"):
                discovery_pyudev.enumerate_ports()
        finally:
            sys.meta_path.pop(0)


class TestEnumeration:
    def test_full_usb_metadata_translates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        device = _fake_device(
            device_node="/dev/ttyUSB0",
            sys_name="ttyUSB0",
            sys_path="/sys/devices/pci0000:00/usb1/1-1/1-1:1.0/ttyUSB0",
            properties={
                "ID_VENDOR_ID": "0403",
                "ID_MODEL_ID": "6001",
                "ID_SERIAL_SHORT": "A12345BC",
                "ID_VENDOR_FROM_DATABASE": "Future Technology Devices",
                "ID_MODEL_FROM_DATABASE": "FT232 Serial (UART) IC",
                "ID_PATH": "pci-0000:00:14.0-usb-0:1:1.0",
                "ID_USB_INTERFACE_NUM": "00",
            },
        )
        _install_fake_pyudev(monkeypatch, [device])

        [port] = discovery_pyudev.enumerate_ports()
        assert port == PortInfo(
            device="/dev/ttyUSB0",
            name="ttyUSB0",
            description="FT232 Serial (UART) IC",
            hwid=("USB VID:PID=0403:6001 SER=A12345BC LOCATION=pci-0000:00:14.0-usb-0:1:1.0"),
            vid=0x0403,
            pid=0x6001,
            serial_number="A12345BC",
            manufacturer="Future Technology Devices",
            product="FT232 Serial (UART) IC",
            location="pci-0000:00:14.0-usb-0:1:1.0",
            interface="00",
        )

    def test_falls_back_to_id_vendor_when_database_lookup_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        device = _fake_device(
            device_node="/dev/ttyUSB0",
            sys_name="ttyUSB0",
            sys_path="/sys/devices/pci0000:00/usb1/1-1/1-1:1.0/ttyUSB0",
            properties={
                "ID_VENDOR_ID": "10C4",
                "ID_MODEL_ID": "EA60",
                # No *_FROM_DATABASE keys; udev rules sometimes omit them.
                "ID_VENDOR": "Silicon_Labs",
                "ID_MODEL": "CP2102_USB_to_UART_Bridge_Controller",
            },
        )
        _install_fake_pyudev(monkeypatch, [device])

        [port] = discovery_pyudev.enumerate_ports()
        assert port.manufacturer == "Silicon_Labs"
        assert port.product == "CP2102_USB_to_UART_Bridge_Controller"

    def test_non_usb_device_yields_minimal_portinfo(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        device = _fake_device(
            device_node="/dev/ttyS0",
            sys_name="ttyS0",
            sys_path="/sys/devices/platform/serial8250/tty/ttyS0",
        )
        _install_fake_pyudev(monkeypatch, [device])

        [port] = discovery_pyudev.enumerate_ports()
        assert port.device == "/dev/ttyS0"
        assert port.vid is None
        assert port.pid is None
        assert port.hwid is None

    def test_devices_without_node_are_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        no_node = _fake_device(device_node=None, sys_name="weird")
        good = _fake_device(
            device_node="/dev/ttyS0",
            sys_name="ttyS0",
            sys_path="/sys/devices/platform/serial8250/tty/ttyS0",
        )
        _install_fake_pyudev(monkeypatch, [no_node, good])

        [port] = discovery_pyudev.enumerate_ports()
        assert port.device == "/dev/ttyS0"

    def test_virtual_consoles_are_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        virtual = _fake_device(
            device_node="/dev/tty1",
            sys_name="tty1",
            sys_path="/sys/devices/virtual/tty/tty1",
        )
        _install_fake_pyudev(monkeypatch, [virtual])

        assert discovery_pyudev.enumerate_ports() == []

    def test_results_sorted_by_device_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        devices = [
            _fake_device(
                device_node=f"/dev/ttyUSB{i}",
                sys_name=f"ttyUSB{i}",
                sys_path=f"/sys/devices/usb/ttyUSB{i}",
            )
            for i in (2, 0, 1)
        ]
        _install_fake_pyudev(monkeypatch, devices)

        ports = discovery_pyudev.enumerate_ports()
        assert [p.device for p in ports] == [
            "/dev/ttyUSB0",
            "/dev/ttyUSB1",
            "/dev/ttyUSB2",
        ]

    def test_malformed_vendor_hex_drops_vid_and_hwid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        linux_platform: None,
    ) -> None:
        device = _fake_device(
            device_node="/dev/ttyUSB0",
            sys_name="ttyUSB0",
            sys_path="/sys/devices/usb/ttyUSB0",
            properties={"ID_VENDOR_ID": "ZZZZ", "ID_MODEL_ID": "6001"},
        )
        _install_fake_pyudev(monkeypatch, [device])

        [port] = discovery_pyudev.enumerate_ports()
        assert port.vid is None
        assert port.pid == 0x6001  # well-formed half still parses
        assert port.hwid is None  # hwid requires both


class TestPropertyHelpers:
    def test_prop_returns_none_when_properties_missing(self) -> None:
        device = SimpleNamespace(properties=None)
        assert discovery_pyudev._prop(device, "ID_VENDOR_ID") is None

    def test_prop_strips_and_normalizes_empty(self) -> None:
        device = _fake_device(device_node="/dev/x", properties={"K": "  ", "Other": "  value  "})
        assert discovery_pyudev._prop(device, "K") is None
        assert discovery_pyudev._prop(device, "Other") == "value"

    def test_parse_hex_handles_none_and_garbage(self) -> None:
        assert discovery_pyudev._parse_hex(None) is None
        assert discovery_pyudev._parse_hex("not-hex") is None
        assert discovery_pyudev._parse_hex("0403") == 0x0403
