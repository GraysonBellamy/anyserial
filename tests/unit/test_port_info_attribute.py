"""Tests for the :attr:`SerialStreamAttribute.port_info` typed attribute.

Two surfaces matter here:

1. :class:`SerialPort` accepts an optional ``port_info=`` constructor arg
   and reflects it on both :attr:`SerialPort.port_info` and the AnyIO
   ``extra(SerialStreamAttribute.port_info)`` typed-attribute lookup.
2. The lookup *raises* :class:`anyio.TypedAttributeLookupError` when no
   ``port_info`` was supplied — matching AnyIO's convention for missing
   attributes (rather than returning ``None``, which would force callers
   to handle two empty states).

The :func:`open_serial_port` integration — sysfs walk → ``PortInfo`` →
``SerialPort`` — is covered by ``tests/integration/test_open_serial_port_info.py``
because it requires a real Linux environment.
"""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import anyio
import pytest

from anyserial import PortInfo, SerialConfig, SerialPort, SerialStreamAttribute
from anyserial.stream import _resolve_port_info_for_path
from anyserial.testing import MockBackend

if sys.platform.startswith("linux"):
    from anyserial._linux import discovery as _linux_discovery
else:
    _linux_discovery = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio


_FTDI_INFO = PortInfo(
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


@pytest.fixture
async def open_pair() -> AsyncIterator[tuple[MockBackend, MockBackend]]:
    a, b = MockBackend.pair()
    cfg = SerialConfig()
    a.open(a.path, cfg)
    b.open(b.path, cfg)
    try:
        yield a, b
    finally:
        a.close()
        b.close()


class TestPortInfoOnConstructor:
    async def test_port_info_property_returns_value_when_supplied(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        backend_a, _backend_b = open_pair
        port = SerialPort(backend_a, SerialConfig(), port_info=_FTDI_INFO)
        try:
            assert port.port_info == _FTDI_INFO
        finally:
            await port.aclose()

    async def test_port_info_property_returns_none_by_default(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        backend_a, _backend_b = open_pair
        port = SerialPort(backend_a, SerialConfig())
        try:
            assert port.port_info is None
        finally:
            await port.aclose()


class TestExtraAttributesLookup:
    async def test_extra_returns_supplied_port_info(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        backend_a, _backend_b = open_pair
        port = SerialPort(backend_a, SerialConfig(), port_info=_FTDI_INFO)
        try:
            assert port.extra(SerialStreamAttribute.port_info) == _FTDI_INFO
        finally:
            await port.aclose()

    async def test_extra_lookup_raises_when_port_info_absent(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        backend_a, _backend_b = open_pair
        port = SerialPort(backend_a, SerialConfig())
        try:
            with pytest.raises(anyio.TypedAttributeLookupError):
                port.extra(SerialStreamAttribute.port_info)
        finally:
            await port.aclose()

    async def test_extra_default_returned_when_port_info_absent(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        # AnyIO's `extra(key, default)` form swallows the lookup error and
        # returns the sentinel — mirror what a downstream caller would do.
        backend_a, _backend_b = open_pair
        port = SerialPort(backend_a, SerialConfig())
        try:
            sentinel = object()
            assert port.extra(SerialStreamAttribute.port_info, sentinel) is sentinel
        finally:
            await port.aclose()

    async def test_capabilities_and_config_still_present_alongside_port_info(
        self,
        open_pair: tuple[MockBackend, MockBackend],
    ) -> None:
        # Regression guard: the conditional include for port_info must not
        # drop the always-present capabilities / config keys.
        backend_a, _backend_b = open_pair
        cfg = SerialConfig(baudrate=19200)
        port = SerialPort(backend_a, cfg, port_info=_FTDI_INFO)
        try:
            assert port.extra(SerialStreamAttribute.config) is cfg
            caps = port.extra(SerialStreamAttribute.capabilities)
            assert caps.backend == "mock"
        finally:
            await port.aclose()


class TestResolvePortInfoHelper:
    def test_unrecognised_path_returns_none(self, tmp_path: object) -> None:
        # An unknown path that maps to nothing under /sys/class/tty.
        # MockBackend paths like "/dev/mockA" exercise this naturally.
        assert _resolve_port_info_for_path("/dev/mockA") is None

    def test_pty_path_returns_none(self) -> None:
        # /dev/pts/N entries don't appear under /sys/class/tty — Path.name
        # is just the digit, which never matches a tty class entry.
        assert _resolve_port_info_for_path("/dev/pts/0") is None

    def test_empty_path_returns_none(self) -> None:
        assert _resolve_port_info_for_path("") is None

    def test_swallows_oserror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Contract: never raise. Even if the platform resolver throws,
        # the public dispatcher converts it to None.
        if not sys.platform.startswith("linux"):
            pytest.skip("OSError swallowing only matters where Linux dispatch fires")

        def _boom(*_a: object, **_kw: object) -> None:
            raise OSError("simulated sysfs failure")

        monkeypatch.setattr(_linux_discovery, "resolve_port_info", _boom)
        assert _resolve_port_info_for_path("/dev/ttyUSB0") is None
