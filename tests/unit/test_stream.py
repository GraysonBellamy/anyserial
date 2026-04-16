"""End-to-end tests for :class:`SerialPort` against :class:`MockBackend`.

These tests drive the full async stack — readiness loop, ResourceGuards,
shielded close, runtime reconfiguration — via a ``MockBackend`` pair.
The same tests exercise every branch that a real POSIX backend would
hit; real-fd integration tests will live in ``tests/integration/``.

Parametrized against asyncio / asyncio+uvloop / trio by the
``anyio_backend`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import gc
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import anyio.abc
import pytest
from anyio.lowlevel import checkpoint
from anyio.streams.file import FileStreamAttribute

from anyserial import (
    SerialClosedError,
    SerialConfig,
    SerialDisconnectedError,
    SerialPort,
    SerialStreamAttribute,
    UnsupportedPlatformError,
)
from anyserial.stream import SerialConnectable, open_serial_port
from anyserial.testing import serial_port_pair

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pair() -> AsyncIterator[tuple[SerialPort, SerialPort]]:
    a, b = serial_port_pair()
    try:
        yield a, b
    finally:
        await a.aclose()
        await b.aclose()


class TestBasicIO:
    async def test_round_trip(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        await a.send(b"ping\n")
        data = await b.receive(1024)
        assert data == b"ping\n"

    async def test_receive_returns_partial(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        await a.send(b"hello")
        data = await b.receive(3)
        assert len(data) <= 3
        assert data
        # Consume the rest so cleanup doesn't race with a pending read.
        if len(data) < 5:
            more = await b.receive(5)
            assert bytes(data + more).startswith(b"hello")

    async def test_receive_into_zero_copy(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        await a.send(b"buffer-me")
        buf = bytearray(16)
        count = await b.receive_into(buf)
        assert count > 0
        assert buf[:count] == b"buffer-me"[:count]

    async def test_receive_available(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        await a.send(b"abc")
        # Give the kernel a moment to deliver.
        data = await b.receive_available()
        assert data == b"abc"

    async def test_send_buffer_accepts_bytearray(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        payload = bytearray(b"mutable")
        await a.send_buffer(payload)
        received = await b.receive(64)
        assert received == b"mutable"

    async def test_send_buffer_accepts_memoryview(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        src = memoryview(b"view-bytes")
        await a.send_buffer(src)
        received = await b.receive(64)
        assert received == b"view-bytes"

    async def test_empty_send_is_noop(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        await a.send(b"")  # must not raise and must not block

    async def test_receive_rejects_zero_max_bytes(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        _, b = pair
        with pytest.raises(ValueError, match="max_bytes"):
            await b.receive(0)

    async def test_receive_into_rejects_empty_buffer(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        _, b = pair
        with pytest.raises(ValueError, match="non-empty"):
            await b.receive_into(bytearray(0))


class TestResourceGuards:
    async def test_concurrent_reads_raise(self, pair: tuple[SerialPort, SerialPort]) -> None:
        _, b = pair
        errors: list[BaseException] = []

        async def read_once() -> None:
            try:
                await b.receive(8)
            except BaseException as exc:
                errors.append(exc)

        # Two tasks read at the same time. No data is available, so both
        # will park — the second must raise BusyResourceError immediately.
        async with anyio.create_task_group() as tg:
            tg.start_soon(read_once)
            await anyio.sleep(0.01)
            tg.start_soon(read_once)
            await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert any(isinstance(e, anyio.BusyResourceError) for e in errors)

    async def test_concurrent_writes_raise(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        errors: list[BaseException] = []
        # Park the first writer indefinitely by injecting many EAGAINs —
        # wait_writable + the retry loop yields between each, giving the
        # second task time to hit the guard and raise.
        a._backend.faults.eagain_writes = 10_000  # type: ignore[union-attr]

        async def first() -> None:
            try:
                await a.send(b"x")
            except BaseException as exc:
                errors.append(exc)

        async def second() -> None:
            try:
                await a.send(b"y")
            except BaseException as exc:
                errors.append(exc)

        async with anyio.create_task_group() as tg:
            tg.start_soon(first)
            await anyio.sleep(0.01)
            tg.start_soon(second)
            await anyio.sleep(0.01)
            tg.cancel_scope.cancel()

        assert any(isinstance(e, anyio.BusyResourceError) for e in errors)

    async def test_full_duplex_allowed(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        received: list[bytes] = []

        async def reader() -> None:
            received.append(await b.receive(16))

        async def writer() -> None:
            await a.send(b"duplex")

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)

        assert received == [b"duplex"]


class TestFaultPaths:
    async def test_eagain_reads_retry_transparently(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        b._backend.faults.eagain_reads = 2  # type: ignore[union-attr]
        await a.send(b"retry")
        data = await b.receive(64)
        assert data == b"retry"

    async def test_eintr_reads_retry_transparently(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        b._backend.faults.eintr_reads = 1  # type: ignore[union-attr]
        await a.send(b"eintr")
        data = await b.receive(64)
        assert data == b"eintr"

    async def test_short_write_completes_full_payload(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        a._backend.faults.short_write_max = 2  # type: ignore[union-attr]
        await a.send(b"abcdef")
        # Drain the peer until we've accumulated all the bytes.
        buf = b""
        while len(buf) < 6:
            buf += await b.receive(16)
        assert buf == b"abcdef"

    async def test_disconnect_surfaces_as_disconnected_error(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        b._backend.faults.disconnected = True  # type: ignore[union-attr]
        # Prime the socket so wait_readable returns, then the mock reports EOF.
        await a.send(b"gone")
        with pytest.raises(SerialDisconnectedError):
            await b.receive(16)

    async def test_disconnect_is_anyio_broken_resource(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, b = pair
        b._backend.faults.disconnected = True  # type: ignore[union-attr]
        await a.send(b"gone")
        # SerialDisconnectedError is a BrokenResourceError — AnyIO code that
        # only catches the base class still handles it.
        with pytest.raises(anyio.BrokenResourceError):
            await b.receive(16)


class TestClose:
    async def test_aclose_is_idempotent(self) -> None:
        a, b = serial_port_pair()
        await a.aclose()
        await a.aclose()
        await b.aclose()

    async def test_operation_after_close_raises_closed(self) -> None:
        a, b = serial_port_pair()
        try:
            await a.aclose()
            with pytest.raises(SerialClosedError):
                await a.send(b"nope")
            with pytest.raises(SerialClosedError):
                await a.receive(1)
        finally:
            await b.aclose()

    async def test_closed_is_anyio_closed(self) -> None:
        a, b = serial_port_pair()
        try:
            await a.aclose()
            with pytest.raises(anyio.ClosedResourceError):
                await a.send(b"nope")
        finally:
            await b.aclose()

    async def test_close_wakes_pending_receive(self) -> None:
        a, b = serial_port_pair()
        raised: list[BaseException] = []

        async def reader() -> None:
            try:
                await b.receive(16)
            except BaseException as exc:
                raised.append(exc)

        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(reader)
                await anyio.sleep(0.02)
                await b.aclose()
        finally:
            await a.aclose()

        assert raised, "reader never woke"
        assert isinstance(raised[0], anyio.ClosedResourceError)

    async def test_context_manager_closes(self) -> None:
        a, b = serial_port_pair()
        try:
            async with a as port:
                assert port is a
                assert port.is_open
            assert not a.is_open
        finally:
            await b.aclose()


class TestConfigure:
    async def test_configure_updates_config(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        assert a.config.baudrate == 115_200
        new = SerialConfig(baudrate=9_600)
        await a.configure(new)
        assert a.config is new
        assert a.config.baudrate == 9_600

    async def test_configure_after_close_raises(self) -> None:
        a, b = serial_port_pair()
        try:
            await a.aclose()
            with pytest.raises(SerialClosedError):
                await a.configure(SerialConfig())
        finally:
            await b.aclose()

    async def test_configure_preserves_rs485_in_stored_config(
        self,
        pair: tuple[SerialPort, SerialPort],
    ) -> None:
        # MockBackend's capability reports RS-485 as UNSUPPORTED, but the
        # stream-level contract still has to round-trip the RS485Config
        # sub-object via SerialConfig.rs485 — real backends read it off
        # the config they are handed.
        from anyserial.config import RS485Config  # noqa: PLC0415 — test-local

        a, _ = pair
        cfg = SerialConfig(rs485=RS485Config(delay_before_send=0.001))
        await a.configure(cfg)
        assert a.config.rs485 is not None
        assert a.config.rs485.delay_before_send == pytest.approx(0.001)  # pyright: ignore[reportUnknownMemberType]

    async def test_configure_leaves_config_unchanged_on_backend_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A backend that raises during configure() must leave the stream's
        # stored config at the previous value so the user can retry with
        # an alternate config instead of seeing a half-applied state.
        import errno  # noqa: PLC0415 — test-local

        from anyserial._mock.backend import MockBackend  # noqa: PLC0415
        from anyserial.exceptions import UnsupportedFeatureError  # noqa: PLC0415

        a, b = serial_port_pair()
        try:
            original = a.config

            def raising_configure(_self: MockBackend, _config: SerialConfig) -> None:
                raise OSError(errno.EINVAL, "mock: configure rejected")

            # MockBackend uses __slots__, so we patch the method on the
            # class. Both backends in the pair see it, but only ``a`` is
            # configured in this test.
            monkeypatch.setattr(MockBackend, "configure", raising_configure)

            with pytest.raises(UnsupportedFeatureError):
                await a.configure(SerialConfig(baudrate=9_600))
            assert a.config is original
        finally:
            await a.aclose()
            await b.aclose()

    async def test_configure_serializes_concurrent_callers(
        self,
        pair: tuple[SerialPort, SerialPort],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Two tasks racing configure() must serialize via _configure_lock:
        # the backend observes one full apply at a time and the stream's
        # final config is one of the two values, not a torn mix.
        from anyserial._mock.backend import MockBackend  # noqa: PLC0415

        a, _ = pair
        observed_concurrency: list[int] = []
        in_flight = 0
        original_configure = MockBackend.configure

        def observed_configure(self: MockBackend, config: SerialConfig) -> None:
            nonlocal in_flight
            in_flight += 1
            observed_concurrency.append(in_flight)
            try:
                original_configure(self, config)
            finally:
                in_flight -= 1

        monkeypatch.setattr(MockBackend, "configure", observed_configure)

        cfg_a = SerialConfig(baudrate=9_600)
        cfg_b = SerialConfig(baudrate=19_200)
        async with anyio.create_task_group() as tg:
            tg.start_soon(a.configure, cfg_a)
            tg.start_soon(a.configure, cfg_b)

        # ``configure`` on the mock is synchronous, so observed_configure
        # can never see two concurrent entries even without the lock —
        # but the assertion still catches a future mock that yields
        # inside configure. The stronger invariant is that both cfg
        # applies completed and the final value is one of the two.
        assert max(observed_concurrency) == 1
        assert a.config in (cfg_a, cfg_b)

    async def test_configure_does_not_block_in_flight_io(
        self,
        pair: tuple[SerialPort, SerialPort],
    ) -> None:
        # The configure lock is separate from the send / receive guards,
        # so a reconfigure may land while a reader is parked in
        # wait_readable. Both must reach their expected terminal state.
        a, b = pair
        received: list[bytes] = []

        async def reader() -> None:
            data = await b.receive(16)
            received.append(data)

        async def reconfigure() -> None:
            await a.configure(SerialConfig(baudrate=57_600))

        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(reconfigure)
            # Give both tasks a scheduler turn before we wake the reader
            # by sending through a; then drain.
            await checkpoint()
            await a.send(b"hello-world")

        assert received == [b"hello-world"]
        assert a.config.baudrate == 57_600


class TestTypedAttributes:
    async def test_fileno_attribute(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        fd = a.extra(FileStreamAttribute.fileno)
        assert isinstance(fd, int)
        assert fd >= 0

    async def test_config_attribute(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        cfg = a.extra(SerialStreamAttribute.config)
        assert isinstance(cfg, SerialConfig)

    async def test_capabilities_attribute(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        caps = a.extra(SerialStreamAttribute.capabilities)
        assert caps.platform == "mock"

    async def test_path_attribute(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        assert a.extra(FileStreamAttribute.path) == Path(a.path)


class TestModemLines:
    async def test_modem_lines_reflect_peer(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        await b.set_control_lines(rts=True)
        lines = await a.modem_lines()
        assert lines.cts is True

    async def test_set_control_lines_after_close_raises(self) -> None:
        a, b = serial_port_pair()
        try:
            await a.aclose()
            with pytest.raises(SerialClosedError):
                await a.set_control_lines(rts=True)
        finally:
            await b.aclose()


class TestBreakAndDrain:
    async def test_send_break_asserts_and_deasserts(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, _ = pair
        # Fast break so the test is quick.
        await a.send_break(duration=0.01)
        assert a._backend.break_asserted is False  # type: ignore[union-attr]

    async def test_send_break_negative_raises(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        with pytest.raises(ValueError, match="non-negative"):
            await a.send_break(-0.1)

    async def test_drain_returns_immediately_when_nothing_pending(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        a, _ = pair
        await a.drain()  # Mock reports output_waiting == 0, returns promptly.

    async def test_send_eof_is_idempotent(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        await a.send_eof()
        await a.send_eof()


class TestFinalization:
    async def test_leaked_open_port_emits_resource_warning(self) -> None:
        """DESIGN §15: garbage-collecting an open port must warn, not silently close."""
        a, b = serial_port_pair()
        try:
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always", ResourceWarning)
                # Drop the only strong reference and force collection.
                del a
                gc.collect()
            assert any(issubclass(w.category, ResourceWarning) for w in captured), (
                f"expected ResourceWarning; got {[w.category.__name__ for w in captured]}"
            )
        finally:
            await b.aclose()

    async def test_cleanly_closed_port_does_not_warn(self) -> None:
        a, b = serial_port_pair()
        await a.aclose()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            del a
            gc.collect()
        try:
            assert not any(issubclass(w.category, ResourceWarning) for w in captured), (
                "properly closed port must not warn"
            )
        finally:
            await b.aclose()


import sys as _sys  # noqa: E402 — re-imported here for inline monkeypatch usage


class TestEntryPoint:
    # Every shipped platform now has a real backend; the
    # UnsupportedPlatformError path is exercised by monkeypatching
    # sys.platform to a value the selector doesn't handle. Real-fd
    # behaviour against shipped platforms is covered by the pty-backed
    # integration tests under tests/integration/.

    async def test_open_serial_port_raises_on_unsupported_platform(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Every shipped platform now has a backend, so to actually reach
        # the UnsupportedPlatformError branch we have to lie about
        # sys.platform. The selector reads it at call time.
        monkeypatch.setattr(_sys, "platform", "haiku")
        with pytest.raises(UnsupportedPlatformError):
            await open_serial_port("/dev/ttyX")

    async def test_serial_connectable_is_bytestream_connectable(self) -> None:
        conn = SerialConnectable(path="/dev/ttyX")
        assert isinstance(conn, anyio.abc.ByteStreamConnectable)

    async def test_serial_connectable_opens_via_connect(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_sys, "platform", "haiku")
        conn = SerialConnectable(path="/dev/ttyX")
        with pytest.raises(UnsupportedPlatformError):
            await conn.connect()
