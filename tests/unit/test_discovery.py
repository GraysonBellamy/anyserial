# pyright: reportPrivateUsage=false
"""Tests for the public discovery API.

Covers:

- :class:`PortInfo` shape, defaults, immutability, and hashability.
- :func:`list_serial_ports` and :func:`find_serial_port` filter logic, with
  the platform selector monkeypatched so the tests do not depend on real
  sysfs / IOKit / WinAPI state.
- Platform dispatch for Linux / Darwin / BSD / Windows enumerators and
  the unimplemented-platform error path for anything else.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import TYPE_CHECKING

import pytest

import anyserial
from anyserial import discovery
from anyserial._discovery.pyserial import enumerate_ports as _pyserial_enumerate
from anyserial._discovery.pyudev import enumerate_ports as _pyudev_enumerate
from anyserial._linux.discovery import enumerate_ports as _linux_enumerate_ports
from anyserial.discovery import (
    PortInfo,
    canonical_port_name,
    find_serial_port,
    list_serial_ports,
)
from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


pytestmark = pytest.mark.anyio


_FTDI = PortInfo(
    device="/dev/ttyUSB0",
    name="ttyUSB0",
    description="FT232R USB UART",
    vid=0x0403,
    pid=0x6001,
    serial_number="A12345BC",
    manufacturer="FTDI",
    product="FT232R USB UART",
    location="usb-0000:00:14.0-1",
    interface="iface0",
)
_CP210X = PortInfo(
    device="/dev/ttyUSB1",
    name="ttyUSB1",
    description="CP2102 USB to UART Bridge Controller",
    vid=0x10C4,
    pid=0xEA60,
    serial_number="0001",
    manufacturer="Silicon Labs",
)
_BUILTIN = PortInfo(device="/dev/ttyS0", name="ttyS0")
_WIN_COM3 = PortInfo(device="COM3", name="COM3", description="Communications Port (COM3)")
_WIN_COM8 = PortInfo(
    device="COM8",
    name="COM8",
    description="USB Serial Port (COM8)",
    vid=0x0403,
    pid=0x6001,
    serial_number="BG00VBZS",
)


def _symlink(link: Path, target: Path) -> None:
    """Create ``link`` pointing at ``target``, or skip where that is not permitted."""
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create symlinks here: {exc}")


def _stub_enumerator(ports: list[PortInfo]) -> Callable[[], list[PortInfo]]:
    """Return a zero-arg callable that yields ``ports`` (fresh list each call)."""

    def _enumerate() -> list[PortInfo]:
        return list(ports)

    return _enumerate


@pytest.fixture
def stub_discovery(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[PortInfo]], None]:
    """Inject a fixed port list as the platform discovery backend."""

    def _install(ports: list[PortInfo]) -> None:
        monkeypatch.setattr(
            discovery, "_select_discovery", lambda backend="native": _stub_enumerator(ports)
        )

    return _install


class TestPortInfo:
    def test_required_field_is_device(self) -> None:
        port = PortInfo(device="/dev/ttyS0")
        assert port.device == "/dev/ttyS0"
        assert port.vid is None
        assert port.serial_number is None

    def test_is_frozen(self) -> None:
        port = PortInfo(device="/dev/ttyS0")
        with pytest.raises(dataclasses.FrozenInstanceError):
            port.device = "/dev/ttyS1"  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        # With slots there is no per-instance __dict__; combined with frozen=True
        # this gives the cheap, hash-stable PortInfo §23 prescribes.
        port = PortInfo(device="/dev/ttyS0")
        assert not hasattr(port, "__dict__")
        assert hasattr(PortInfo, "__slots__")

    def test_equality_by_value(self) -> None:
        a = PortInfo(device="/dev/ttyUSB0", vid=0x0403, pid=0x6001)
        b = PortInfo(device="/dev/ttyUSB0", vid=0x0403, pid=0x6001)
        assert a == b
        assert hash(a) == hash(b)

    def test_kw_only_construction(self) -> None:
        with pytest.raises(TypeError):
            PortInfo("/dev/ttyS0")  # type: ignore[call-arg]


class TestListSerialPorts:
    async def test_returns_platform_enumeration(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _BUILTIN])
        ports = await list_serial_ports()
        assert ports == [_FTDI, _BUILTIN]

    async def test_returns_fresh_list_each_call(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI])
        first = await list_serial_ports()
        second = await list_serial_ports()
        assert first == second
        assert first is not second

    async def test_empty_when_platform_has_no_ports(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([])
        assert await list_serial_ports() == []


class TestFindSerialPort:
    async def test_no_filters_returns_first_port(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _CP210X])
        assert await find_serial_port() == _FTDI

    async def test_filter_by_vid_pid(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _CP210X, _BUILTIN])
        assert await find_serial_port(vid=0x10C4, pid=0xEA60) == _CP210X

    async def test_filter_by_serial_number(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _CP210X])
        assert await find_serial_port(serial_number="A12345BC") == _FTDI

    async def test_filter_by_device_path(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _CP210X, _BUILTIN])
        assert await find_serial_port(device="/dev/ttyS0") == _BUILTIN

    async def test_filters_are_anded(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI, _CP210X])
        # vid matches FTDI but pid does not — no match.
        assert await find_serial_port(vid=0x0403, pid=0x9999) is None

    async def test_returns_first_match(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        twin = dataclasses.replace(_FTDI, device="/dev/ttyUSB2", serial_number="DUPLICATE")
        stub_discovery([_FTDI, twin])
        # Both share vid/pid; first match wins.
        assert await find_serial_port(vid=0x0403, pid=0x6001) == _FTDI

    async def test_no_match_returns_none(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([_FTDI])
        assert await find_serial_port(vid=0xDEAD) is None

    async def test_empty_enumeration_returns_none(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
    ) -> None:
        stub_discovery([])
        assert await find_serial_port(vid=0x0403) is None

    @pytest.mark.parametrize("name", ["COM8", "com8", "\\\\.\\COM8", "\\\\?\\com8"])
    async def test_device_matches_every_windows_spelling(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
        monkeypatch: pytest.MonkeyPatch,
        name: str,
    ) -> None:
        stub_discovery([_WIN_COM3, _WIN_COM8])
        monkeypatch.setattr(sys, "platform", "win32")
        assert await find_serial_port(device=name) == _WIN_COM8

    async def test_device_matches_a_symlink_to_the_port(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "ttyUSB0"
        target.touch()
        link = tmp_path / "usb-FTDI_FT232R_USB_UART_A12345BC-if00-port0"
        _symlink(link, target)
        port = dataclasses.replace(_FTDI, device=str(target))
        stub_discovery([_BUILTIN, port])
        monkeypatch.setattr(sys, "platform", "linux")
        assert await find_serial_port(device=str(link)) == port

    async def test_device_and_other_filters_are_anded(
        self,
        stub_discovery: Callable[[list[PortInfo]], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stub_discovery([_WIN_COM3, _WIN_COM8])
        monkeypatch.setattr(sys, "platform", "win32")
        assert await find_serial_port(device="com8", vid=0x0403) == _WIN_COM8
        assert await find_serial_port(device="com8", vid=0x10C4) is None


class TestCanonicalPortName:
    @pytest.mark.parametrize(
        "name",
        ["COM8", "com8", "Com8", "\\\\.\\COM8", "\\\\?\\COM8", "\\\\.\\com8", "\\\\?\\com8"],
    )
    def test_windows_names_of_one_port_agree(self, name: str) -> None:
        assert canonical_port_name(name, platform="win32") == "COM8"

    def test_windows_ports_above_com9(self) -> None:
        assert canonical_port_name("\\\\.\\com10", platform="win32") == "COM10"

    def test_windows_name_matches_what_discovery_reports(self) -> None:
        assert canonical_port_name("\\\\?\\com8", platform="win32") == _WIN_COM8.device

    def test_windows_device_interface_path(self) -> None:
        path = "\\\\?\\usb#vid_0403&pid_6001#a12345#{86e0d1e0-8089-11d0-9ce4-08003e301f73}"
        assert canonical_port_name(path, platform="win32") == (
            "USB#VID_0403&PID_6001#A12345#{86E0D1E0-8089-11D0-9CE4-08003E301F73}"
        )

    def test_posix_symlink_resolves_to_its_target(self, tmp_path: Path) -> None:
        target = tmp_path / "ttyUSB0"
        target.touch()
        link = tmp_path / "by-id-link"
        _symlink(link, target)
        assert canonical_port_name(str(link), platform="linux") == os.path.realpath(target)
        assert canonical_port_name(str(link), platform="linux") == canonical_port_name(
            str(target), platform="linux"
        )

    def test_posix_relative_path_that_exists_becomes_absolute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / "ttyS9").touch()
        monkeypatch.chdir(tmp_path)
        assert canonical_port_name("ttyS9", platform="linux") == os.path.realpath(
            tmp_path / "ttyS9"
        )

    @pytest.mark.parametrize(
        "name",
        ["/dev/anyserial-absent", "mock://port", "socket://localhost:7000", ""],
    )
    def test_posix_names_that_do_not_exist_are_kept(self, name: str) -> None:
        assert canonical_port_name(name, platform="linux") == name

    def test_posix_dangling_symlink_is_kept(self, tmp_path: Path) -> None:
        link = tmp_path / "unplugged"
        _symlink(link, tmp_path / "ttyUSB9")
        assert canonical_port_name(str(link), platform="darwin") == str(link)

    def test_platform_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert canonical_port_name("\\\\?\\com8") == "COM8"
        monkeypatch.setattr(sys, "platform", "linux")
        assert canonical_port_name("/dev/anyserial-absent") == "/dev/anyserial-absent"


class TestPlatformSelector:
    def test_unimplemented_platform_raises_with_platform_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "haiku")
        with pytest.raises(UnsupportedPlatformError, match="haiku"):
            discovery._select_discovery()

    def test_win32_resolves_to_native_setupapi_enumerator(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # sys.platform == "win32" dispatches to the SetupAPI enumerator.
        from anyserial._windows.discovery import (  # noqa: PLC0415 — lazy-per-platform mirror
            enumerate_ports as _windows_enumerate_ports,
        )

        monkeypatch.setattr(sys, "platform", "win32")
        assert discovery._select_discovery() is _windows_enumerate_ports

    @pytest.mark.parametrize("platform", ["linux", "linux2"])
    def test_linux_platforms_resolve_to_native_enumerator(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform: str,
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        assert discovery._select_discovery() is _linux_enumerate_ports

    def test_darwin_resolves_to_native_iokit_enumerator(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # sys.platform == "darwin" dispatches to the IOKit enumerator.
        from anyserial._darwin.discovery import (  # noqa: PLC0415 — lazy-per-platform mirror
            enumerate_ports as _darwin_enumerate_ports,
        )

        monkeypatch.setattr(sys, "platform", "darwin")
        assert discovery._select_discovery() is _darwin_enumerate_ports

    @pytest.mark.parametrize(
        "platform",
        ["freebsd13", "freebsd14", "openbsd7", "netbsd11", "dragonfly6"],
    )
    def test_bsd_family_resolves_to_native_dev_scanner(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform: str,
    ) -> None:
        # BSD discovery is shared across FreeBSD / NetBSD / OpenBSD /
        # DragonFly. All four variants dispatch to the same /dev-scan
        # enumerator; variant-specific pattern selection happens inside
        # the enumerator.
        from anyserial._bsd.discovery import (  # noqa: PLC0415 — lazy-per-platform mirror
            enumerate_ports as _bsd_enumerate_ports,
        )

        monkeypatch.setattr(sys, "platform", platform)
        assert discovery._select_discovery() is _bsd_enumerate_ports

    async def test_list_serial_ports_propagates_unimplemented(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "haiku")
        with pytest.raises(UnsupportedPlatformError):
            await list_serial_ports()


class TestBackendSelector:
    def test_pyserial_backend_returns_pyserial_enumerator(self) -> None:
        assert discovery._select_discovery("pyserial") is _pyserial_enumerate

    def test_pyudev_backend_returns_pyudev_enumerator(self) -> None:
        assert discovery._select_discovery("pyudev") is _pyudev_enumerate

    def test_pyserial_selector_works_on_non_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # pyserial is cross-platform; selector must not fall through to the
        # native UnsupportedPlatformError path on darwin/win32.
        monkeypatch.setattr(sys, "platform", "darwin")
        assert discovery._select_discovery("pyserial") is _pyserial_enumerate

    async def test_list_serial_ports_dispatches_through_backend_kwarg(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Replace the pyserial enumerator with a sentinel so we can prove the
        # backend kwarg routed there (instead of native).
        sentinel = [PortInfo(device="/dev/sentinel")]

        def fake_enumerate() -> list[PortInfo]:
            return sentinel

        monkeypatch.setattr("anyserial._discovery.pyserial.enumerate_ports", fake_enumerate)
        result = await list_serial_ports(backend="pyserial")
        assert result == sentinel

    async def test_find_serial_port_propagates_backend_kwarg(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorded: list[str] = []
        sentinel = [
            PortInfo(device="/dev/ttyUSB0", vid=0x0403, pid=0x6001),
        ]

        def fake_select(backend: str) -> object:
            recorded.append(backend)
            return lambda: sentinel

        monkeypatch.setattr(discovery, "_select_discovery", fake_select)

        result = await find_serial_port(vid=0x0403, backend="pyudev")
        assert result == sentinel[0]
        assert recorded == ["pyudev"]


class TestPublicReexports:
    def test_top_level_module_exposes_discovery_api(self) -> None:
        assert anyserial.PortInfo is PortInfo
        assert anyserial.list_serial_ports is list_serial_ports
        assert anyserial.find_serial_port is find_serial_port
        assert anyserial.canonical_port_name is canonical_port_name
        for name in (
            "PortInfo",
            "list_serial_ports",
            "find_serial_port",
            "canonical_port_name",
            "DiscoveryBackend",
        ):
            assert name in anyserial.__all__
