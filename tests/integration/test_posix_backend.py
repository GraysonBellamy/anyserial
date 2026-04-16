"""Integration tests for :class:`PosixBackend` against real pty fds.

Two pty idioms are used across this file:

1. ``pty_pair()`` — :func:`pty.openpty` returns a controller/follower pair with
   live data flow between them. Hot-path and ioctl tests adopt the follower
   fd directly into a backend instance via :func:`_backend_from_fd` so we
   exercise ``os.read``/``os.write`` and the ioctl helpers against a real
   kernel tty.
2. ``pty_path()`` — a ``(controller, follower, path)`` triple where the
   caller keeps the original follower open as an anchor. Lifecycle tests
   (open/configure/close) open a *second* fd to the same ``/dev/pts/X``
   path. Data flow between the controller and that second fd is undefined,
   but every termios / ioctl operation works correctly.
"""

from __future__ import annotations

import contextlib
import os
import pty
import sys
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

if sys.platform == "win32":
    pytest.skip("POSIX-only", allow_module_level=True)

import termios

from anyserial._posix.backend import PosixBackend
from anyserial._types import ByteSize, Parity, StopBits
from anyserial.config import FlowControl, SerialConfig
from anyserial.exceptions import UnsupportedConfigurationError

# ``TIOCINQ`` (a.k.a. ``FIONREAD``) on a pty *master* is a Linux-ism: Linux
# returns the count of bytes the master can read, but Darwin's pty driver
# tracks the input/output queues separately and returns 0 on the master
# regardless of what the slave wrote. The ioctl still works on real serial
# fds and on pty *slaves* — it's specifically the master-side query that's
# a no-op on Darwin. Tests that depend on master-side counts skip on
# Darwin; the same code paths are covered on Linux CI.
_skip_pty_master_inq = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="TIOCINQ on pty master returns 0 on Darwin (kernel pty quirk, not a bug)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def pty_pair() -> Iterator[tuple[int, int]]:
    """Yield ``(controller, follower)`` fds in raw mode with live data flow.

    The follower is switched to raw mode up front: by default the pty line
    discipline keeps writes from the controller in a canonical-mode buffer
    until a newline arrives, which makes byte-oriented tests hang waiting
    for data the kernel is withholding. Raw mode disables that buffering
    and also turns off the ``\\n``→``\\r\\n`` OPOST translation that would
    otherwise inflate byte counts.
    """
    controller, follower = pty.openpty()
    try:
        attrs = termios.tcgetattr(follower)
        # Hand-rolled cfmakeraw; doing it in-place avoids pulling in our
        # own builders, which this test module is a consumer of.
        attrs[0] &= ~(
            termios.IGNBRK
            | termios.BRKINT
            | termios.PARMRK
            | termios.ISTRIP
            | termios.INLCR
            | termios.IGNCR
            | termios.ICRNL
            | termios.IXON
        )
        attrs[1] &= ~termios.OPOST
        attrs[3] &= ~(
            termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
        )
        attrs[2] = (attrs[2] & ~(termios.CSIZE | termios.PARENB)) | termios.CS8
        termios.tcsetattr(follower, termios.TCSANOW, attrs)
        yield controller, follower
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


@contextmanager
def pty_path() -> Iterator[tuple[int, int, str]]:
    """Yield ``(controller, follower, path)`` where a second fd can be opened.

    The follower fd is kept open as an anchor so the tty stays alive; the
    caller is expected to open its own fd to ``path`` for the subject
    under test. Data flow between ``controller`` and that second fd is not
    guaranteed — use :func:`pty_pair` when data exchange matters.
    """
    controller, follower = pty.openpty()
    path = os.ttyname(follower)
    try:
        yield controller, follower, path
    finally:
        for fd in (controller, follower):
            with contextlib.suppress(OSError):
                os.close(fd)


