"""Unit tests for the Linux ``struct termios2`` packer and bitflag helpers."""

from __future__ import annotations

import sys

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

from anyserial._linux.baudrate import (
    BOTHER,
    CBAUD,
    TCGETS2,
    TCSETS2,
    Termios2Attrs,
    clear_cbaud,
    mark_bother,
)


class TestConstants:
    def test_ioctl_numbers_match_linux_abi(self) -> None:
        # Stable kernel ABI from <asm-generic/termbits.h>. If these ever
        # change, pySerial and every other userspace serial stack breaks
        # simultaneously — so asserting the literal is safe.
        assert TCGETS2 == 0x802C542A
        assert TCSETS2 == 0x402C542B

    def test_bother_and_cbaud_masks(self) -> None:
        assert BOTHER == 0o010000
        # BOTHER must live inside the CBAUD slot so clear_cbaud wipes it.
        assert CBAUD & BOTHER == BOTHER


class TestTermios2Attrs:
    def test_pack_unpack_round_trip(self) -> None:
        attrs = Termios2Attrs(
            iflag=0x1,
            oflag=0x2,
            cflag=0x3,
            lflag=0x4,
            line=5,
            cc=bytes(range(19)),
            ispeed=115200,
            ospeed=230400,
        )
        packed = attrs.pack()
        assert len(packed) == 44
        restored = Termios2Attrs.unpack(packed)
        assert restored == attrs

    def test_unpack_preserves_cc_bytes(self) -> None:
        raw = bytes(range(19))
        attrs = Termios2Attrs(
            iflag=0,
            oflag=0,
            cflag=0,
            lflag=0,
            line=0,
            cc=raw,
            ispeed=0,
            ospeed=0,
        )
        assert Termios2Attrs.unpack(attrs.pack()).cc == raw

    def test_with_changes_returns_new_instance(self) -> None:
        original = Termios2Attrs(
            iflag=0,
            oflag=0,
            cflag=0,
            lflag=0,
            line=0,
            cc=bytes(19),
            ispeed=9600,
            ospeed=9600,
        )
        changed = original.with_changes(ispeed=115200, ospeed=115200)
        assert original.ispeed == 9600
        assert changed.ispeed == 115200
        assert changed is not original


class TestBitHelpers:
    def test_clear_cbaud_wipes_every_baud_bit(self) -> None:
        # cflag = CBAUD bits set, plus some unrelated high bits we want kept.
        preserved = 1 << 20
        cflag = CBAUD | preserved
        assert clear_cbaud(cflag) == preserved

    def test_mark_bother_clears_legacy_then_sets_bother(self) -> None:
        # Legacy B115200 bits inside CBAUD must be cleared before BOTHER lands.
        legacy = 0o10002  # termios.B115200 shape — low + high halves
        other = 0xABCD_0000
        cflag = legacy | other
        result = mark_bother(cflag)
        assert result & CBAUD == BOTHER
        # Unrelated bits survive.
        assert result & other == other

    def test_mark_bother_is_idempotent(self) -> None:
        cflag = 0
        once = mark_bother(cflag)
        twice = mark_bother(once)
        assert once == twice
