"""Static-typing assertions for the public ``anyserial`` API.

These tests use :func:`typing.assert_type` to pin the shape of the
public surface. ``mypy`` and ``pyright`` fail the CI job if a signature
regresses.

Pattern: each "shape test" is either

- a plain function that constructs real values and asserts on them
  (enums, configs, dataclasses), OR
- a function that contains a nested ``_type_check_only`` helper. The
  helper's body is inspected by the type checker but the helper is
  never called at runtime — that lets us reference methods on a
  :class:`SerialPort` we never actually construct or open.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, assert_type

from anyio.streams.file import FileStreamAttribute

import anyserial
from anyserial import (
    ByteSize,
    BytesLike,
    Capability,
    ConfigurationError,
    ControlLines,
    FlowControl,
    ModemLines,
    Parity,
    PortBusyError,
    PortNotFoundError,
    RS485Config,
    SerialCapabilities,
    SerialClosedError,
    SerialConfig,
    SerialConnectable,
    SerialDisconnectedError,
    SerialError,
    SerialPort,
    SerialStreamAttribute,
    StopBits,
    UnsupportedConfigurationError,
    UnsupportedFeatureError,
    UnsupportedPolicy,
)

if TYPE_CHECKING:
    import anyio.abc


def test_enums_are_strenums() -> None:
    # Enum *types* are already covered indirectly — every field that
    # carries one (``SerialConfig.parity``, ``.byte_size``, ``.stop_bits``,
    # ``.unsupported_policy``, ``SerialCapabilities.*``) has its own
    # ``assert_type`` below. Here we just verify the StrEnum runtime
    # contract: members are also ``str``.
    assert isinstance(Parity.NONE, str)
    assert isinstance(ByteSize.EIGHT, str)
    assert isinstance(StopBits.ONE, str)
    assert isinstance(Capability.SUPPORTED, str)
    assert isinstance(UnsupportedPolicy.RAISE, str)
    # And serialize as their value.
    v: str = Parity.NONE
    assert v == "none"


def test_config_fields() -> None:
    cfg = SerialConfig()
    assert_type(cfg.baudrate, int)
    assert_type(cfg.byte_size, ByteSize)
    assert_type(cfg.parity, Parity)
    assert_type(cfg.stop_bits, StopBits)
    assert_type(cfg.flow_control, FlowControl)
    assert_type(cfg.exclusive, bool)
    assert_type(cfg.hangup_on_close, bool)
    assert_type(cfg.low_latency, bool)
    assert_type(cfg.read_chunk_size, int)
    assert_type(cfg.rs485, RS485Config | None)
    assert_type(cfg.unsupported_policy, UnsupportedPolicy)


def test_flow_control_fields() -> None:
    fc = FlowControl()
    assert_type(fc.xon_xoff, bool)
    assert_type(fc.rts_cts, bool)
    assert_type(fc.dtr_dsr, bool)
    assert_type(FlowControl.none(), FlowControl)


def test_rs485_fields() -> None:
    rs = RS485Config()
    assert_type(rs.enabled, bool)
    assert_type(rs.delay_before_send, float)
    assert_type(rs.rx_during_tx, bool)


def test_modem_and_control_lines() -> None:
    assert_type(ModemLines(cts=False, dsr=False, ri=False, cd=False), ModemLines)
    assert_type(ControlLines(rts=False, dtr=False), ControlLines)


def test_bytes_like_alias() -> None:
    # ``BytesLike`` is a type alias for ``collections.abc.Buffer``; any
    # object satisfying the buffer protocol is assignable to it.
    buf: BytesLike = b"bytes"
    buf = bytearray(b"array")
    buf = memoryview(b"view")
    assert buf is not None


def test_serial_connectable_shape() -> None:
    conn = SerialConnectable(path="/dev/x")
    assert_type(conn.path, str)
    assert_type(conn.config, SerialConfig)
    # SerialConnectable must satisfy the AnyIO connectable interface.
    connectable: anyio.abc.ByteStreamConnectable = conn
    assert connectable is conn


def test_open_serial_port_signature() -> None:
    # ``_shape`` is an async helper that's defined but never called: the
    # coroutine is never constructed, so pyright's "unused coroutine"
    # lint stays quiet. Type checkers still inspect the body.
    async def _shape() -> None:
        port = await anyserial.open_serial_port("/dev/x")
        assert_type(port, SerialPort)

    assert _shape is not None


def test_serial_connectable_connect_signature() -> None:
    async def _shape(conn: SerialConnectable) -> None:
        assert_type(await conn.connect(), SerialPort)

    assert _shape is not None


def test_serial_port_is_byte_stream_subtype() -> None:
    def _shape(port: SerialPort) -> anyio.abc.ByteStream:
        return port

    assert _shape is not None


def test_serial_port_property_types() -> None:
    def _shape(port: SerialPort) -> None:
        assert_type(port.path, str)
        assert_type(port.is_open, bool)
        assert_type(port.config, SerialConfig)
        assert_type(port.capabilities, SerialCapabilities)
        assert_type(port.extra_attributes, Mapping[Any, Callable[[], Any]])

    assert _shape is not None


def test_serial_port_method_signatures() -> None:
    # Defined-but-never-called async helper so each ``await`` expression
    # can be type-checked against the method's declared return type.
    async def _shape(port: SerialPort) -> None:
        assert_type(await port.receive(10), bytes)
        assert_type(await port.receive(), bytes)
        assert_type(await port.send(b"x"), None)
        assert_type(await port.send_buffer(bytearray(b"x")), None)
        assert_type(await port.receive_into(bytearray(8)), int)
        assert_type(await port.receive_available(), bytes)
        assert_type(await port.receive_available(limit=64), bytes)
        assert_type(await port.configure(SerialConfig()), None)
        assert_type(await port.modem_lines(), ModemLines)
        assert_type(await port.set_control_lines(rts=True, dtr=None), None)
        assert_type(await port.drain(), None)
        assert_type(await port.drain_exact(), None)
        assert_type(await port.send_break(0.1), None)
        assert_type(await port.send_eof(), None)
        assert_type(await port.reset_input_buffer(), None)
        assert_type(await port.reset_output_buffer(), None)
        assert_type(await port.aclose(), None)
        # Non-awaiting snapshots.
        assert_type(port.input_waiting(), int)
        assert_type(port.output_waiting(), int)

    assert _shape is not None


def test_serial_port_extra_attribute_return_types() -> None:
    def _shape(port: SerialPort) -> None:
        assert_type(port.extra(FileStreamAttribute.fileno), int)
        assert_type(port.extra(FileStreamAttribute.path), Path)
        assert_type(port.extra(SerialStreamAttribute.capabilities), SerialCapabilities)
        assert_type(port.extra(SerialStreamAttribute.config), SerialConfig)

    assert _shape is not None


def test_exception_inheritance_is_type_visible() -> None:
    # OSError-based classes carry errno / strerror / filename attributes.
    def _shape(err: SerialError) -> None:
        assert_type(err.errno, int | None)
        assert_type(err.strerror, str | None)

    # Every domain exception is a SerialError at the type level.
    domain: tuple[type[SerialError], ...] = (
        ConfigurationError,
        PortBusyError,
        PortNotFoundError,
        UnsupportedFeatureError,
        UnsupportedConfigurationError,
        SerialClosedError,
        SerialDisconnectedError,
    )
    assert all(issubclass(c, SerialError) for c in domain)
    assert _shape is not None


def test_serial_capabilities_fields() -> None:
    caps = SerialCapabilities(
        platform="p",
        backend="b",
        custom_baudrate=Capability.SUPPORTED,
        mark_space_parity=Capability.UNKNOWN,
        one_point_five_stop_bits=Capability.UNKNOWN,
        xon_xoff=Capability.UNKNOWN,
        rts_cts=Capability.UNKNOWN,
        dtr_dsr=Capability.UNKNOWN,
        modem_lines=Capability.UNKNOWN,
        break_signal=Capability.UNKNOWN,
        exclusive_access=Capability.UNKNOWN,
        low_latency=Capability.UNKNOWN,
        rs485=Capability.UNKNOWN,
        input_waiting=Capability.UNKNOWN,
        output_waiting=Capability.UNKNOWN,
        port_discovery=Capability.UNKNOWN,
    )
    assert_type(caps.platform, str)
    assert_type(caps.custom_baudrate, Capability)


def test_top_level_reexports_are_present() -> None:
    # Every name in the design §7.6 must be importable from top-level.
    expected = {
        "ByteSize",
        "BytesLike",
        "Capability",
        "ConfigurationError",
        "ControlLines",
        "FlowControl",
        "ModemLines",
        "Parity",
        "PortBusyError",
        "PortNotFoundError",
        "RS485Config",
        "SerialCapabilities",
        "SerialClosedError",
        "SerialConfig",
        "SerialConnectable",
        "SerialDisconnectedError",
        "SerialError",
        "SerialPort",
        "SerialStreamAttribute",
        "StopBits",
        "UnsupportedConfigurationError",
        "UnsupportedFeatureError",
        "UnsupportedPolicy",
        "__version__",
        "open_serial_port",
    }
    missing = expected - set(anyserial.__all__)
    assert not missing, f"missing from anyserial.__all__: {sorted(missing)}"