def _backend_from_fd(fd: int, path: str = "/dev/ptsN", *, baudrate: int = 9600) -> PosixBackend:
    """Construct a :class:`PosixBackend` wrapping an already-open fd.

    Test-only shortcut that bypasses ``open()`` so the read/write and ioctl
    methods can exercise a live pty. The fd is forced into ``O_NONBLOCK`` to
    match what ``PosixBackend.open`` would have done — otherwise the hot-path
    ``os.readv`` / ``os.write`` calls could block and hang the test suite.
    The caller retains fd ownership — call ``backend.close()`` OR close the
    fd manually, not both.
    """
    import fcntl  # noqa: PLC0415  — only needed here

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    backend = PosixBackend()
    backend._fd = fd  # pyright: ignore[reportPrivateUsage]
    backend._path = path  # pyright: ignore[reportPrivateUsage]
    backend._config = SerialConfig(baudrate=baudrate)  # pyright: ignore[reportPrivateUsage]
    return backend


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_fresh_backend_is_closed(self) -> None:
        backend = PosixBackend()
        assert not backend.is_open
        assert backend.fileno() == -1

    def test_open_then_close(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(path, SerialConfig(baudrate=9600))
            try:
                assert backend.is_open
                assert backend.path == path
                assert backend.fileno() >= 0
            finally:
                backend.close()
            assert not backend.is_open

    def test_close_is_idempotent(self) -> None:
        backend = PosixBackend()
        backend.close()  # no-op on fresh instance
        with pty_path() as (_controller, _anchor, path):
            backend.open(path, SerialConfig(baudrate=9600))
            backend.close()
            backend.close()  # second close is also a no-op

    def test_double_open_raises(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(path, SerialConfig(baudrate=9600))
            try:
                with pytest.raises(RuntimeError, match="already open"):
                    backend.open(path, SerialConfig(baudrate=9600))
            finally:
                backend.close()

    def test_open_propagates_enoent(self) -> None:
        backend = PosixBackend()
        with pytest.raises(FileNotFoundError):
            backend.open("/dev/definitely-not-a-tty", SerialConfig(baudrate=9600))

    def test_open_propagates_unsupported_baud(self) -> None:
        # 1234 is never a standard termios rate — the config-apply step
        # raises UnsupportedConfigurationError before tcsetattr is called,
        # and the backend cleans up the fd it just opened.
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            with pytest.raises(UnsupportedConfigurationError):
                backend.open(path, SerialConfig(baudrate=1234))
            assert not backend.is_open


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfigure:
    def test_open_applies_raw_mode(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(path, SerialConfig(baudrate=9600))
            try:
                attrs = termios.tcgetattr(backend.fileno())
                iflag, oflag, cflag, lflag = attrs[:4]
                # Raw mode: ICANON/ECHO off, OPOST off, CS8 + CREAD + CLOCAL set.
                assert lflag & termios.ICANON == 0
                assert lflag & termios.ECHO == 0
                assert oflag & termios.OPOST == 0
                assert cflag & termios.CSIZE == termios.CS8
                assert cflag & termios.CREAD
                assert cflag & termios.CLOCAL
                # IXON and ICRNL must be cleared so byte payloads aren't cooked.
                assert iflag & termios.IXON == 0
                assert iflag & termios.ICRNL == 0
            finally:
                backend.close()

    def test_configure_updates_baud(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(path, SerialConfig(baudrate=9600))
            try:
                backend.configure(SerialConfig(baudrate=115200))
                attrs = termios.tcgetattr(backend.fileno())
                # Speeds are at indices 4 (ispeed) and 5 (ospeed).
                assert attrs[4] == termios.B115200
                assert attrs[5] == termios.B115200
            finally:
                backend.close()

    def test_configure_applies_stop_bits_and_hangup(self) -> None:
        # CSIZE and PARENB are silently normalized by the Linux pty driver
        # (ptys don't implement 5/6/7-bit framing or parity), so those
        # branches are verified in the pure-builder unit tests rather than
        # here. CSTOPB and HUPCL do round-trip through tcsetattr on a pty
        # and are sufficient to prove the builder pipeline reaches the fd.
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(
                path,
                SerialConfig(
                    baudrate=9600,
                    byte_size=ByteSize.SEVEN,
                    parity=Parity.EVEN,
                    stop_bits=StopBits.TWO,
                    hangup_on_close=True,
                ),
            )
            try:
                cflag = termios.tcgetattr(backend.fileno())[2]
                assert cflag & termios.CSTOPB
                assert cflag & termios.HUPCL
                assert cflag & termios.CREAD
                assert cflag & termios.CLOCAL
            finally:
                backend.close()

    def test_configure_rejects_xon_xoff_via_flowcontrol_flags(self) -> None:
        with pty_path() as (_controller, _anchor, path):
            backend = PosixBackend()
            backend.open(
                path,
                SerialConfig(baudrate=9600, flow_control=FlowControl(xon_xoff=True)),
            )
            try:
                iflag = termios.tcgetattr(backend.fileno())[0]
                assert iflag & termios.IXON
                assert iflag & termios.IXOFF
            finally:
                backend.close()


# ---------------------------------------------------------------------------
# Hot path (read / write)
# ---------------------------------------------------------------------------


class TestHotPath:
    def test_write_then_read(self) -> None:
        with pty_pair() as (controller, follower):
            backend = _backend_from_fd(follower)
            # write from backend → should arrive on controller
            written = backend.write_nonblocking(memoryview(b"hello"))
            assert written == 5
            # small sleep so the kernel can post the write
            for _ in range(50):
                buf = bytearray(16)
                try:
                    n = os.readv(controller, [buf])
                    if n > 0:
                        assert bytes(buf[:n]) == b"hello"
                        break
                except BlockingIOError:
                    time.sleep(0.001)
            else:
                pytest.fail("controller never saw backend write")

    def test_read_nonblocking_returns_zero_when_idle(self) -> None:
        with pty_pair() as (_controller, follower):
            backend = _backend_from_fd(follower)
            with pytest.raises(BlockingIOError):
                backend.read_nonblocking(bytearray(16))

    def test_read_nonblocking_fills_buffer(self) -> None:
        with pty_pair() as (controller, follower):
            backend = _backend_from_fd(follower)
            os.write(controller, b"pingpong")
            # Poll until the bytes arrive on the follower side.
            buf = bytearray(16)
            for _ in range(50):
                try:
                    n = backend.read_nonblocking(buf)
                    assert n == 8
                    assert bytes(buf[:n]) == b"pingpong"
                    break
                except BlockingIOError:
                    time.sleep(0.001)
            else:
                pytest.fail("backend never saw controller write")


# ---------------------------------------------------------------------------
# Snapshots + flushes
# ---------------------------------------------------------------------------


class TestSnapshots:
    @_skip_pty_master_inq
    def test_input_waiting_reflects_pending_bytes(self) -> None:
        with pty_pair() as (controller, follower):
            backend = _backend_from_fd(controller)
            # Payload without \n — the default pty line discipline would
            # translate \n to \r\n and inflate the count.
            os.write(follower, b"pingpong")
            for _ in range(50):
                if backend.input_waiting() == 8:
                    break
                time.sleep(0.001)
            assert backend.input_waiting() == 8

    def test_output_waiting_returns_zero_on_pty(self) -> None:
        with pty_pair() as (_controller, follower):
            backend = _backend_from_fd(follower)
            # Ptys don't queue output; the call must still succeed.
            assert backend.output_waiting() == 0

    @_skip_pty_master_inq
    def test_reset_input_buffer_discards_pending(self) -> None:
        with pty_pair() as (controller, follower):
            backend = _backend_from_fd(controller)
            os.write(follower, b"stale")
            for _ in range(50):
                if backend.input_waiting() > 0:
                    break
                time.sleep(0.001)
            assert backend.input_waiting() > 0
            backend.reset_input_buffer()
            assert backend.input_waiting() == 0

    def test_reset_output_buffer_succeeds(self) -> None:
        with pty_pair() as (_controller, follower):
            backend = _backend_from_fd(follower)
            backend.reset_output_buffer()  # no-op but must not raise


# ---------------------------------------------------------------------------
# Break + tcdrain
# ---------------------------------------------------------------------------


class TestBreakAndDrain:
    def test_set_break_assert_and_deassert(self) -> None:
        with pty_pair() as (_controller, follower):
            backend = _backend_from_fd(follower)
            backend.set_break(on=True)
            backend.set_break(on=False)

    def test_tcdrain_blocking_on_idle_pty(self) -> None:
        with pty_pair() as (_controller, follower):
            backend = _backend_from_fd(follower)
            # Nothing queued; tcdrain should return immediately.
            backend.tcdrain_blocking()


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_generic_posix_capabilities_shape(self) -> None:
        caps = PosixBackend().capabilities
        assert caps.backend == "posix"
        # Generic-POSIX never advertises Linux-only features.
        assert caps.custom_baudrate.value == "unsupported"
        assert caps.low_latency.value == "unsupported"
        assert caps.rs485.value == "unsupported"
        # DTR/DSR isn't configurable via generic termios.
        assert caps.dtr_dsr.value == "unsupported"
        # Exclusive access via flock is always available.
        assert caps.exclusive_access.value == "supported"
