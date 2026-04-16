"""Unit tests for the standard-baud → termios-constant mapping."""

from __future__ import annotations

import sys

import pytest

if sys.platform == "win32":
    pytest.skip("POSIX-only", allow_module_level=True)

import termios

from anyserial._posix.baudrate import (
    STANDARD_BAUD_RATES,
    baudrate_to_speed,
    is_standard_baud,
)
from anyserial.exceptions import UnsupportedConfigurationError


class TestStandardBaudRates:
    def test_mapping_includes_common_rates(self) -> None:
        # Every POSIX termios exposes at least these.
        for rate in (300, 1200, 9600, 19200, 38400):
            assert rate in STANDARD_BAUD_RATES

    def test_values_match_termios_constants(self) -> None:
        assert STANDARD_BAUD_RATES[9600] == termios.B9600
        assert STANDARD_BAUD_RATES[19200] == termios.B19200
        assert STANDARD_BAUD_RATES[38400] == termios.B38400

    def test_no_b0_entry(self) -> None:
        # B0 means hang up — we filter it out so a valid baudrate=0 request
        # can never silently map to a hang-up.
        assert 0 not in STANDARD_BAUD_RATES

    def test_mapping_is_readonly(self) -> None:
        with pytest.raises(TypeError):
            STANDARD_BAUD_RATES[4242] = 0  # type: ignore[index]


class TestBaudrateToSpeed:
    def test_returns_termios_constant_for_known_rate(self) -> None:
        assert baudrate_to_speed(9600) == termios.B9600
        assert baudrate_to_speed(115200) == termios.B115200

    def test_raises_unsupported_for_custom_rate(self) -> None:
        # 1234 is never a standard baud rate on any termios.
        with pytest.raises(UnsupportedConfigurationError, match="1234"):
            baudrate_to_speed(1234)

    def test_raises_unsupported_for_zero(self) -> None:
        with pytest.raises(UnsupportedConfigurationError):
            baudrate_to_speed(0)


class TestIsStandardBaud:
    def test_true_for_common_rates(self) -> None:
        assert is_standard_baud(9600)
        assert is_standard_baud(115200)

    def test_false_for_custom_rate(self) -> None:
        assert not is_standard_baud(1234)
