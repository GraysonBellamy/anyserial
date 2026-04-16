"""Tests for :mod:`anyserial._types`."""

from __future__ import annotations

import json

from anyserial._types import (
    ByteSize,
    Capability,
    ControlLines,
    ModemLines,
    Parity,
    StopBits,
    UnsupportedPolicy,
)


class TestStrEnums:
    def test_bytesize_members(self) -> None:
        assert ByteSize.FIVE.value == "5"
        assert ByteSize.EIGHT.value == "8"
        assert set(ByteSize) == {ByteSize.FIVE, ByteSize.SIX, ByteSize.SEVEN, ByteSize.EIGHT}

    def test_parity_members(self) -> None:
        assert Parity.NONE.value == "none"
        assert Parity.MARK.value == "mark"
        assert Parity.SPACE.value == "space"

    def test_stopbits_members(self) -> None:
        assert StopBits.ONE.value == "1"
        assert StopBits.ONE_POINT_FIVE.value == "1.5"
        assert StopBits.TWO.value == "2"

    def test_capability_tri_state(self) -> None:
        assert Capability.SUPPORTED.value == "supported"
        assert Capability.UNSUPPORTED.value == "unsupported"
        assert Capability.UNKNOWN.value == "unknown"

    def test_unsupported_policy_members(self) -> None:
        assert UnsupportedPolicy.RAISE.value == "raise"
        assert UnsupportedPolicy.WARN.value == "warn"
        assert UnsupportedPolicy.IGNORE.value == "ignore"

    def test_strenum_is_str(self) -> None:
        # StrEnum instances must be usable wherever ``str`` is expected.
        assert isinstance(Parity.NONE, str)
        # And they must serialize as the value string.
        assert json.dumps({"parity": Parity.NONE}) == '{"parity": "none"}'


class TestModemLines:
    def test_immutable_and_kw_only(self) -> None:
        lines = ModemLines(cts=True, dsr=False, ri=False, cd=True)
        assert lines.cts is True
        assert lines.cd is True

    def test_equality(self) -> None:
        a = ModemLines(cts=True, dsr=False, ri=False, cd=True)
        b = ModemLines(cts=True, dsr=False, ri=False, cd=True)
        assert a == b


class TestControlLines:
    def test_roundtrip(self) -> None:
        lines = ControlLines(rts=True, dtr=False)
        assert lines.rts is True
        assert lines.dtr is False
