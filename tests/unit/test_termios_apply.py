# pyright: reportPrivateUsage=false
"""Unit tests for the pure POSIX termios builders.

These tests do not open a real tty. They construct a synthetic
:class:`TermiosAttrs` and verify that each builder flips exactly the right
bits. The tests run on any POSIX host (Linux, macOS, BSD); Windows is skipped
because :mod:`termios` is unavailable there.
"""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    pytest.skip("termios is POSIX-only", allow_module_level=True)

import termios

from anyserial._posix import termios_apply as _termios_apply
from anyserial._posix.termios_apply import (
    TermiosAttrs,
    apply_byte_size,
    apply_flow_control,
    apply_hangup,
    apply_parity,
    apply_raw_mode,
    apply_stop_bits,
)
from anyserial._types import ByteSize, Parity, StopBits
from anyserial.config import FlowControl
from anyserial.exceptions import UnsupportedFeatureError

# Control-chars array length. 32 is the Linux NCCS; macOS uses 20 but still
# fits every VMIN/VTIME index we touch, so 32 works on every platform.
_NCCS = 32

# Use the module-resolved CMSPAR — on Linux this is the hardcoded fallback
# (0o10000000000) since stdlib termios omits the constant; on macOS it is 0.
_CMSPAR = _termios_apply._CMSPAR
_HAS_CMSPAR = bool(_CMSPAR)


def _blank_attrs(
    *,
    iflag: int = 0,
    oflag: int = 0,
    cflag: int = 0,
    lflag: int = 0,
) -> TermiosAttrs:
    """Build a minimal TermiosAttrs with zeroed control chars."""
    return TermiosAttrs(
        iflag=iflag,
        oflag=oflag,
        cflag=cflag,
        lflag=lflag,
        ispeed=termios.B0,
        ospeed=termios.B0,
        cc=(0,) * _NCCS,
    )


def _rts_cts_mask() -> int:
    """Mirror the helper in termios_apply to keep tests independent."""
    crtscts = int(getattr(termios, "CRTSCTS", 0))
    if crtscts:
        return crtscts
    ccts = int(getattr(termios, "CCTS_OFLOW", 0))
    crts = int(getattr(termios, "CRTS_IFLOW", 0))
    return (ccts | crts) if (ccts and crts) else 0


# ---------------------------------------------------------------------------
# TermiosAttrs
# ---------------------------------------------------------------------------


class TestTermiosAttrs:
    def test_from_list_and_to_list_round_trip(self) -> None:
        raw: list[object] = [
            termios.IGNPAR,
            termios.OPOST,
            termios.CS8 | termios.CREAD,
            termios.ICANON,
            termios.B9600,
            termios.B9600,
            [0] * _NCCS,
        ]
        attrs = TermiosAttrs.from_list(raw)
        assert attrs.iflag == termios.IGNPAR
        assert attrs.oflag == termios.OPOST
        assert attrs.cflag == termios.CS8 | termios.CREAD
        assert attrs.lflag == termios.ICANON
        assert attrs.cc == (0,) * _NCCS

        round_tripped = attrs.to_list()
        assert round_tripped[:6] == raw[:6]
        assert round_tripped[6] == list(attrs.cc)
        # to_list must return fresh mutable objects so callers can't mutate
        # internal state by accident.
        assert round_tripped[6] is not attrs.cc

    def test_with_changes_returns_new_instance(self) -> None:
        original = _blank_attrs(cflag=termios.CS5)
        changed = original.with_changes(cflag=termios.CS8)
        assert original.cflag == termios.CS5
        assert changed.cflag == termios.CS8
        assert changed is not original

    def test_is_hashable(self) -> None:
        a = _blank_attrs()
        b = _blank_attrs()
        # Frozen + slots gives us structural equality and hashing for free.
        assert a == b
        assert hash(a) == hash(b)
        assert {a} == {b}


# ---------------------------------------------------------------------------
# apply_raw_mode
# ---------------------------------------------------------------------------


