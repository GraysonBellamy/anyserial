"""Round-trip tests for ``SerialConfig`` ↔ ``DCB`` translation.

Every flow-control combination is exercised so a regression in
:mod:`anyserial._windows.dcb` (e.g. forgetting to set ``fOutX`` /
``fInX`` together) surfaces immediately. The tests run on Linux because
``DCB`` is a pure ctypes structure — no Win32 calls.

design-windows-backend.md §6.2 invariants are pinned at the bottom.
"""

from __future__ import annotations

from ctypes import sizeof
from itertools import product

import pytest

from anyserial._types import ByteSize, Parity, StopBits
from anyserial._windows import _win32 as w
from anyserial._windows.dcb import apply_config, build_dcb, build_read_any_timeouts, read_dcb
from anyserial.config import FlowControl, SerialConfig


def _round_trip(config: SerialConfig) -> dict[str, object]:
    return read_dcb(build_dcb(config))


class TestInvariants:
    def test_dcblength_is_sizeof_dcb(self) -> None:
        dcb = build_dcb(SerialConfig())
        assert dcb.DCBlength == sizeof(w.DCB) == 28

    def test_fbinary_always_one(self) -> None:
        # design-windows-backend.md §6.2: Windows documents non-binary
        # mode as unsupported. We must always set fBinary = 1.
        for config in (
            SerialConfig(),
            SerialConfig(parity=Parity.EVEN),
            SerialConfig(flow_control=FlowControl(rts_cts=True)),
            SerialConfig(flow_control=FlowControl(xon_xoff=True, dtr_dsr=True)),
        ):
            assert build_dcb(config).fBinary == 1

    def test_fabortonerror_always_zero(self) -> None:
        for config in (
            SerialConfig(),
            SerialConfig(flow_control=FlowControl(rts_cts=True, xon_xoff=True)),
        ):
            assert build_dcb(config).fAbortOnError == 0


class TestApplyConfig:
    """Verify that :func:`apply_config` mutates a DCB in place."""

    def test_overlay_preserves_existing_reserved_bytes(self) -> None:
        dcb = w.DCB()
        # Simulate a driver putting a non-zero value in wReserved1 — a
        # field we don't own. apply_config should leave it untouched.
        dcb.wReserved1 = 0xBEEF
        apply_config(dcb, SerialConfig())
        assert dcb.wReserved1 == 0xBEEF
        # But the fields we own are set correctly.
        assert dcb.fBinary == 1
        assert dcb.BaudRate == 115200  # SerialConfig default

    def test_apply_config_matches_build_dcb(self) -> None:
        config = SerialConfig(
            baudrate=115200,
            parity=Parity.EVEN,
            flow_control=FlowControl(rts_cts=True),
        )
        dcb_build = build_dcb(config)
        dcb_apply = w.DCB()
        apply_config(dcb_apply, config)
        assert read_dcb(dcb_apply) == read_dcb(dcb_build)


class TestRoundTrip:
    @pytest.mark.parametrize("baudrate", [9600, 115_200, 921_600, 1, 0xFFFFFFFF])
    def test_baudrate_passthrough(self, baudrate: int) -> None:
        out = _round_trip(SerialConfig(baudrate=baudrate))
        assert out["baudrate"] == baudrate

    @pytest.mark.parametrize(
        "byte_size",
        [ByteSize.FIVE, ByteSize.SIX, ByteSize.SEVEN, ByteSize.EIGHT],
    )
    def test_byte_size_round_trip(self, byte_size: ByteSize) -> None:
        out = _round_trip(SerialConfig(byte_size=byte_size))
        assert out["byte_size"] == byte_size

    @pytest.mark.parametrize(
        "parity",
        [Parity.NONE, Parity.ODD, Parity.EVEN, Parity.MARK, Parity.SPACE],
    )
    def test_parity_round_trip(self, parity: Parity) -> None:
        out = _round_trip(SerialConfig(parity=parity))
        assert out["parity"] == parity
        # f_parity bit is set iff parity is enabled.
        assert out["f_parity"] == (0 if parity is Parity.NONE else 1)

    @pytest.mark.parametrize(
        "stop_bits",
        [StopBits.ONE, StopBits.ONE_POINT_FIVE, StopBits.TWO],
    )
    def test_stop_bits_round_trip(self, stop_bits: StopBits) -> None:
        out = _round_trip(SerialConfig(stop_bits=stop_bits))
        assert out["stop_bits"] == stop_bits


