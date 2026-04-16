"""Comprehensive :class:`SerialPort` coverage against real Linux ptys.

Mirrors the ``MockBackend``-driven suite in :mod:`tests.unit.test_stream` —
round-trip, full-duplex, ResourceGuard, cancellation, close-while-reading,
drain, send_eof, receive_into, receive_available, exclusive access — but
exercises the genuine ``O_NONBLOCK`` fd path through ``anyio.wait_readable``
/ ``wait_writable`` / ``notify_closing``. Parametrized across asyncio,
asyncio+uvloop, and trio by the ``anyio_backend`` fixture in the top-level
``tests/conftest.py``.

Fault-injection tests (EAGAIN / EINTR / short-write) stay on the
``MockBackend`` side — real kernels don't expose hooks for forcing those
failures deterministically. The goal of this file is to prove the async
orchestration works the same way against a real tty as it does against
the mock.
"""

from __future__ import annotations

import os
import sys

import anyio
import pytest

from anyserial import (
    SerialConfig,
    SerialPort,
    open_serial_port,
)
from anyserial.exceptions import PortBusyError

_IS_POSIX_PTY_PLATFORM = sys.platform.startswith("linux") or sys.platform == "darwin"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not _IS_POSIX_PTY_PLATFORM,
        reason=(
            "POSIX pty + fd-readiness orchestration (Linux + Darwin); "
            "BSD lands with that backend, Windows gates at conftest."
        ),
    ),
]

_BAUD = 115_200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain_from_controller(
    controller: int,
    n: int,
    *,
    timeout: float = 1.0,  # noqa: ASYNC109 — helper's own fail_after bound
) -> bytes:
    """Read exactly ``n`` bytes from ``controller``, polling on EAGAIN.

    The ``timeout`` is a helper-owned bound for the internal ``fail_after``
    scope; callers pass what makes sense for the payload size (bulk tests
    need a few seconds, single-byte tests tolerate the default).
    """
    received = bytearray()
    with anyio.fail_after(timeout):
        while len(received) < n:
            try:
                chunk = os.read(controller, n - len(received))
            except BlockingIOError:
                await anyio.sleep(0.001)
                continue
            if not chunk:
                break
            received.extend(chunk)
    return bytes(received)


async def _flush_controller_output(controller: int, data: bytes) -> None:
    """Write ``data`` to ``controller`` in full, polling on EAGAIN."""
    offset = 0
    with anyio.fail_after(1.0):
        while offset < len(data):
            try:
                offset += os.write(controller, data[offset:])
            except BlockingIOError:
                await anyio.sleep(0.001)


# ---------------------------------------------------------------------------
# Basic I/O
# ---------------------------------------------------------------------------