class TestApplyRawMode:
    def test_clears_input_processing_flags(self) -> None:
        noisy = _blank_attrs(
            iflag=termios.BRKINT | termios.ICRNL | termios.IXON | termios.ISTRIP,
        )
        result = apply_raw_mode(noisy)
        for bit in (termios.BRKINT, termios.ICRNL, termios.IXON, termios.ISTRIP):
            assert result.iflag & bit == 0

    def test_clears_output_post_processing(self) -> None:
        result = apply_raw_mode(_blank_attrs(oflag=termios.OPOST))
        assert result.oflag & termios.OPOST == 0

    def test_clears_line_discipline_flags(self) -> None:
        noisy = _blank_attrs(
            lflag=termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN,
        )
        result = apply_raw_mode(noisy)
        for bit in (termios.ECHO, termios.ICANON, termios.ISIG, termios.IEXTEN):
            assert result.lflag & bit == 0

    def test_sets_cs8_cread_clocal_and_clears_parenb(self) -> None:
        prior = _blank_attrs(cflag=termios.CS5 | termios.PARENB)
        result = apply_raw_mode(prior)
        assert result.cflag & termios.CSIZE == termios.CS8
        assert result.cflag & termios.CREAD == termios.CREAD
        assert result.cflag & termios.CLOCAL == termios.CLOCAL
        assert result.cflag & termios.PARENB == 0

    def test_sets_vmin_one_vtime_zero(self) -> None:
        result = apply_raw_mode(_blank_attrs())
        assert result.cc[termios.VMIN] == 1
        assert result.cc[termios.VTIME] == 0

    def test_is_idempotent(self) -> None:
        once = apply_raw_mode(_blank_attrs())
        twice = apply_raw_mode(once)
        assert once == twice


# ---------------------------------------------------------------------------
# apply_byte_size
# ---------------------------------------------------------------------------


class TestApplyByteSize:
    @pytest.mark.parametrize(
        ("byte_size", "expected"),
        [
            (ByteSize.FIVE, termios.CS5),
            (ByteSize.SIX, termios.CS6),
            (ByteSize.SEVEN, termios.CS7),
            (ByteSize.EIGHT, termios.CS8),
        ],
    )
    def test_sets_expected_bits(self, byte_size: ByteSize, expected: int) -> None:
        result = apply_byte_size(_blank_attrs(cflag=termios.CS5), byte_size)
        assert result.cflag & termios.CSIZE == expected

    def test_preserves_other_cflag_bits(self) -> None:
        prior = _blank_attrs(cflag=termios.CREAD | termios.CLOCAL | termios.CS5)
        result = apply_byte_size(prior, ByteSize.EIGHT)
        assert result.cflag & termios.CREAD
        assert result.cflag & termios.CLOCAL


# ---------------------------------------------------------------------------
# apply_parity
# ---------------------------------------------------------------------------


class TestApplyParity:
    def test_none_clears_parenb_and_parodd(self) -> None:
        prior = _blank_attrs(cflag=termios.PARENB | termios.PARODD)
        result = apply_parity(prior, Parity.NONE)
        assert result.cflag & termios.PARENB == 0
        assert result.cflag & termios.PARODD == 0

    def test_even_sets_parenb_clears_parodd(self) -> None:
        result = apply_parity(_blank_attrs(cflag=termios.PARODD), Parity.EVEN)
        assert result.cflag & termios.PARENB
        assert result.cflag & termios.PARODD == 0

    def test_odd_sets_parenb_and_parodd(self) -> None:
        result = apply_parity(_blank_attrs(), Parity.ODD)
        assert result.cflag & termios.PARENB
        assert result.cflag & termios.PARODD

    @pytest.mark.skipif(not _HAS_CMSPAR, reason="platform termios lacks CMSPAR")
    def test_mark_sets_parenb_parodd_and_cmspar(self) -> None:
        result = apply_parity(_blank_attrs(), Parity.MARK)
        assert result.cflag & termios.PARENB
        assert result.cflag & termios.PARODD
        assert result.cflag & _CMSPAR

    @pytest.mark.skipif(not _HAS_CMSPAR, reason="platform termios lacks CMSPAR")
    def test_space_sets_parenb_and_cmspar_clears_parodd(self) -> None:
        prior = _blank_attrs(cflag=termios.PARODD)
        result = apply_parity(prior, Parity.SPACE)
        assert result.cflag & termios.PARENB
        assert result.cflag & termios.PARODD == 0
        assert result.cflag & _CMSPAR

    @pytest.mark.skipif(_HAS_CMSPAR, reason="platform exposes CMSPAR; skip the negative case")
    def test_mark_raises_without_cmspar(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="CMSPAR"):
            apply_parity(_blank_attrs(), Parity.MARK)

    @pytest.mark.skipif(_HAS_CMSPAR, reason="platform exposes CMSPAR; skip the negative case")
    def test_space_raises_without_cmspar(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="CMSPAR"):
            apply_parity(_blank_attrs(), Parity.SPACE)


# ---------------------------------------------------------------------------
# apply_stop_bits
# ---------------------------------------------------------------------------


class TestApplyStopBits:
    def test_one_clears_cstopb(self) -> None:
        result = apply_stop_bits(_blank_attrs(cflag=termios.CSTOPB), StopBits.ONE)
        assert result.cflag & termios.CSTOPB == 0

    def test_two_sets_cstopb(self) -> None:
        result = apply_stop_bits(_blank_attrs(), StopBits.TWO)
        assert result.cflag & termios.CSTOPB

    def test_one_point_five_raises(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match=r"1\.5 stop bits"):
            apply_stop_bits(_blank_attrs(), StopBits.ONE_POINT_FIVE)


