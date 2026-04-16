"""End-to-end :class:`anyserial.sync.SerialPort` tests against real Linux ptys.

The sync wrapper is a pure delegation layer; these tests prove the full
stack — portal spin-up, fd-readiness dispatch, shielded close, runtime
reconfigure, timeouts — works against a genuine ``O_NONBLOCK`` fd rather
than only through :class:`MockBackend`.

Non-parametrized: sync code picks one AnyIO backend for the portal. The
default (``asyncio``) is exercised here; ``configure_portal`` is covered
in the unit tests.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

import pytest
from anyio.streams.file import FileStreamAttribute

from anyserial import SerialConfig, SerialStreamAttribute
from anyserial import sync as _sync
from anyserial.sync import open_serial_port

_reset_portal_for_testing = _sync._reset_portal_for_testing  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Iterator

_BAUD = 115_200
_IS_POSIX_PTY_PLATFORM = sys.platform.startswith("linux") or sys.platform == "darwin"

pytestmark = pytest.mark.skipif(
    not _IS_POSIX_PTY_PLATFORM,
    reason=(
        "POSIX pty + fd-readiness orchestration (Linux + Darwin); "
        "BSD lands with that backend, Windows gates at conftest."
    ),
)


@pytest.fixture(autouse=True)
def _reset_portal_after_test() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    yield
    _reset_portal_for_testing()


def _blocking_read(controller: int, n: int, *, timeout: float = 2.0) -> bytes:
    """Read exactly ``n`` bytes from a non-blocking ``controller`` fd."""
    received = bytearray()
    deadline = time.monotonic() + timeout
    while len(received) < n:
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {n} bytes (got {len(received)})")
        try:
            chunk = os.read(controller, n - len(received))
        except BlockingIOError:
            time.sleep(0.001)
            continue
        if not chunk:
            break
        received.extend(chunk)
    return bytes(received)


def _blocking_write(controller: int, data: bytes, *, timeout: float = 2.0) -> None:
    offset = 0
    deadline = time.monotonic() + timeout
    while offset < len(data):
        if time.monotonic() > deadline:
            raise TimeoutError("timed out writing to controller fd")
        try:
            written = os.write(controller, data[offset:])
        except BlockingIOError:
            time.sleep(0.001)
            continue
        if written <= 0:
            break
        offset += written


class TestOpenClose:
    def test_open_returns_working_port(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        with open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            assert port.path == path
            assert port.is_open
            port.send(b"hello")
            assert _blocking_read(controller, 5) == b"hello"

    def test_context_manager_closes_port(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path) as port:
            pass
        assert port.is_open is False

    def test_close_is_idempotent(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        port = open_serial_port(path)
        port.close()
        port.close()


class TestIO:
    def test_round_trip(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        with open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            port.send(b"ping\n")
            assert _blocking_read(controller, 5) == b"ping\n"

            _blocking_write(controller, b"pong\n")
            assert port.receive(1024, timeout=2.0) == b"pong\n"

    def test_receive_into(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        with open_serial_port(path) as port:
            _blocking_write(controller, b"into-me")
            buf = bytearray(32)
            count = port.receive_into(buf, timeout=2.0)
            assert count > 0
            assert buf[:count].startswith(b"into-me"[:count])

    def test_receive_timeout(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path) as port:
            with pytest.raises(TimeoutError):
                port.receive(16, timeout=0.1)
            # Port must still be usable after the timeout.
            port.send(b"after")
            assert port.is_open


class TestControl:
    def test_reset_buffers(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        with open_serial_port(path) as port:
            _blocking_write(controller, b"garbage")
            # Give the kernel a moment to deliver.
            time.sleep(0.01)
            port.reset_input_buffer()
            port.reset_output_buffer()

    def test_configure_runtime(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path, SerialConfig(baudrate=9600)) as port:
            assert port.config.baudrate == 9600
            port.configure(SerialConfig(baudrate=_BAUD))
            assert port.config.baudrate == _BAUD

    def test_drain_is_noop_when_empty(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path) as port:
            port.drain(timeout=1.0)


class TestExtraAttributes:
    def test_fileno_and_path(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path) as port:
            fd = port.extra(FileStreamAttribute.fileno)
            assert fd >= 0
            assert str(port.extra(FileStreamAttribute.path)) == path

    def test_capabilities(self, pty_port: tuple[int, str]) -> None:
        _, path = pty_port
        with open_serial_port(path) as port:
            caps = port.extra(SerialStreamAttribute.capabilities)
            assert caps == port.capabilities
