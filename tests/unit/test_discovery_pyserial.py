# pyright: reportPrivateUsage=false
"""Unit tests for the optional pyserial discovery backend.

Stubs ``serial.tools.list_ports.comports`` so the translation layer is
exercised without depending on the real package being installed. The
``_normalize`` rule (``"n/a"`` and empty strings → ``None``) is the part
worth nailing down — pyserial's placeholder is the most common surprise
when migrating from it.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from anyserial._discovery import pyserial as discovery_pyserial
from anyserial.discovery import PortInfo

if TYPE_CHECKING:
    from collections.abc import Callable


def _fake_listportinfo(**kwargs: Any) -> SimpleNamespace:
    """Build a stand-in for ``serial.tools.list_ports_common.ListPortInfo``.

    Defaults mirror pyserial's: ``"n/a"`` for ``description`` and ``hwid``,
    ``None`` for everything else. Tests override what they care about.
    """
    base: dict[str, Any] = {
        "device": "/dev/ttyUSB0",
        "name": "ttyUSB0",
        "description": "n/a",
        "hwid": "n/a",
        "vid": None,
        "pid": None,
        "serial_number": None,
        "manufacturer": None,
        "product": None,
        "location": None,
        "interface": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _install_fake_pyserial(
    monkeypatch: pytest.MonkeyPatch, comports_fn: Callable[[], list[Any]]
) -> None:
    """Stub ``serial.tools.list_ports`` with a custom ``comports`` callable."""
    serial_pkg = ModuleType("serial")
    serial_tools = ModuleType("serial.tools")
    list_ports = ModuleType("serial.tools.list_ports")
    list_ports.comports = comports_fn  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "serial", serial_pkg)
    monkeypatch.setitem(sys.modules, "serial.tools", serial_tools)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", list_ports)


class TestMissingExtra:
    def test_missing_pyserial_raises_with_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Wipe any cached serial.* modules and block re-import.
        for key in [k for k in sys.modules if k == "serial" or k.startswith("serial.")]:
            monkeypatch.delitem(sys.modules, key, raising=False)

        class _Blocker:
            def find_spec(self, name: str, *args: object, **kwargs: object) -> None:
                if name == "serial" or name.startswith("serial."):
                    raise ImportError("blocked for test")

        sys.meta_path.insert(0, _Blocker())
        try:
            with pytest.raises(ImportError, match="anyserial\\[discovery-pyserial\\]"):
                discovery_pyserial.enumerate_ports()
        finally:
            sys.meta_path.pop(0)


class TestTranslation:
    def test_full_metadata_translates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_pyserial(
            monkeypatch,
            lambda: [
                _fake_listportinfo(
                    device="/dev/ttyUSB0",
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
            ],
        )

        [port] = discovery_pyserial.enumerate_ports()
        assert port == PortInfo(
            device="/dev/ttyUSB0",
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

    def test_pyserial_placeholder_normalized_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default ListPortInfo has description="n/a", hwid="n/a".
        _install_fake_pyserial(monkeypatch, lambda: [_fake_listportinfo(device="/dev/ttyS0")])

        [port] = discovery_pyserial.enumerate_ports()
        assert port.description is None
        assert port.hwid is None

    def test_empty_strings_normalized_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_pyserial(
            monkeypatch,
            lambda: [_fake_listportinfo(description="", manufacturer="   ")],
        )

        [port] = discovery_pyserial.enumerate_ports()
        assert port.description is None
        assert port.manufacturer is None

    def test_empty_comports_returns_empty_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_pyserial(monkeypatch, lambda: [])
        assert discovery_pyserial.enumerate_ports() == []

    def test_returns_list_not_iterator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_pyserial(monkeypatch, lambda: [_fake_listportinfo()])
        assert isinstance(discovery_pyserial.enumerate_ports(), list)


class TestNormalize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("n/a", None),
            ("", None),
            ("   ", None),
            ("FTDI", "FTDI"),
            ("  FTDI  ", "FTDI"),
            ("0", "0"),
        ],
    )
    def test_normalize_table(self, value: str | None, expected: str | None) -> None:
        assert discovery_pyserial._normalize(value) == expected