# ---------------------------------------------------------------------------
# apply_flow_control
# ---------------------------------------------------------------------------


class TestApplyFlowControl:
    def test_none_clears_all_flow_bits(self) -> None:
        mask = _rts_cts_mask()
        prior = _blank_attrs(
            iflag=termios.IXON | termios.IXOFF | termios.IXANY,
            cflag=mask,
        )
        result = apply_flow_control(prior, FlowControl())
        assert result.iflag & (termios.IXON | termios.IXOFF | termios.IXANY) == 0
        assert result.cflag & mask == 0

    def test_xon_xoff_sets_ixon_ixoff(self) -> None:
        result = apply_flow_control(_blank_attrs(), FlowControl(xon_xoff=True))
        assert result.iflag & termios.IXON
        assert result.iflag & termios.IXOFF
        # IXANY is always cleared — rarely what a binary protocol wants.
        assert result.iflag & termios.IXANY == 0

    def test_clears_ixany_even_when_xon_xoff_is_requested(self) -> None:
        prior = _blank_attrs(iflag=termios.IXANY)
        result = apply_flow_control(prior, FlowControl(xon_xoff=True))
        assert result.iflag & termios.IXANY == 0

    @pytest.mark.skipif(not _rts_cts_mask(), reason="no hardware handshake bits on this platform")
    def test_rts_cts_sets_platform_mask(self) -> None:
        mask = _rts_cts_mask()
        result = apply_flow_control(_blank_attrs(), FlowControl(rts_cts=True))
        assert result.cflag & mask == mask

    @pytest.mark.skipif(
        bool(_rts_cts_mask()),
        reason="platform exposes hardware handshake; skip negative",
    )
    def test_rts_cts_raises_without_platform_bits(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="RTS/CTS"):
            apply_flow_control(_blank_attrs(), FlowControl(rts_cts=True))

    def test_dtr_dsr_always_raises(self) -> None:
        with pytest.raises(UnsupportedFeatureError, match="DTR/DSR"):
            apply_flow_control(_blank_attrs(), FlowControl(dtr_dsr=True))


# ---------------------------------------------------------------------------
# apply_hangup
# ---------------------------------------------------------------------------


class TestApplyHangup:
    def test_true_sets_hupcl(self) -> None:
        result = apply_hangup(_blank_attrs(), hangup_on_close=True)
        assert result.cflag & termios.HUPCL

    def test_false_clears_hupcl(self) -> None:
        result = apply_hangup(_blank_attrs(cflag=termios.HUPCL), hangup_on_close=False)
        assert result.cflag & termios.HUPCL == 0


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


class TestComposition:
    def test_chained_builders_produce_expected_shape(self) -> None:
        """Compose the full config pipeline and spot-check critical bits.

        Mirrors how the backend orchestrator (DESIGN §16) will use the
        builders: starting from a raw tcgetattr result, run the requested
        transformations end-to-end, then hand the result to tcsetattr.
        """
        prior = _blank_attrs(
            iflag=termios.ICRNL | termios.IXON,
            oflag=termios.OPOST,
            cflag=termios.CS5 | termios.PARODD | termios.CSTOPB,
            lflag=termios.ICANON | termios.ECHO,
        )
        step1 = apply_raw_mode(prior)
        step2 = apply_byte_size(step1, ByteSize.EIGHT)
        step3 = apply_parity(step2, Parity.NONE)
        step4 = apply_stop_bits(step3, StopBits.ONE)
        step5 = apply_flow_control(step4, FlowControl())
        result = apply_hangup(step5, hangup_on_close=True)

        # Input cooking fully disabled.
        assert result.iflag & (termios.ICRNL | termios.IXON) == 0
        # Output post-processing off.
        assert result.oflag & termios.OPOST == 0
        # Canonical / echo off.
        assert result.lflag & (termios.ICANON | termios.ECHO) == 0
        # 8N1, receiver enabled, ignore modem control, HUPCL on.
        assert result.cflag & termios.CSIZE == termios.CS8
        assert result.cflag & termios.PARENB == 0
        assert result.cflag & termios.CSTOPB == 0
        assert result.cflag & termios.CREAD
        assert result.cflag & termios.CLOCAL
        assert result.cflag & termios.HUPCL
        # Raw mode set VMIN=1, VTIME=0.
        assert result.cc[termios.VMIN] == 1
        assert result.cc[termios.VTIME] == 0
