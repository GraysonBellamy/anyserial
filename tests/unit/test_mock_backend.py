"""Tests for :class:`anyserial.testing.MockBackend`.

MockBackend is itself sync and requires no event loop, so these tests
run as plain pytest functions. Later tests that drive it via a real
:class:`SerialPort` will cover the async readiness loop.
"""

from __future__ import annotations

import errno
import os
import sys

import pytest

from anyserial._backend import SyncSerialBackend
from anyserial._types import Capability
from anyserial.config import SerialConfig
from anyserial.testing import FaultPlan, MockBackend


class TestPairing:
    def test_pair_returns_two_open_backends(self) -> None:
        a, b = MockBackend.pair()
        try:
            assert a.fileno() != b.fileno()
            assert a.path == "/dev/mockA"
            assert b.path == "/dev/mockB"
        finally:
            a.close()
            b.close()

    def test_custom_paths(self) -> None:
        a, b = MockBackend.pair(path_a="/dev/x", path_b="/dev/y")
        try:
            assert a.path == "/dev/x"
            assert b.path == "/dev/y"
        finally:
            a.close()
            b.close()

    def test_satisfies_sync_protocol(self) -> None:
        a, b = MockBackend.pair()
        try:
            assert isinstance(a, SyncSerialBackend)
            assert isinstance(b, SyncSerialBackend)
        finally:
            a.close()
            b.close()


