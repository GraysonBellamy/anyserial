"""Unit tests for :mod:`anyserial._linux.rs485`.

Two surfaces: the pure-Python encoders / decoders (``RS485State``,
``from_config``, ``_seconds_to_ms``) and the ``TIOCGRS485`` /
``TIOCSRS485`` round-trip (monkeypatched ``fcntl.ioctl`` so the tests
run without a real serial device).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("Linux-only", allow_module_level=True)

import errno

from anyserial._linux import rs485
from anyserial.config import RS485Config

if TYPE_CHECKING:
    from collections.abc import Iterator


class TestConstants:
    def test_ioctl_codes_match_kernel_abi(self) -> None:
        # Values from <asm-generic/ioctls.h>; stable Linux kernel ABI.
        assert rs485.TIOCGRS485 == 0x542E
        assert rs485.TIOCSRS485 == 0x542F

    def test_flag_bits_match_kernel_abi(self) -> None:
        # Values from <linux/serial.h>. Bit 3 is intentionally unused by
        # the kernel in the SER_RS485_* namespace; we don't expose it.
        assert rs485.SER_RS485_ENABLED == 0x01
        assert rs485.SER_RS485_RTS_ON_SEND == 0x02
        assert rs485.SER_RS485_RTS_AFTER_SEND == 0x04
        assert rs485.SER_RS485_RX_DURING_TX == 0x10
        assert rs485.SER_RS485_TERMINATE_BUS == 0x20


class TestRS485StatePackUnpack:
    def test_struct_size_is_thirty_two_bytes(self) -> None:
        # struct serial_rs485 is 32 bytes on every Linux arch Python
        # supports. A mismatch here usually means padding alignment drift.
        state = rs485.RS485State()
        assert len(state.to_bytes()) == 32

    def test_round_trip_preserves_every_field(self) -> None:
        original = rs485.RS485State(
            flags=rs485.SER_RS485_ENABLED | rs485.SER_RS485_RTS_ON_SEND,
            delay_rts_before_send=5,
            delay_rts_after_send=10,
            addr_recv=0xAB,
            addr_dest=0xCD,
        )
        decoded = rs485.RS485State.from_bytes(original.to_bytes())
        assert decoded == original

    def test_from_bytes_ignores_trailing_padding(self) -> None:
        # Future kernels may extend the struct; trailing bytes must be
        # ignored rather than treated as an error.
        payload = rs485.RS485State(flags=rs485.SER_RS485_ENABLED).to_bytes()
        padded = payload + b"\x00" * 16
        decoded = rs485.RS485State.from_bytes(padded)
        assert decoded.enabled

    def test_from_bytes_rejects_short_payload(self) -> None:
        with pytest.raises(ValueError, match="expected at least 32"):
            rs485.RS485State.from_bytes(b"\x00" * 16)

    def test_enabled_property_reads_bit_zero(self) -> None:
        assert not rs485.RS485State().enabled
        assert rs485.RS485State(flags=rs485.SER_RS485_ENABLED).enabled
        # Other bits set but ENABLED clear → still disabled.
        other = rs485.SER_RS485_RTS_ON_SEND | rs485.SER_RS485_RX_DURING_TX
        assert not rs485.RS485State(flags=other).enabled

    def test_state_is_hashable(self) -> None:
        # frozen+slots dataclass — tests need set / dict membership.
        enabled = rs485.RS485State(flags=rs485.SER_RS485_ENABLED)
        disabled = rs485.RS485State()
        bucket = {enabled, disabled}
        assert enabled in bucket
        assert disabled in bucket
        assert len(bucket) == 2


_seconds_to_ms = rs485._seconds_to_ms  # pyright: ignore[reportPrivateUsage]


class TestSecondsToMs:
    def test_zero_and_negative_return_zero(self) -> None:
        assert _seconds_to_ms(0.0) == 0
        assert _seconds_to_ms(-0.5) == 0

    def test_milliseconds_round_to_nearest(self) -> None:
        assert _seconds_to_ms(0.001) == 1
        assert _seconds_to_ms(0.0015) == 2  # banker's rounding: 2 is nearest even
        assert _seconds_to_ms(0.123) == 123

    def test_clamps_at_u32_max(self) -> None:
        # 10^8 seconds → 10^11 ms, well past the u32 ceiling.
        assert _seconds_to_ms(1e8) == 0xFFFF_FFFF


class TestFromConfig:
    def test_default_config_enables_rs485(self) -> None:
        # RS485Config defaults to enabled=True / rts_on_send=True.
        state = rs485.from_config(RS485Config())
        assert state.flags & rs485.SER_RS485_ENABLED
        assert state.flags & rs485.SER_RS485_RTS_ON_SEND
        assert not state.flags & rs485.SER_RS485_RTS_AFTER_SEND
        assert not state.flags & rs485.SER_RS485_RX_DURING_TX

    def test_all_flags_cleared(self) -> None:
        state = rs485.from_config(
            RS485Config(
                enabled=False,
                rts_on_send=False,
                rts_after_send=False,
                rx_during_tx=False,
            ),
        )
        assert state.flags == 0

    def test_all_flags_set(self) -> None:
        state = rs485.from_config(
            RS485Config(
                enabled=True,
                rts_on_send=True,
                rts_after_send=True,
                rx_during_tx=True,
            ),
        )
        expected = (
            rs485.SER_RS485_ENABLED
            | rs485.SER_RS485_RTS_ON_SEND
            | rs485.SER_RS485_RTS_AFTER_SEND
            | rs485.SER_RS485_RX_DURING_TX
        )
        assert state.flags == expected

    def test_delays_converted_to_milliseconds(self) -> None:
        state = rs485.from_config(
            RS485Config(delay_before_send=0.005, delay_after_send=0.100),
        )
        assert state.delay_rts_before_send == 5
        assert state.delay_rts_after_send == 100

    def test_address_bytes_default_zero(self) -> None:
        # RS485Config has no address fields; from_config must leave them
        # zero so read-modify-write through with_flags_from preserves
        # whatever the driver reported.
        state = rs485.from_config(RS485Config())
        assert state.addr_recv == 0
        assert state.addr_dest == 0


class TestWithFlagsFrom:
    def test_preserves_terminate_bus(self) -> None:
        # A driver that reports bus-termination support keeps its
        # setting after a config apply.
        current = rs485.RS485State(flags=rs485.SER_RS485_TERMINATE_BUS)
        merged = current.with_flags_from(RS485Config(enabled=True))
        assert merged.flags & rs485.SER_RS485_TERMINATE_BUS
        assert merged.flags & rs485.SER_RS485_ENABLED

    def test_preserves_address_bytes(self) -> None:
        current = rs485.RS485State(addr_recv=0x42, addr_dest=0x7F)
        merged = current.with_flags_from(RS485Config())
        assert merged.addr_recv == 0x42
        assert merged.addr_dest == 0x7F

    def test_overrides_config_owned_flags(self) -> None:
        # Current state has all four config-owned flags set; new config
        # clears RTS_AFTER_SEND and RX_DURING_TX.
        current = rs485.RS485State(
            flags=(
                rs485.SER_RS485_ENABLED
                | rs485.SER_RS485_RTS_ON_SEND
                | rs485.SER_RS485_RTS_AFTER_SEND
                | rs485.SER_RS485_RX_DURING_TX
                | rs485.SER_RS485_TERMINATE_BUS
            ),
        )
        merged = current.with_flags_from(
            RS485Config(
                enabled=True,
                rts_on_send=True,
                rts_after_send=False,
                rx_during_tx=False,
            ),
        )
        assert merged.flags & rs485.SER_RS485_ENABLED
        assert merged.flags & rs485.SER_RS485_RTS_ON_SEND
        assert not merged.flags & rs485.SER_RS485_RTS_AFTER_SEND
        assert not merged.flags & rs485.SER_RS485_RX_DURING_TX
        # Driver-reserved bits must not be touched.
        assert merged.flags & rs485.SER_RS485_TERMINATE_BUS

    def test_overwrites_delays(self) -> None:
        current = rs485.RS485State(
            delay_rts_before_send=99,
            delay_rts_after_send=99,
        )
        merged = current.with_flags_from(
            RS485Config(delay_before_send=0.002, delay_after_send=0.004),
        )
        assert merged.delay_rts_before_send == 2
        assert merged.delay_rts_after_send == 4


class _FakeIoctl:
    """Capture ``fcntl.ioctl`` calls and emulate ``TIOCGRS485``.

    ``TIOCGRS485`` returns the bytes form of ``initial_state`` so the
    read path can be driven without a real tty. ``TIOCSRS485`` snapshots
    the payload for later assertions. Any other request aborts the test —
    the module under test should never issue one.
    """

    def __init__(self, *, initial_state: rs485.RS485State) -> None:
        self.state = initial_state
        self.set_payloads: list[bytes] = []
        self.set_states: list[rs485.RS485State] = []
        self.calls: list[int] = []

    def __call__(self, fd: int, request: int, arg: object) -> object:
        self.calls.append(request)
        if request == rs485.TIOCGRS485:
            return self.state.to_bytes()
        if request == rs485.TIOCSRS485:
            assert isinstance(arg, (bytes, bytearray)), type(arg)
            payload = bytes(arg)
            self.set_payloads.append(payload)
            self.set_states.append(rs485.RS485State.from_bytes(payload))
            return 0
        msg = f"unexpected ioctl request {request:#x}"
        raise AssertionError(msg)


@pytest.fixture
def fake_ioctl(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeIoctl]:
    """Install a :class:`_FakeIoctl` over ``fcntl.ioctl`` in the module."""
    fake = _FakeIoctl(initial_state=rs485.RS485State())
    monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", fake)
    yield fake


class TestReadRS485:
    def test_returns_decoded_state(self, fake_ioctl: _FakeIoctl) -> None:
        fake_ioctl.state = rs485.RS485State(
            flags=rs485.SER_RS485_ENABLED | rs485.SER_RS485_RTS_ON_SEND,
            delay_rts_before_send=3,
            delay_rts_after_send=7,
            addr_recv=0x11,
            addr_dest=0x22,
        )
        result = rs485.read_rs485(fd=99)
        assert result == fake_ioctl.state
        assert fake_ioctl.calls == [rs485.TIOCGRS485]

    def test_propagates_enotty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drivers without RS-485 support return ENOTTY.
        def raising_ioctl(*_args: object, **_kwargs: object) -> object:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

        monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", raising_ioctl)
        with pytest.raises(OSError) as excinfo:
            rs485.read_rs485(fd=99)
        assert excinfo.value.errno == errno.ENOTTY

    def test_propagates_einval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Some drivers recognise the ioctl number but refuse the request.
        def raising_ioctl(*_args: object, **_kwargs: object) -> object:
            raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", raising_ioctl)
        with pytest.raises(OSError) as excinfo:
            rs485.read_rs485(fd=99)
        assert excinfo.value.errno == errno.EINVAL


class TestWriteRS485:
    def test_sends_encoded_payload(self, fake_ioctl: _FakeIoctl) -> None:
        state = rs485.RS485State(
            flags=rs485.SER_RS485_ENABLED | rs485.SER_RS485_RX_DURING_TX,
            delay_rts_before_send=1,
            delay_rts_after_send=2,
        )
        rs485.write_rs485(fd=99, state=state)
        assert fake_ioctl.calls == [rs485.TIOCSRS485]
        assert fake_ioctl.set_states == [state]

    def test_round_trips_read_then_write(self, fake_ioctl: _FakeIoctl) -> None:
        # Driver advertises TERMINATE_BUS + an address pair; config apply
        # must preserve them.
        fake_ioctl.state = rs485.RS485State(
            flags=rs485.SER_RS485_TERMINATE_BUS,
            addr_recv=0x42,
            addr_dest=0x7F,
        )
        current = rs485.read_rs485(fd=99)
        merged = current.with_flags_from(RS485Config())
        rs485.write_rs485(fd=99, state=merged)

        assert len(fake_ioctl.set_states) == 1
        written = fake_ioctl.set_states[0]
        assert written.flags & rs485.SER_RS485_TERMINATE_BUS
        assert written.flags & rs485.SER_RS485_ENABLED
        assert written.addr_recv == 0x42
        assert written.addr_dest == 0x7F

    def test_propagates_enotty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising_ioctl(*_args: object, **_kwargs: object) -> object:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

        monkeypatch.setattr("anyserial._linux.rs485.fcntl.ioctl", raising_ioctl)
        with pytest.raises(OSError) as excinfo:
            rs485.write_rs485(fd=99, state=rs485.RS485State())
        assert excinfo.value.errno == errno.ENOTTY
