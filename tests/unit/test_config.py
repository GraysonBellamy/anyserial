"""Tests for :mod:`anyserial.config`."""

from __future__ import annotations

import pytest

from anyserial._types import ByteSize, Parity, StopBits, UnsupportedPolicy
from anyserial.config import FlowControl, RS485Config, SerialConfig
from anyserial.exceptions import ConfigurationError


class TestFlowControl:
    def test_defaults_all_off(self) -> None:
        fc = FlowControl()
        assert fc.xon_xoff is False
        assert fc.rts_cts is False
        assert fc.dtr_dsr is False

    def test_none_classmethod(self) -> None:
        assert FlowControl.none() == FlowControl()

    def test_frozen(self) -> None:
        fc = FlowControl()
        with pytest.raises(AttributeError):
            fc.xon_xoff = True  # type: ignore[misc]


class TestRS485Config:
    def test_defaults(self) -> None:
        cfg = RS485Config()
        assert cfg.enabled is True
        assert cfg.rts_on_send is True
        assert cfg.delay_before_send == 0.0

    def test_rejects_negative_delay_before(self) -> None:
        with pytest.raises(ConfigurationError):
            RS485Config(delay_before_send=-0.001)

    def test_rejects_negative_delay_after(self) -> None:
        with pytest.raises(ConfigurationError):
            RS485Config(delay_after_send=-1.0)


class TestSerialConfig:
    def test_defaults(self) -> None:
        cfg = SerialConfig()
        assert cfg.baudrate == 115_200
        assert cfg.byte_size is ByteSize.EIGHT
        assert cfg.parity is Parity.NONE
        assert cfg.stop_bits is StopBits.ONE
        assert cfg.flow_control == FlowControl()
        assert cfg.exclusive is False
        assert cfg.hangup_on_close is True
        assert cfg.low_latency is False
        assert cfg.rs485 is None
        assert cfg.unsupported_policy is UnsupportedPolicy.RAISE

    def test_rejects_non_positive_baud(self) -> None:
        with pytest.raises(ConfigurationError):
            SerialConfig(baudrate=0)
        with pytest.raises(ConfigurationError):
            SerialConfig(baudrate=-9600)

    @pytest.mark.parametrize("size", [0, 63, 1 << 25])
    def test_rejects_bad_chunk_size(self, size: int) -> None:
        with pytest.raises(ConfigurationError):
            SerialConfig(read_chunk_size=size)

    def test_accepts_boundary_chunk_sizes(self) -> None:
        SerialConfig(read_chunk_size=64)
        SerialConfig(read_chunk_size=1 << 24)

    def test_with_changes_revalidates(self) -> None:
        base = SerialConfig(baudrate=9600)
        assert base.with_changes(baudrate=1_000_000).baudrate == 1_000_000
        with pytest.raises(ConfigurationError):
            base.with_changes(baudrate=0)

    def test_is_hashable(self) -> None:
        a = SerialConfig()
        b = SerialConfig()
        assert hash(a) == hash(b)

    def test_frozen(self) -> None:
        cfg = SerialConfig()
        with pytest.raises(AttributeError):
            cfg.baudrate = 9600  # type: ignore[misc]

    def test_rs485_roundtrip(self) -> None:
        cfg = SerialConfig(rs485=RS485Config(delay_before_send=0.001))
        assert cfg.rs485 is not None
        assert cfg.rs485.delay_before_send == 0.001
