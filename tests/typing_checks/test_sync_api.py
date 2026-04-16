"""Static-typing assertions for the ``anyserial.sync`` public API.

Mirrors :mod:`tests.typing_checks.test_public_api` but for the sync
wrapper. :func:`typing.assert_type` pins each method's return type so
regressions are caught by ``mypy`` / ``pyright`` in CI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, assert_type

from anyio.streams.file import FileStreamAttribute

from anyserial import (
    ModemLines,
    SerialCapabilities,
    SerialConfig,
    SerialStreamAttribute,
)
from anyserial.sync import (
    SerialConnectable,
    SerialPort,
    configure_portal,
    open_serial_port,
)


def test_open_serial_port_return_type() -> None:
    def _shape() -> None:
        assert_type(open_serial_port("/dev/x"), SerialPort)
        assert_type(open_serial_port("/dev/x", SerialConfig()), SerialPort)
        assert_type(open_serial_port("/dev/x", timeout=1.0), SerialPort)

    assert _shape is not None


def test_classmethod_open_return_type() -> None:
    def _shape() -> None:
        assert_type(SerialPort.open("/dev/x"), SerialPort)
        assert_type(SerialPort.open("/dev/x", baudrate=115200), SerialPort)

    assert _shape is not None


def test_property_types() -> None:
    def _shape(port: SerialPort) -> None:
        assert_type(port.path, str)
        assert_type(port.is_open, bool)
        assert_type(port.config, SerialConfig)
        assert_type(port.capabilities, SerialCapabilities)
        assert_type(port.extra_attributes, Mapping[Any, Callable[[], Any]])

    assert _shape is not None


def test_blocking_method_signatures() -> None:
    def _shape(port: SerialPort) -> None:
        assert_type(port.receive(10), bytes)
        assert_type(port.receive(10, timeout=1.0), bytes)
        assert_type(port.send(b"x"), None)
        assert_type(port.send(b"x", timeout=1.0), None)
        assert_type(port.send_buffer(bytearray(b"x")), None)
        assert_type(port.receive_into(bytearray(8)), int)
        assert_type(port.receive_available(), bytes)
        assert_type(port.receive_available(limit=32, timeout=0.5), bytes)
        assert_type(port.configure(SerialConfig()), None)
        assert_type(port.modem_lines(), ModemLines)
        assert_type(port.set_control_lines(rts=True), None)
        assert_type(port.drain(), None)
        assert_type(port.drain_exact(), None)
        assert_type(port.send_break(0.1), None)
        assert_type(port.send_eof(), None)
        assert_type(port.reset_input_buffer(), None)
        assert_type(port.reset_output_buffer(), None)
        assert_type(port.close(), None)
        assert_type(port.input_waiting(), int)
        assert_type(port.output_waiting(), int)

    assert _shape is not None


def test_extra_attribute_return_types() -> None:
    def _shape(port: SerialPort) -> None:
        assert_type(port.extra(FileStreamAttribute.fileno), int)
        assert_type(port.extra(FileStreamAttribute.path), Path)
        assert_type(port.extra(SerialStreamAttribute.capabilities), SerialCapabilities)
        assert_type(port.extra(SerialStreamAttribute.config), SerialConfig)

    assert _shape is not None


def test_context_manager_protocol() -> None:
    def _shape() -> None:
        with SerialPort.open("/dev/x") as port:
            assert_type(port, SerialPort)

    assert _shape is not None


def test_connectable_shape() -> None:
    def _shape() -> None:
        conn = SerialConnectable(path="/dev/x")
        assert_type(conn.path, str)
        assert_type(conn.config, SerialConfig)
        assert_type(conn.connect(), SerialPort)
        assert_type(conn.connect(timeout=1.0), SerialPort)

    assert _shape is not None


def test_configure_portal_signature() -> None:
    def _shape() -> None:
        assert_type(configure_portal(), None)
        assert_type(configure_portal(backend="asyncio", backend_options={"use_uvloop": True}), None)

    assert _shape is not None
