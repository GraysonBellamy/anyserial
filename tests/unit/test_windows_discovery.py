# pyright: reportPrivateUsage=false
"""Unit tests for the Windows SetupAPI discovery helpers.

Tests the pure-Python string-parsing functions that extract USB metadata
from Windows device instance IDs, hardware IDs and friendly names, and the
path matching in ``resolve_port_info`` with the enumerators replaced. These
run on any platform — the actual SetupAPI calls are exercised only in
Windows CI integration tests.
"""

from __future__ import annotations

import pytest

from anyserial._windows import discovery as windows_discovery
from anyserial._windows.discovery import (
    _extract_com_name,
    _format_hwid,
    _ftdi_serial_number,
    _parse_device_ids,
    _parse_hardware_id,
    _strip_com_suffix,
    resolve_port_info,
)
from anyserial.discovery import PortInfo


class TestParseDeviceIds:
    """``(vid, pid, serial)`` from a port's device instance ID and hardware ID.

    The first five cases are the IDs Windows reports for real adapters.
    """

    @pytest.mark.parametrize(
        ("instance_id", "hardware_id", "expected"),
        [
            pytest.param(
                "FTDIBUS\\VID_0403+PID_6001+BG00VBZSA\\0000",
                "FTDIBUS\\COMPORT&VID_0403&PID_6001",
                (0x0403, 0x6001, "BG00VBZS"),
                id="ft232r",
            ),
            pytest.param(
                "FTDIBUS\\VID_0856+PID_AC33+BBZ7WWS2A\\0000",
                "FTDIBUS\\COMPORT&VID_0856&PID_AC33",
                (0x0856, 0xAC33, "BBZ7WWS2"),
                id="ftdi-vcp-other-vendor",
            ),
            pytest.param(
                "USB\\VID_067B&PID_2303\\6&406CFD0&0&3",
                "USB\\VID_067B&PID_2303&REV_0400",
                (0x067B, 0x2303, None),
                id="prolific-no-serial",
            ),
            pytest.param(
                "PCI\\VEN_8086&DEV_7AEB&SUBSYS_334B17AA&REV_11\\3&11583659&0&B3",
                "PCI\\VEN_8086&DEV_7AEB&SUBSYS_334B17AA&REV_11",
                (None, None, None),
                id="pci-uart",
            ),
            pytest.param(
                "ACPI\\PNP0501\\0",
                "ACPI\\PNP0501",
                (None, None, None),
                id="acpi-uart",
            ),
            pytest.param(
                "USB\\VID_10C4&PID_EA60\\0001",
                "USB\\VID_10C4&PID_EA60&REV_0100",
                (0x10C4, 0xEA60, "0001"),
                id="usb-serial-number",
            ),
            pytest.param(
                "USB\\VID_1A86&PID_7523\\5&3753427A&0&4",
                "USB\\VID_1A86&PID_7523&REV_0254",
                (0x1A86, 0x7523, None),
                id="ch340-generated-id",
            ),
            pytest.param(
                "USB\\VID_2341&PID_8036&MI_00\\7&2A4F6E1&0&0000",
                "USB\\VID_2341&PID_8036&REV_0100&MI_00",
                (0x2341, 0x8036, None),
                id="composite-interface",
            ),
            pytest.param(
                "FTDIBUS\\VID_0403+PID_6010+FT5ABCDEB\\0000",
                "FTDIBUS\\COMPORT&VID_0403&PID_6010",
                (0x0403, 0x6010, "FT5ABCDE"),
                id="ft2232h-second-port",
            ),
            pytest.param(
                "usb\\vid_0403&pid_6001\\a12345",
                None,
                (0x0403, 0x6001, "a12345"),
                id="lower-case",
            ),
            pytest.param(
                None,
                "FTDIBUS\\COMPORT&VID_0403&PID_6001",
                (0x0403, 0x6001, None),
                id="hardware-id-fallback",
            ),
            pytest.param(None, None, (None, None, None), id="nothing"),
        ],
    )
    def test_ids(
        self,
        instance_id: str | None,
        hardware_id: str | None,
        expected: tuple[int | None, int | None, str | None],
    ) -> None:
        assert _parse_device_ids(instance_id, hardware_id) == expected


class TestParseHardwareId:
    """VID / PID named anywhere in a hardware ID."""

    def test_usb(self) -> None:
        assert _parse_hardware_id("USB\\VID_067B&PID_2303&REV_0400") == (0x067B, 0x2303)

    def test_ftdibus(self) -> None:
        assert _parse_hardware_id("FTDIBUS\\COMPORT&VID_0403&PID_6001") == (0x0403, 0x6001)

    def test_lower_case_hex(self) -> None:
        assert _parse_hardware_id("USB\\VID_1a86&PID_7523") == (0x1A86, 0x7523)

    @pytest.mark.parametrize("hwid", ["ACPI\\PNP0501", "PCI\\VEN_8086&DEV_1E3D", "", None])
    def test_no_vid_pid(self, hwid: str | None) -> None:
        assert _parse_hardware_id(hwid) == (None, None)


