# pyright: reportPrivateUsage=false
"""Unit tests for :func:`anyserial._linux.discovery.enumerate_ports`.

Builds synthetic sysfs trees under ``tmp_path`` and points
:func:`enumerate_ports` at them, so the full walk — including symlink
resolution, USB-ancestor detection, virtual-console filtering, and partial
metadata fallbacks — exercises real filesystem code without needing
``/sys/class/tty`` to look any particular way. Pure :mod:`pathlib`, no
Linux-only syscalls, so the suite is collected (and runs) on every host;
the actual sysfs format is the same shape on every Linux kernel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from anyserial._linux.discovery import (
    _DEFAULT_DEV_ROOT,
    _DEFAULT_SYS_ROOT,
    enumerate_ports,
    resolve_port_info,
)
from anyserial.discovery import PortInfo

if TYPE_CHECKING:
    from pathlib import Path


def _make_usb_device(
    devices_root: Path,
    *,
    bus: str = "1-1",
    iface: str = "1.0",
    vid: str | None = "0403",
    pid: str | None = "6001",
    serial: str | None = "A12345BC",
    manufacturer: str | None = "FTDI",
    product: str | None = "FT232R USB UART",
    interface_name: str | None = "FT232R USB UART",
) -> Path:
    """Build a USB device + interface under ``devices_root`` and return the iface dir.

    The returned path is what ``/sys/class/tty/<name>/device`` should
    symlink to — i.e. the USB *interface* directory, mirroring the real
    kernel layout.
    """
    dev_dir = devices_root / "pci0000:00" / "0000:00:14.0" / "usb1" / bus
    dev_dir.mkdir(parents=True)
    if vid is not None:
        (dev_dir / "idVendor").write_text(vid)
    if pid is not None:
        (dev_dir / "idProduct").write_text(pid)
    if serial is not None:
        (dev_dir / "serial").write_text(serial)
    if manufacturer is not None:
        (dev_dir / "manufacturer").write_text(manufacturer)
    if product is not None:
        (dev_dir / "product").write_text(product)

    iface_dir = dev_dir / f"{bus}:{iface}"
    iface_dir.mkdir()
    if interface_name is not None:
        (iface_dir / "interface").write_text(interface_name)
    return iface_dir


def _make_tty_entry(sys_root: Path, name: str, *, device_target: Path | None) -> Path:
    """Create ``sys_root/<name>`` and optionally symlink ``device`` to ``device_target``."""
    entry = sys_root / name
    entry.mkdir(parents=True)
    if device_target is not None:
        (entry / "device").symlink_to(device_target)
    return entry


@pytest.fixture
def fake_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return ``(sys_root, devices_root, dev_root)`` triple inside ``tmp_path``."""
    sys_root = tmp_path / "sys" / "class" / "tty"
    devices_root = tmp_path / "sys" / "devices"
    dev_root = tmp_path / "dev"
    sys_root.mkdir(parents=True)
    devices_root.mkdir(parents=True)
    dev_root.mkdir(parents=True)
    return sys_root, devices_root, dev_root


class TestUsbAdapter:
    def test_full_metadata_populated(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root)
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        ports = enumerate_ports(sys_root=sys_root, dev_root=dev_root)

        assert ports == [
            PortInfo(
                device=str(dev_root / "ttyUSB0"),
                name="ttyUSB0",
                description="FT232R USB UART",
                hwid="USB VID:PID=0403:6001 SER=A12345BC LOCATION=1-1",
                vid=0x0403,
                pid=0x6001,
                serial_number="A12345BC",
                manufacturer="FTDI",
                product="FT232R USB UART",
                location="1-1",
                interface="FT232R USB UART",
            )
        ]

    def test_missing_optional_metadata_fields_are_none(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        # Some adapters omit serial / manufacturer / product / interface name.
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(
            devices_root,
            serial=None,
            manufacturer=None,
            product=None,
            interface_name=None,
        )
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.vid == 0x0403
        assert port.pid == 0x6001
        assert port.serial_number is None
        assert port.manufacturer is None
        assert port.product is None
        assert port.interface is None
        # hwid still emitted because vid+pid are present, just no SER suffix.
        assert port.hwid == "USB VID:PID=0403:6001 LOCATION=1-1"

    def test_walks_through_hub_to_root_device(self, fake_roots: tuple[Path, Path, Path]) -> None:
        # Adapter behind a USB hub: bus is "1-1.4" — interface dir is one
        # level deeper but the walker still finds the right device.
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root, bus="1-1.4", vid="10C4", pid="EA60", serial="0001")
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.vid == 0x10C4
        assert port.pid == 0xEA60
        assert port.location == "1-1.4"

    def test_uppercase_hex_emitted_even_for_lowercase_sysfs_input(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        # sysfs writes "0403" / "6001" as lowercase; pyserial-style hwid is
        # uppercase. Verify the formatting normalizes.
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root, vid="abcd", pid="ef01")
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.hwid is not None
        assert "ABCD:EF01" in port.hwid