class TestBasicIO:
    async def test_single_byte_round_trip(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            await port.send(b"x")
            assert await _drain_from_controller(controller, 1) == b"x"

            os.write(controller, b"y")
            assert await port.receive(1) == b"y"

    async def test_bulk_round_trip(self, pty_port: tuple[int, str]) -> None:
        # 16 KiB typically exceeds the pty line-buffer so the write loop
        # has to cycle through multiple wait_writable checkpoints. This
        # exercises the partial-write path that receive() doesn't see.
        controller, path = pty_port
        payload = bytes(range(256)) * 64  # 16384 bytes
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:

            async def send_all() -> None:
                await port.send(payload)

            async def drain_peer() -> None:
                received = await _drain_from_controller(controller, len(payload), timeout=5.0)
                assert received == payload

            async with anyio.create_task_group() as tg:
                tg.start_soon(send_all)
                tg.start_soon(drain_peer)

    async def test_receive_into_zero_copy(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            os.write(controller, b"buffer-me")
            buf = bytearray(32)
            count = await port.receive_into(buf)
            assert count > 0
            assert buf[:count] == b"buffer-me"[:count]

    async def test_receive_available_drains_kernel_queue(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            os.write(controller, b"abcdef")
            # Give the kernel a beat to deliver; receive_available parks on
            # readiness and drains whatever is waiting in one syscall.
            data = await port.receive_available()
            assert data == b"abcdef"

    async def test_send_buffer_accepts_memoryview(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            await port.send_buffer(memoryview(b"view-bytes"))
            assert await _drain_from_controller(controller, 10) == b"view-bytes"

    async def test_empty_send_is_noop(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            await port.send(b"")  # must not raise and must not park


# ---------------------------------------------------------------------------
# Full duplex + guards
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_full_duplex_allowed(self, pty_port: tuple[int, str]) -> None:
        controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            received: list[bytes] = []

            async def reader() -> None:
                received.append(await port.receive(16))

            async def writer() -> None:
                await port.send(b"duplex")
                # Reply path: controller echoes back once it sees our send.
                assert await _drain_from_controller(controller, 6) == b"duplex"
                await _flush_controller_output(controller, b"echoed")

            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                tg.start_soon(writer)

            assert received == [b"echoed"]

    async def test_concurrent_reads_raise_busy(self, pty_port: tuple[int, str]) -> None:
        # No data is flowing, so the first reader parks in wait_readable.
        # The second reader must hit the ResourceGuard and raise immediately.
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            errors: list[BaseException] = []

            async def reader() -> None:
                try:
                    await port.receive(8)
                except BaseException as exc:
                    errors.append(exc)

            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                await anyio.sleep(0.02)
                tg.start_soon(reader)
                await anyio.sleep(0.02)
                tg.cancel_scope.cancel()

        assert any(isinstance(e, anyio.BusyResourceError) for e in errors)


# ---------------------------------------------------------------------------
# Cancellation + close races
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_fail_after_during_receive(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            # Nothing queued on the pty; receive must park and the
            # fail_after scope must cancel it cleanly.
            with pytest.raises(TimeoutError):
                with anyio.fail_after(0.05):
                    await port.receive(8)
            # Port is still usable after the cancellation.
            os.write(_controller, b"alive")
            assert await port.receive(5) == b"alive"

    async def test_close_while_reading_wakes_reader(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        port = await open_serial_port(path, SerialConfig(baudrate=_BAUD))
        errors: list[BaseException] = []

        async def reader() -> None:
            try:
                await port.receive(8)
            except BaseException as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            await anyio.sleep(0.02)
            await port.aclose()

        # DESIGN §11.3: notify_closing(fd) must wake the parked reader with
        # ClosedResourceError (or a subclass).
        assert errors, "reader did not wake on close"
        assert isinstance(errors[0], anyio.ClosedResourceError)


# ---------------------------------------------------------------------------
# Drain + send_eof
# ---------------------------------------------------------------------------


class TestDrainAndEof:
    async def test_drain_returns_immediately_when_idle(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            with anyio.fail_after(0.5):
                await port.drain()

    async def test_send_eof_is_idempotent(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            await port.send_eof()
            await port.send_eof()
            # Port is still open for both directions after send_eof —
            # serial has no true half-close (DESIGN §14.4).
            assert port.is_open


# ---------------------------------------------------------------------------
# Exclusive access via flock
# ---------------------------------------------------------------------------


class TestExclusiveAccess:
    async def test_second_open_with_exclusive_raises_busy(self, pty_port: tuple[int, str]) -> None:
        _controller, path = pty_port
        config = SerialConfig(baudrate=_BAUD, exclusive=True)
        async with await open_serial_port(path, config) as first:
            assert first.is_open
            # A second open with exclusive=True must fail fast — the flock
            # is advisory but the backend maps EAGAIN/EWOULDBLOCK to EBUSY
            # so the orchestrator surfaces PortBusyError.
            with pytest.raises(PortBusyError):
                await open_serial_port(path, config)

    async def test_non_exclusive_second_open_succeeds(self, pty_port: tuple[int, str]) -> None:
        # Without exclusive=True the flock is never taken — two SerialPorts
        # coexisting on the same tty is fine (and expected for test rigs
        # that observe from a second process).
        _controller, path = pty_port
        first: SerialPort | None = None
        second: SerialPort | None = None
        try:
            first = await open_serial_port(path, SerialConfig(baudrate=_BAUD))
            second = await open_serial_port(path, SerialConfig(baudrate=_BAUD))
            assert first.is_open
            assert second.is_open
        finally:
            if second is not None:
                await second.aclose()
            if first is not None:
                await first.aclose()


# ---------------------------------------------------------------------------
# Runtime reconfiguration against a real pty
# ---------------------------------------------------------------------------


class TestRuntimeReconfigurePty:
    async def test_reconfigure_while_reader_parked(
        self,
        pty_port: tuple[int, str],
    ) -> None:
        # A reader parked in wait_readable must keep its place across a
        # concurrent configure() — the configure path only takes
        # _configure_lock, not _receive_guard, so it cannot block the
        # in-flight read. When the controller finally writes, the reader
        # wakes up and sees the bytes.
        controller, path = pty_port
        received: list[bytes] = []

        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:

            async def reader() -> None:
                data = await port.receive(32)
                received.append(data)

            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                # Yield so ``reader`` gets a chance to park before the
                # reconfigure fires, then change baud while it sleeps.
                await anyio.sleep(0.01)
                await port.configure(SerialConfig(baudrate=57_600))
                # Only now wake the reader with a controller-side write.
                await _flush_controller_output(controller, b"reconfigured")

        assert received == [b"reconfigured"]

    async def test_sequential_reconfigures_leave_last_config(
        self,
        pty_port: tuple[int, str],
    ) -> None:
        # A run of configure() calls must end with the last config
        # reflected on the port. This is the "config history must not
        # tear" invariant from DESIGN §8.5.
        _controller, path = pty_port
        async with await open_serial_port(path, SerialConfig(baudrate=_BAUD)) as port:
            for rate in (9_600, 19_200, 38_400, 57_600, 115_200):
                await port.configure(SerialConfig(baudrate=rate))
            assert port.config.baudrate == 115_200
