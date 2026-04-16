"""Tests for :mod:`anyserial.capabilities`."""

from __future__ import annotations

from anyserial._types import Capability
from anyserial.capabilities import SerialCapabilities, SerialStreamAttribute


def _all_unknown(platform: str = "test", backend: str = "mock") -> SerialCapabilities:
    """Helper: capabilities snapshot with every tri-state at ``UNKNOWN``."""
    return SerialCapabilities(
        platform=platform,
        backend=backend,
        custom_baudrate=Capability.UNKNOWN,
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


class TestSerialCapabilities:
    def test_immutable(self) -> None:
        caps = _all_unknown()
        try:
            caps.platform = "other"  # type: ignore[misc]
        except AttributeError:
            return
        msg = "SerialCapabilities must be frozen"
        raise AssertionError(msg)

    def test_round_trip(self) -> None:
        caps = _all_unknown(platform="linux", backend="linux")
        assert caps.platform == "linux"
        assert caps.custom_baudrate is Capability.UNKNOWN


class TestSerialStreamAttribute:
    def test_attributes_are_distinct_sentinels(self) -> None:
        # ``typed_attribute`` returns a unique sentinel per slot; if the two
        # attributes aliased each other we'd never be able to distinguish
        # ``config`` from ``capabilities`` in ``extra(...)``.
        cap: object = SerialStreamAttribute.capabilities
        cfg: object = SerialStreamAttribute.config
        assert cap is not cfg
