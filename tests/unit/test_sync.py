"""Unit tests for :mod:`anyserial.sync`.

The sync wrapper is a thin delegation layer over the async
:class:`anyserial.SerialPort`; the tests drive it against
:class:`MockBackend` pairs so we exercise the portal plumbing end-to-end
without opening a real kernel fd.
"""

from __future__ import annotations

import gc
import threading
import warnings
from typing import TYPE_CHECKING

import pytest

from anyserial import (
    SerialClosedError,
    SerialConfig,
    SerialDisconnectedError,
    SerialStreamAttribute,
)
from anyserial import sync as _sync
from anyserial.sync import SerialConnectable, SerialPort, configure_portal
from anyserial.testing import serial_port_pair

_get_provider = _sync._get_provider  # pyright: ignore[reportPrivateUsage]
_reset_portal_for_testing = _sync._reset_portal_for_testing  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Iterator


def _sync_port_pair() -> tuple[SerialPort, SerialPort]:
    """Wrap a :func:`serial_port_pair` in two sync ports sharing the portal."""
    async_a, async_b = serial_port_pair()
    provider = _get_provider()
    portal_a = provider.__enter__()
    try:
        portal_b = provider.__enter__()
    except BaseException:
        provider.__exit__(None, None, None)
        raise
    sync_a = SerialPort(async_a, portal=portal_a, provider=provider)
    sync_b = SerialPort(async_b, portal=portal_b, provider=provider)
    return sync_a, sync_b


@pytest.fixture
def pair() -> Iterator[tuple[SerialPort, SerialPort]]:
    a, b = _sync_port_pair()
    try:
        yield a, b
    finally:
        a.close()
        b.close()


class TestBasicIO:
    def test_round_trip(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        a.send(b"ping\n")
        data = b.receive(1024)
        assert data == b"ping\n"

    def test_receive_into_zero_copy(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        a.send(b"buffer-me")
        buf = bytearray(16)
        count = b.receive_into(buf)
        assert count > 0
        assert buf[:count] == b"buffer-me"[:count]

    def test_receive_available(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        a.send(b"abc")
        data = b.receive_available()
        assert data == b"abc"

    def test_send_buffer_accepts_memoryview(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        a.send_buffer(memoryview(b"view-bytes"))
        assert b.receive(64) == b"view-bytes"

    def test_empty_send_is_noop(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        a.send(b"")  # must not raise

    def test_receive_rejects_zero_max_bytes(self, pair: tuple[SerialPort, SerialPort]) -> None:
        _, b = pair
        with pytest.raises(ValueError, match="max_bytes"):
            b.receive(0)


class TestTimeout:
    def test_receive_timeout_raises(self, pair: tuple[SerialPort, SerialPort]) -> None:
        _, b = pair
        # Nothing queued; receive blocks until timeout.
        with pytest.raises(TimeoutError):
            b.receive(16, timeout=0.1)

    def test_timeout_does_not_leave_guard_held(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        with pytest.raises(TimeoutError):
            b.receive(16, timeout=0.05)
        # After the timeout the guard must be released — a subsequent
        # receive on the same port must succeed.
        a.send(b"after")
        assert b.receive(16) == b"after"

    def test_zero_timeout_raises(self, pair: tuple[SerialPort, SerialPort]) -> None:
        _, b = pair
        with pytest.raises(TimeoutError):
            b.receive(16, timeout=0.0)


class TestProperties:
    def test_path_and_config(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        assert a.path == "/dev/mockA"
        assert isinstance(a.config, SerialConfig)

    def test_is_open(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        assert a.is_open is True
        a.close()
        assert a.is_open is False

    def test_extra_returns_typed_attribute(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        cap = a.extra(SerialStreamAttribute.capabilities)
        assert cap == a.capabilities

    def test_extra_default_on_missing(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, _ = pair
        sentinel = object()
        # port_info is absent on mock ports — default must surface.
        assert a.extra(SerialStreamAttribute.port_info, sentinel) is sentinel


class TestLifecycle:
    def test_close_is_idempotent(self) -> None:
        a, b = _sync_port_pair()
        a.close()
        a.close()
        b.close()

    def test_operation_after_close_raises(self) -> None:
        a, b = _sync_port_pair()
        try:
            a.close()
            with pytest.raises(SerialClosedError):
                a.send(b"x")
        finally:
            b.close()

    def test_context_manager_closes(self) -> None:
        a, b = _sync_port_pair()
        with a as handle:
            assert handle is a
            handle.send(b"hi")
        assert a.is_open is False
        b.close()

    def test_connectable_shape(self) -> None:
        connectable = SerialConnectable(path="/dev/mockA")
        assert connectable.path == "/dev/mockA"
        assert isinstance(connectable.config, SerialConfig)
        # ``connect`` is callable; full open flow is exercised in the
        # pty-backed integration tests.
        assert callable(connectable.connect)


class TestResourceWarningOnLeak:
    def test_gc_emits_resource_warning(self) -> None:
        a, b = _sync_port_pair()
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                del a
                gc.collect()
            assert any(
                issubclass(w.category, ResourceWarning)
                and "unclosed sync serial port" in str(w.message)
                for w in caught
            )
        finally:
            b.close()


class TestFaultPaths:
    def test_disconnect_raises_disconnected(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        b._async_port._backend.faults.disconnected = True  # type: ignore[union-attr]
        a.send(b"gone")
        with pytest.raises(SerialDisconnectedError):
            b.receive(16)

    def test_eagain_reads_retry_transparently(self, pair: tuple[SerialPort, SerialPort]) -> None:
        a, b = pair
        b._async_port._backend.faults.eagain_reads = 2  # type: ignore[union-attr]
        a.send(b"retry")
        assert b.receive(64) == b"retry"


class TestThreadedCallers:
    def test_reader_and_writer_in_different_threads(
        self, pair: tuple[SerialPort, SerialPort]
    ) -> None:
        """One reader thread + one writer thread share the portal cleanly.

        Full-duplex I/O from different OS threads must work: each call
        dispatches through the single-portal event loop; the send-guard
        and receive-guard do not collide because they wrap disjoint code
        paths.
        """
        a, b = pair
        received: list[bytes] = []

        def writer() -> None:
            a.send(b"cross-thread", timeout=2.0)

        def reader() -> None:
            received.append(b.receive(64, timeout=2.0))

        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        r.start()
        w.start()
        for t in (w, r):
            t.join(timeout=5.0)
            assert not t.is_alive()
        assert received == [b"cross-thread"]


class TestConfigurePortalGuard:
    def test_configure_after_open_raises(self) -> None:
        # Force the provider into existence.
        a, b = _sync_port_pair()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                configure_portal(backend="asyncio")
        finally:
            a.close()
            b.close()


@pytest.fixture(autouse=True)
def _reset_portal_between_tests() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Drop the cached provider after each test so other test files start clean.

    Sync tests share a process-wide portal; we reset the reference (not
    the actual backend thread, which AnyIO shuts down via refcounting
    once the last sync port closes) to keep the configure_portal guard
    test deterministic regardless of ordering.
    """
    yield
    _reset_portal_for_testing()