class TestFlowControl:
    @pytest.mark.parametrize(
        ("xon_xoff", "rts_cts", "dtr_dsr"),
        list(product([False, True], repeat=3)),
    )
    def test_every_combination_round_trips(
        self,
        xon_xoff: bool,
        rts_cts: bool,
        dtr_dsr: bool,
    ) -> None:
        flow = FlowControl(xon_xoff=xon_xoff, rts_cts=rts_cts, dtr_dsr=dtr_dsr)
        out = _round_trip(SerialConfig(flow_control=flow))
        assert out["xon_xoff"] is xon_xoff
        assert out["rts_cts"] is rts_cts
        assert out["dtr_dsr"] is dtr_dsr

    def test_xon_xoff_sets_paired_flags(self) -> None:
        # Forgetting fOutX or fInX would silently make the port half-deaf
        # to flow control. Pin both bits.
        dcb = build_dcb(SerialConfig(flow_control=FlowControl(xon_xoff=True)))
        assert dcb.fOutX == 1
        assert dcb.fInX == 1
        # Default Xon/Xoff chars (PC convention).
        assert dcb.XonChar == bytes([w.XON_CHAR])
        assert dcb.XoffChar == bytes([w.XOFF_CHAR])

    def test_rts_cts_sets_handshake_mode(self) -> None:
        dcb = build_dcb(SerialConfig(flow_control=FlowControl(rts_cts=True)))
        assert dcb.fOutxCtsFlow == 1
        assert dcb.fRtsControl == w.RTS_CONTROL_HANDSHAKE

    def test_dtr_dsr_sets_handshake_mode(self) -> None:
        dcb = build_dcb(SerialConfig(flow_control=FlowControl(dtr_dsr=True)))
        assert dcb.fOutxDsrFlow == 1
        assert dcb.fDtrControl == w.DTR_CONTROL_HANDSHAKE

    def test_no_flow_control_uses_enabled_lines(self) -> None:
        # design-windows-backend.md §6.2: when no hardware handshaking is
        # requested, RTS / DTR are driven steady-on rather than left in
        # an undefined state.
        dcb = build_dcb(SerialConfig(flow_control=FlowControl()))
        assert dcb.fOutxCtsFlow == 0
        assert dcb.fOutxDsrFlow == 0
        assert dcb.fRtsControl == w.RTS_CONTROL_ENABLE
        assert dcb.fDtrControl == w.DTR_CONTROL_ENABLE


class TestReadAnyTimeouts:
    def test_wait_for_any_policy(self) -> None:
        # design-windows-backend.md §6.3: MAXDWORD / MAXDWORD / 1 is the
        # documented "wait for first byte, then return available" mode.
        # This matches ByteStream.receive(max_bytes) semantics.
        t = build_read_any_timeouts()
        assert t.ReadIntervalTimeout == w.MAXDWORD
        assert t.ReadTotalTimeoutMultiplier == w.MAXDWORD
        assert t.ReadTotalTimeoutConstant == 1
        assert t.WriteTotalTimeoutMultiplier == 0
        assert t.WriteTotalTimeoutConstant == 0

    def test_read_timeouts_are_not_all_zero(self) -> None:
        # Guard against regression to the old all-zero policy, which waits
        # for the full buffer instead of returning on first byte.
        t = build_read_any_timeouts()
        assert (t.ReadIntervalTimeout, t.ReadTotalTimeoutMultiplier) != (0, 0)