class TestLifecycle:
    def test_initial_state_is_closed(self) -> None:
        a, b = MockBackend.pair()
        try:
            assert bool(a.is_open) is False
        finally:
            a.close()
            b.close()

    def test_open_sets_is_open_true(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            assert bool(a.is_open) is True
        finally:
            a.close()
            b.close()

    def test_close_sets_is_open_false(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            a.close()
            assert bool(a.is_open) is False
        finally:
            b.close()

    def test_double_open_raises(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            with pytest.raises(RuntimeError):
                a.open(a.path, SerialConfig())
        finally:
            a.close()
            b.close()

    def test_close_is_idempotent(self) -> None:
        a, b = MockBackend.pair()
        a.open(a.path, SerialConfig())
        a.close()
        a.close()  # second call must not raise
        b.close()


class TestLoopback:
    def test_write_one_side_read_the_other(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            b.open(b.path, SerialConfig())
            written = a.write_nonblocking(memoryview(b"ping"))
            assert written == 4
            buf = bytearray(16)
            count = b.read_nonblocking(buf)
            assert bytes(buf[:count]) == b"ping"
        finally:
            a.close()
            b.close()

    def test_read_with_no_data_raises_blockingio(self) -> None:
        a, b = MockBackend.pair()
        try:
            buf = bytearray(16)
            with pytest.raises(BlockingIOError):
                a.read_nonblocking(buf)
        finally:
            a.close()
            b.close()

    def test_input_waiting_reflects_peer_writes(self) -> None:
        a, b = MockBackend.pair()
        try:
            assert b.input_waiting() == 0
            a.write_nonblocking(memoryview(b"abc"))
            assert b.input_waiting() == 3
            # input_waiting must be non-destructive: reading still yields
            # the same bytes.
            buf = bytearray(8)
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"abc"
        finally:
            a.close()
            b.close()

    def test_loopback_does_not_depend_on_posix_fd_helpers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_posix_helper(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("MockBackend must use socket-native I/O")

        monkeypatch.setattr(os, "readv", fail_posix_helper, raising=False)
        monkeypatch.setattr(os, "write", fail_posix_helper)

        a, b = MockBackend.pair()
        try:
            written = a.write_nonblocking(memoryview(b"portable"))
            assert written == len(b"portable")
            buf = bytearray(16)
            count = b.read_nonblocking(buf)
            assert bytes(buf[:count]) == b"portable"
        finally:
            a.close()
            b.close()


class TestFaultInjection:
    def test_eagain_reads_decrement_and_clear(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"hello"))
            b.faults.eagain_reads = 2
            buf = bytearray(16)
            with pytest.raises(BlockingIOError):
                b.read_nonblocking(buf)
            with pytest.raises(BlockingIOError):
                b.read_nonblocking(buf)
            # Third attempt succeeds now that the counter is drained.
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"hello"
            assert b.faults.eagain_reads == 0
        finally:
            a.close()
            b.close()

    def test_eintr_reads(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"hi"))
            b.faults.eintr_reads = 1
            buf = bytearray(4)
            with pytest.raises(InterruptedError):
                b.read_nonblocking(buf)
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"hi"
        finally:
            a.close()
            b.close()

    def test_short_write(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.faults.short_write_max = 3
            written = a.write_nonblocking(memoryview(b"abcdef"))
            assert written == 3
            buf = bytearray(16)
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"abc"
        finally:
            a.close()
            b.close()

    def test_eagain_writes(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.faults.eagain_writes = 1
            with pytest.raises(BlockingIOError):
                a.write_nonblocking(memoryview(b"x"))
            # Next write goes through.
            a.write_nonblocking(memoryview(b"y"))
        finally:
            a.close()
            b.close()

    def test_disconnect_reads_return_zero(self) -> None:
        a, b = MockBackend.pair()
        try:
            b.faults.disconnected = True
            buf = bytearray(4)
            assert b.read_nonblocking(buf) == 0
        finally:
            a.close()
            b.close()

    def test_disconnect_writes_raise_epipe(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.faults.disconnected = True
            with pytest.raises(BrokenPipeError) as info:
                a.write_nonblocking(memoryview(b"x"))
            assert info.value.errno == errno.EPIPE
        finally:
            a.close()
            b.close()

    def test_eio_on_read_fires_once(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"z"))
            b.faults.raise_eio_on_read = True
            buf = bytearray(4)
            with pytest.raises(OSError) as info:
                b.read_nonblocking(buf)
            assert info.value.errno == errno.EIO
            # Cleared after firing — follow-up read succeeds with the byte.
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"z"
        finally:
            a.close()
            b.close()

    def test_parity_error_fault(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"p"))
            b.faults.parity_errors = 2
            buf = bytearray(4)
            with pytest.raises(OSError) as info:
                b.read_nonblocking(buf)
            assert info.value.errno == errno.EIO
            assert "parity" in (info.value.strerror or "")
            with pytest.raises(OSError):
                b.read_nonblocking(buf)
            # Third attempt succeeds — counter drained.
            n = b.read_nonblocking(buf)
            assert bytes(buf[:n]) == b"p"
            assert b.faults.parity_errors == 0
        finally:
            a.close()
            b.close()


class TestControlPlane:
    def test_capabilities_are_mock_snapshot(self) -> None:
        a, b = MockBackend.pair()
        try:
            caps = a.capabilities
            assert caps.platform == "mock"
            assert caps.backend == "mock"
            assert caps.low_latency is Capability.UNSUPPORTED
            assert caps.input_waiting is Capability.SUPPORTED
        finally:
            a.close()
            b.close()

    def test_configure_stores_last_config(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            new = SerialConfig(baudrate=9_600)
            a.configure(new)
            assert a.last_config is new
        finally:
            a.close()
            b.close()

    def test_modem_lines_reflect_peer_control_lines(self) -> None:
        a, b = MockBackend.pair()
        try:
            # Initially everything is low.
            assert a.modem_lines().cts is False
            # B raises RTS → A sees CTS.
            b.set_control_lines(rts=True)
            assert a.modem_lines().cts is True
            # B raises DTR → A sees DSR and CD.
            b.set_control_lines(dtr=True)
            lines = a.modem_lines()
            assert lines.dsr is True
            assert lines.cd is True
            # Leaving rts unchanged (None) preserves the bit.
            b.set_control_lines(dtr=False)
            lines = a.modem_lines()
            assert lines.cts is True
            assert lines.dsr is False
        finally:
            a.close()
            b.close()

    def test_reset_input_buffer_drops_pending(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"garbage"))
            b.reset_input_buffer()
            assert b.input_waiting() == 0
        finally:
            a.close()
            b.close()

    def test_set_break_toggles_flag(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.set_break(on=True)
            assert a.break_asserted is True
            a.set_break(on=False)
            assert a.break_asserted is False
        finally:
            a.close()
            b.close()


class TestFileDescriptor:
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows socket handles are not CRT fds")
    def test_fileno_usable_with_os_read(self) -> None:
        a, b = MockBackend.pair()
        try:
            a.write_nonblocking(memoryview(b"fd"))
            data = os.read(b.fileno(), 8)
            assert data == b"fd"
        finally:
            a.close()
            b.close()


@pytest.fixture
def mock_pair() -> tuple[MockBackend, MockBackend]:
    a, b = MockBackend.pair()
    try:
        a.open(a.path, SerialConfig())
        b.open(b.path, SerialConfig())
        return a, b
    except Exception:
        a.close()
        b.close()
        raise


class TestFaultPlanDefaults:
    def test_defaults_all_inert(self) -> None:
        fp = FaultPlan()
        assert fp.eagain_reads == 0
        assert fp.eagain_writes == 0
        assert fp.eintr_reads == 0
        assert fp.eintr_writes == 0
        assert fp.short_write_max is None
        assert fp.disconnected is False
        assert fp.raise_eio_on_read is False