class TestNonUsbDevice:
    def test_platform_serial_has_no_usb_metadata_but_is_listed(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        # On-board UART: device target is a platform-bus dir, no idVendor anywhere.
        sys_root, devices_root, dev_root = fake_roots
        platform_dir = devices_root / "platform" / "serial8250" / "tty" / "ttyS0"
        platform_dir.mkdir(parents=True)
        _make_tty_entry(sys_root, "ttyS0", device_target=platform_dir)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.device == str(dev_root / "ttyS0")
        assert port.name == "ttyS0"
        assert port.vid is None
        assert port.pid is None
        assert port.hwid is None
        assert port.location is None


class TestFiltering:
    def test_entries_without_device_link_are_skipped(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        # `console` and friends: directory exists but no `device` symlink.
        sys_root, _devices_root, dev_root = fake_roots
        _make_tty_entry(sys_root, "console", device_target=None)
        assert enumerate_ports(sys_root=sys_root, dev_root=dev_root) == []

    def test_virtual_console_entries_are_skipped(self, fake_roots: tuple[Path, Path, Path]) -> None:
        # /sys/devices/virtual/tty/tty1 — pseudo terminal, not a serial device.
        sys_root, devices_root, dev_root = fake_roots
        virtual_target = devices_root / "virtual" / "tty" / "tty1"
        virtual_target.mkdir(parents=True)
        _make_tty_entry(sys_root, "tty1", device_target=virtual_target)
        assert enumerate_ports(sys_root=sys_root, dev_root=dev_root) == []

    def test_broken_device_symlink_is_skipped(self, fake_roots: tuple[Path, Path, Path]) -> None:
        # USB device unplugged mid-walk: symlink target no longer exists.
        sys_root, devices_root, dev_root = fake_roots
        target = devices_root / "ghost"
        # Don't create the target — leaves a dangling symlink.
        _make_tty_entry(sys_root, "ttyUSB9", device_target=target)
        assert enumerate_ports(sys_root=sys_root, dev_root=dev_root) == []

    def test_missing_sys_root_returns_empty(self, tmp_path: Path) -> None:
        # Sandboxed CI runners and some containers lack /sys/class/tty entirely.
        ghost = tmp_path / "no-such-sys"
        assert enumerate_ports(sys_root=ghost, dev_root=tmp_path / "dev") == []


class TestOrderingAndShape:
    def test_ports_are_sorted_by_name(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, devices_root, dev_root = fake_roots
        for i, name in enumerate(["ttyUSB2", "ttyUSB0", "ttyUSB1"]):
            iface = _make_usb_device(devices_root, bus=f"1-{i + 1}", serial=f"S{i}")
            _make_tty_entry(sys_root, name, device_target=iface)

        ports = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert [p.name for p in ports] == ["ttyUSB0", "ttyUSB1", "ttyUSB2"]

    def test_returns_list_not_iterator(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, _devices_root, dev_root = fake_roots
        result = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert isinstance(result, list)


class TestMalformedAttributes:
    def test_non_hex_vendor_id_drops_vid_and_hwid(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        # Defensive: a corrupted sysfs read shouldn't crash the walker.
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root, vid="GARBAGE")
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.vid is None
        # hwid requires both vid AND pid; if either is None, no string emitted.
        assert port.hwid is None
        assert port.pid == 0x6001  # the well-formed half is still parsed

    def test_empty_metadata_files_treated_as_none(
        self, fake_roots: tuple[Path, Path, Path]
    ) -> None:
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root, serial="", manufacturer="")
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        [port] = enumerate_ports(sys_root=sys_root, dev_root=dev_root)
        assert port.serial_number is None
        assert port.manufacturer is None


class TestProductionDefaults:
    def test_default_roots_are_real_sysfs_paths(self) -> None:
        # Smoke-check the module-level defaults so a typo never makes it past CI.
        assert str(_DEFAULT_SYS_ROOT) == "/sys/class/tty"
        assert str(_DEFAULT_DEV_ROOT) == "/dev"


class TestResolvePortInfo:
    def test_resolves_known_usb_path(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, devices_root, dev_root = fake_roots
        iface = _make_usb_device(devices_root)
        _make_tty_entry(sys_root, "ttyUSB0", device_target=iface)

        info = resolve_port_info("/dev/ttyUSB0", sys_root=sys_root, dev_root=dev_root)
        assert info is not None
        assert info.vid == 0x0403
        assert info.serial_number == "A12345BC"

    def test_resolves_non_usb_platform_serial(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, devices_root, dev_root = fake_roots
        platform_dir = devices_root / "platform" / "serial8250" / "tty" / "ttyS0"
        platform_dir.mkdir(parents=True)
        _make_tty_entry(sys_root, "ttyS0", device_target=platform_dir)

        info = resolve_port_info("/dev/ttyS0", sys_root=sys_root, dev_root=dev_root)
        assert info is not None
        assert info.device.endswith("ttyS0")
        assert info.vid is None

    def test_unknown_path_returns_none(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, _devices_root, dev_root = fake_roots
        # No entry created — looking for /dev/ttyUSB99 yields nothing.
        assert resolve_port_info("/dev/ttyUSB99", sys_root=sys_root, dev_root=dev_root) is None

    def test_pty_path_returns_none(self, fake_roots: tuple[Path, Path, Path]) -> None:
        # Path("/dev/pts/0").name == "0"; sys/class/tty/0 doesn't exist.
        sys_root, _devices_root, dev_root = fake_roots
        assert resolve_port_info("/dev/pts/0", sys_root=sys_root, dev_root=dev_root) is None

    def test_empty_path_returns_none(self, fake_roots: tuple[Path, Path, Path]) -> None:
        sys_root, _devices_root, dev_root = fake_roots
        assert resolve_port_info("", sys_root=sys_root, dev_root=dev_root) is None

    def test_virtual_console_path_returns_none(self, fake_roots: tuple[Path, Path, Path]) -> None:
        # Even if /sys/class/tty/tty1 exists, virtual-target filtering applies.
        sys_root, devices_root, dev_root = fake_roots
        virtual_target = devices_root / "virtual" / "tty" / "tty1"
        virtual_target.mkdir(parents=True)
        _make_tty_entry(sys_root, "tty1", device_target=virtual_target)

        assert resolve_port_info("/dev/tty1", sys_root=sys_root, dev_root=dev_root) is None
