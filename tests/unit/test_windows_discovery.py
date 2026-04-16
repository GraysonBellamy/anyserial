# pyright: reportPrivateUsage=false
"""Unit tests for the Windows SetupAPI discovery helpers.

Tests the pure-Python string-parsing functions that extract USB metadata
from Windows hardware ID strings and friendly names. These run on any
platform — the actual SetupAPI calls are exercised only in Windows CI
integration tests.
"""

from __future__ import annotations

import pytest

from anyserial._windows.discovery import (
    _extract_com_name,
    _format_hwid,
    _parse_hardware_id,
    _strip_com_suffix,
    _strip_dos_prefix,
)


class TestParseHardwareId:
    """Parse ``USB\\VID_xxxx&PID_xxxx\\serial`` into (vid, pid, serial)."""

    def test_full_usb_with_serial(self) -> None:
        vid, pid, serial = _parse_hardware_id("USB\\VID_0403&PID_6001\\A12345")
        assert vid == 0x0403
        assert pid == 0x6001
        assert serial == "A12345"

    def test_usb_without_serial(self) -> None:
        vid, pid, serial = _parse_hardware_id("USB\\VID_067B&PID_2303")
        assert vid == 0x067B
        assert pid == 0x2303
        assert serial is None

    def test_lowercase_hex_parsed(self) -> None:
        vid, pid, serial = _parse_hardware_id("USB\\VID_0403&PID_6001\\ftdi_serial")
        assert vid == 0x0403
        assert pid == 0x6001
        assert serial == "ftdi_serial"

    def test_non_usb_returns_none_triple(self) -> None:
        assert _parse_hardware_id("ACPI\\PNP0501\\0") == (None, None, None)

    def test_pci_returns_none_triple(self) -> None:
        assert _parse_hardware_id("PCI\\VEN_8086&DEV_1E3D\\3&11583659&0&B3") == (
            None,
            None,
            None,
        )

    def test_none_input_returns_none_triple(self) -> None:
        assert _parse_hardware_id(None) == (None, None, None)

    def test_empty_string_returns_none_triple(self) -> None:
        assert _parse_hardware_id("") == (None, None, None)

    def test_ch340_hardware_id(self) -> None:
        vid, pid, serial = _parse_hardware_id("USB\\VID_1A86&PID_7523\\5&3753427A&0&4")
        assert vid == 0x1A86
        assert pid == 0x7523
        assert serial == "5&3753427A&0&4"


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


class TestStripDosPrefix:
    r"""Strip ``\\.\`` or ``\\?\`` device prefix."""

    def test_dot_prefix(self) -> None:
        assert _strip_dos_prefix("\\\\.\\COM1") == "COM1"

    def test_question_prefix(self) -> None:
        assert _strip_dos_prefix("\\\\?\\COM10") == "COM10"

    def test_no_prefix(self) -> None:
        assert _strip_dos_prefix("COM1") == "COM1"

    def test_long_path(self) -> None:
        path = "\\\\?\\USB#VID_0403&PID_6001#A12345#{86e0d1e0}"
        assert _strip_dos_prefix(path) == "USB#VID_0403&PID_6001#A12345#{86e0d1e0}"


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