class TestFtdiSerialNumber:
    """The FTDI driver's port letter is removed from the chip serial number."""

    @pytest.mark.parametrize(
        ("segment", "expected"),
        [
            ("A103H1FFA", "A103H1FF"),
            ("FT5ABCDEB", "FT5ABCDE"),
            ("FT5ABCDED", "FT5ABCDE"),
            ("12345678", "12345678"),
            ("5&2D0E5D3B&0&2", None),
            ("", None),
            (None, None),
        ],
    )
    def test_segments(self, segment: str | None, expected: str | None) -> None:
        assert _ftdi_serial_number(segment) == expected


class TestExtractComName:
    """Extract ``COMn`` from a Windows friendly name string."""

    def test_typical_usb_serial(self) -> None:
        assert _extract_com_name("USB Serial Port (COM3)") == "COM3"

    def test_communications_port(self) -> None:
        assert _extract_com_name("Communications Port (COM1)") == "COM1"

    def test_high_com_number(self) -> None:
        assert _extract_com_name("Prolific USB-to-Serial (COM256)") == "COM256"

    def test_no_com_suffix(self) -> None:
        assert _extract_com_name("Some Port") is None

    def test_none_input(self) -> None:
        assert _extract_com_name(None) is None


class TestStripComSuffix:
    """Strip ``(COMn)`` to get the product description."""

    def test_strip_com3(self) -> None:
        assert _strip_com_suffix("USB Serial Port (COM3)") == "USB Serial Port"

    def test_strip_com256(self) -> None:
        assert _strip_com_suffix("Prolific USB-to-Serial (COM256)") == "Prolific USB-to-Serial"

    def test_no_com_returns_original(self) -> None:
        assert _strip_com_suffix("Some Port") == "Some Port"

    def test_none_input(self) -> None:
        assert _strip_com_suffix(None) is None


_COM3 = PortInfo(device="COM3", name="COM3", description="Communications Port (COM3)")
_COM8 = PortInfo(device="COM8", name="COM8", description="USB Serial Port (COM8)")
_COM20 = PortInfo(device="COM20", name="COM20")


class TestResolvePortInfo:
    """``resolve_port_info`` finds the entry for every spelling of a port."""

    @pytest.fixture(autouse=True)
    def _ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            windows_discovery,
            "_enumerate_setupapi",
            lambda: [_COM3, _COM8],
        )
        monkeypatch.setattr(
            windows_discovery,
            "_enumerate_registry_fallback",
            lambda: [_COM20],
        )

    @pytest.mark.parametrize(
        "path",
        ["COM8", "com8", "\\\\.\\COM8", "\\\\?\\COM8", "\\\\.\\com8", "\\\\?\\com8"],
    )
    def test_every_spelling_resolves(self, path: str) -> None:
        assert resolve_port_info(path) == _COM8

    def test_falls_back_to_the_registry(self) -> None:
        assert resolve_port_info("com20") == _COM20

    def test_unknown_port_is_none(self) -> None:
        assert resolve_port_info("COM99") is None


class TestFormatHwid:
    """Build pyserial-compatible ``USB VID:PID=…`` string."""

    def test_full_hwid(self) -> None:
        result = _format_hwid(0x0403, 0x6001, "A12345", "Port_#0001.Hub_#0003")
        assert result == "USB VID:PID=0403:6001 SER=A12345 LOCATION=Port_#0001.Hub_#0003"

    def test_no_serial_no_location(self) -> None:
        result = _format_hwid(0x067B, 0x2303, None, None)
        assert result == "USB VID:PID=067B:2303"

    def test_serial_only(self) -> None:
        result = _format_hwid(0x1A86, 0x7523, "FTDI123", None)
        assert result == "USB VID:PID=1A86:7523 SER=FTDI123"

    def test_location_only(self) -> None:
        result = _format_hwid(0x0403, 0x6001, None, "Port_#0001")
        assert result == "USB VID:PID=0403:6001 LOCATION=Port_#0001"

    def test_none_vid_returns_none(self) -> None:
        assert _format_hwid(None, 0x6001, None, None) is None

    def test_none_pid_returns_none(self) -> None:
        assert _format_hwid(0x0403, None, None, None) is None

    def test_both_none_returns_none(self) -> None:
        assert _format_hwid(None, None, None, None) is None

    @pytest.mark.parametrize(
        ("vid", "pid", "expected_prefix"),
        [
            (0x0000, 0x0000, "USB VID:PID=0000:0000"),
            (0xFFFF, 0xFFFF, "USB VID:PID=FFFF:FFFF"),
        ],
    )
    def test_hex_padding(self, vid: int, pid: int, expected_prefix: str) -> None:
        result = _format_hwid(vid, pid, None, None)
        assert result == expected_prefix
