# pyright: reportPrivateUsage=false
"""Tests for the :class:`SerialPort` ``__new__`` dispatch and the
``_AsyncBackendSerialPort`` variant.

The toy backend below satisfies :class:`AsyncSerialBackend` structurally
with in-memory queues for ``receive`` / ``send``, so we can exercise the
async-dispatch path end-to-end without any platform-specific code.

Companion to ``tests/unit/test_backend_protocol.py``: that file pins the
Protocol shape; this file pins the ``SerialPort`` dispatch behaviour and
the ``_AsyncBackendSerialPort`` semantics (guards, close-lock idempotency,
typed attributes, EOF translation).
"""

from __future__ import annotations

import sys

import anyio
import pytest
from anyio.streams.file import FileStreamAttribute

from anyserial._backend import AsyncSerialBackend, SyncSerialBackend
from anyserial._types import Capability, ModemLines
from anyserial.capabilities import SerialCapabilities, SerialStreamAttribute
from anyserial.config import SerialConfig
from anyserial.exceptions import (
    SerialClosedError,
    SerialDisconnectedError,
    UnsupportedPlatformError,
)
from anyserial.stream import (
    SerialPort,
    _AsyncBackendSerialPort,
    _PosixSerialPort,
    open_serial_port,
)

# Run the async tests across the full backend matrix from tests/conftest.py
# (asyncio, asyncio+uvloop, trio).
pytestmark = pytest.mark.anyio


def _caps() -> SerialCapabilities:
    return SerialCapabilities(
        platform="test",
        backend="toy-async",
        custom_baudrate=Capability.UNKNOWN,
        mark_space_parity=Capability.UNKNOWN,
        one_point_five_stop_bits=Capability.UNKNOWN,
        xon_xoff=Capability.UNKNOWN,
        rts_cts=Capability.UNKNOWN,
        dtr_dsr=Capability.UNKNOWN,
        modem_lines=Capability.UNKNOWN,
        break_signal=Capability.UNKNOWN,
        exclusive_access=Capability.UNKNOWN,
        low_latency=Capability.UNKNOWN,
        rs485=Capability.UNKNOWN,
        input_waiting=Capability.UNKNOWN,
        output_waiting=Capability.UNKNOWN,
        port_discovery=Capability.UNKNOWN,
    )


class _ToyAsyncBackend:
    """Minimal :class:`AsyncSerialBackend` driving an in-memory pipe.

    ``receive`` parks on an :class:`anyio.Event` until ``feed()`` is called,
    so we can deterministically test cancellation mid-receive without races.
    ``send`` appends to ``written`` so tests can assert payload integrity.
    """

    def __init__(self, *, path: str = "TOY1") -> None:
        self._path = path
        # Tests construct the backend already-opened so the fixture can be
        # synchronous (pytest core has no native async fixture support;
        # AnyIO's plugin only converts async tests, not async fixtures).
        self._open = True
        self._caps = _caps()
        self._inbox = bytearray()
        self._inbox_event = anyio.Event()
        self.written = bytearray()
        self.aclose_calls = 0
        self.configure_calls: list[SerialConfig] = []
        self.reset_input_calls = 0
        self.reset_output_calls = 0
        self.drain_calls = 0
        self.send_break_calls: list[float] = []
        self.set_control_calls: list[tuple[bool | None, bool | None]] = []

    # Synchronous test helpers (not part of the Protocol)
    def feed(self, data: bytes) -> None:
        self._inbox.extend(data)
        # Fire and reset so subsequent ``receive`` calls park again.
        event, self._inbox_event = self._inbox_event, anyio.Event()
        event.set()

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def capabilities(self) -> SerialCapabilities:
        return self._caps

    async def open(self, path: str, config: SerialConfig) -> None:
        self._open = True

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self._open = False
        # Wake any parked ``receive`` so a concurrent reader sees EOF
        # instead of hanging forever.
        self._inbox_event.set()

    async def receive(self, max_bytes: int) -> bytes:
        while not self._inbox:
            if not self._open:
                return b""
            await self._inbox_event.wait()
        chunk = bytes(self._inbox[:max_bytes])
        del self._inbox[:max_bytes]
        return chunk

    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        data = await self.receive(len(buffer))
        n = len(data)
        buffer[:n] = data
        return n

    async def send(self, data: memoryview) -> None:
        self.written.extend(bytes(data))

    async def configure(self, config: SerialConfig) -> None:
        self.configure_calls.append(config)

    async def reset_input_buffer(self) -> None:
        self.reset_input_calls += 1
        self._inbox.clear()

    async def reset_output_buffer(self) -> None:
        self.reset_output_calls += 1
        self.written.clear()

    async def drain(self) -> None:
        self.drain_calls += 1

    async def send_break(self, duration: float) -> None:
        self.send_break_calls.append(duration)

    async def modem_lines(self) -> ModemLines:
        return ModemLines(cts=True, dsr=False, ri=False, cd=True)

    async def set_control_lines(
        self,
        *,
        rts: bool | None = None,
        dtr: bool | None = None,
    ) -> None:
        self.set_control_calls.append((rts, dtr))

    def input_waiting(self) -> int:
        return len(self._inbox)

    def output_waiting(self) -> int:
        return 0


@pytest.fixture
def toy_port() -> _AsyncBackendSerialPort:
    backend = _ToyAsyncBackend()
    port = SerialPort(backend, SerialConfig())
    assert isinstance(port, _AsyncBackendSerialPort)
    return port


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_sync_backend_constructs_posix_variant(self) -> None:
        # MockBackend (used by every sync test) satisfies SyncSerialBackend.
        from anyserial.testing import MockBackend  # noqa: PLC0415 — local import

        a, b = MockBackend.pair()
        try:
            a.open(a.path, SerialConfig())
            assert isinstance(a, SyncSerialBackend)
            port = SerialPort(a, SerialConfig())
            assert isinstance(port, _PosixSerialPort)
            assert isinstance(port, SerialPort)
            await port.aclose()
        finally:
            b.close()

    async def test_async_backend_constructs_async_variant(self) -> None:
        backend = _ToyAsyncBackend()
        assert isinstance(backend, AsyncSerialBackend)
        port = SerialPort(backend, SerialConfig())
        assert isinstance(port, _AsyncBackendSerialPort)
        assert isinstance(port, SerialPort)
        await port.aclose()

    def test_non_backend_raises_typeerror(self) -> None:
        with pytest.raises(TypeError, match="SyncSerialBackend or AsyncSerialBackend"):
            SerialPort(object(), SerialConfig())  # type: ignore[arg-type]

    async def test_subclass_construction_skips_dispatch(self) -> None:
        # Direct ``_AsyncBackendSerialPort(...)`` (the path used by
        # ``open_serial_port``) bypasses the dispatch and never re-runs
        # the isinstance probes.
        backend = _ToyAsyncBackend()
        port = _AsyncBackendSerialPort(backend, SerialConfig())
        assert type(port) is _AsyncBackendSerialPort
        await port.aclose()


# ---------------------------------------------------------------------------
# Typed attributes
# ---------------------------------------------------------------------------


class TestTypedAttributes:
    async def test_async_variant_omits_fileno(self, toy_port: _AsyncBackendSerialPort) -> None:
        # Async backends have no integer fd. Per design-windows-backend
        # §24.5 the typed attribute is omitted entirely so generic AnyIO
        # code can branch via ``port.extra(fileno, default=None)``.
        with pytest.raises(anyio.TypedAttributeLookupError):
            toy_port.extra(FileStreamAttribute.fileno)
        await toy_port.aclose()

    async def test_async_variant_keeps_path_and_capabilities(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        from pathlib import Path  # noqa: PLC0415 — narrow import for one assert

        assert toy_port.extra(FileStreamAttribute.path) == Path("TOY1")
        assert toy_port.extra(SerialStreamAttribute.capabilities).backend == "toy-async"
        assert toy_port.extra(SerialStreamAttribute.config) == toy_port.config
        await toy_port.aclose()


# ---------------------------------------------------------------------------
# Hot path: receive / send / receive_into / receive_available
# ---------------------------------------------------------------------------


class TestHotPath:
    async def test_send_round_trip(self, toy_port: _AsyncBackendSerialPort) -> None:
        await toy_port.send(b"hello")
        await toy_port.send(b" world")
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        assert bytes(backend.written) == b"hello world"
        await toy_port.aclose()

    async def test_send_buffer_normalises_views(self, toy_port: _AsyncBackendSerialPort) -> None:
        # PEP 688 buffer protocol: bytearray is a 1-byte itemsize view.
        await toy_port.send_buffer(bytearray(b"abc"))
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        assert bytes(backend.written) == b"abc"
        await toy_port.aclose()

    async def test_receive_returns_fed_data(self, toy_port: _AsyncBackendSerialPort) -> None:
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        backend.feed(b"abc")
        chunk = await toy_port.receive(16)
        assert chunk == b"abc"
        await toy_port.aclose()

    async def test_receive_into_writes_caller_buffer(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        backend.feed(b"xyz")
        buf = bytearray(8)
        n = await toy_port.receive_into(buf)
        assert n == 3
        assert bytes(buf[:n]) == b"xyz"
        await toy_port.aclose()

    async def test_receive_available_uses_input_waiting_hint(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        backend.feed(b"hello")
        # input_waiting() reports 5 → one receive call returns the full buffer.
        chunk = await toy_port.receive_available()
        assert chunk == b"hello"
        await toy_port.aclose()

    async def test_receive_after_close_raises_disconnect(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        # Toy backend's ``receive`` returns b"" once not-open and inbox empty;
        # ``_AsyncBackendSerialPort.receive`` translates that to a clean
        # SerialDisconnectedError, matching the POSIX EOF contract.
        async with anyio.create_task_group() as tg:

            async def reader() -> None:
                with pytest.raises(SerialDisconnectedError):
                    await toy_port.receive(16)

            tg.start_soon(reader)
            await anyio.sleep(0.01)
            await toy_port.aclose()

    async def test_receive_zero_max_bytes_raises_value_error(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        with pytest.raises(ValueError, match="max_bytes must be positive"):
            await toy_port.receive(0)
        await toy_port.aclose()

    async def test_receive_into_empty_buffer_raises_value_error(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        with pytest.raises(ValueError, match="buffer must be non-empty"):
            await toy_port.receive_into(bytearray())
        await toy_port.aclose()


# ---------------------------------------------------------------------------
# Resource guards & lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_aclose_is_idempotent(self, toy_port: _AsyncBackendSerialPort) -> None:
        await toy_port.aclose()
        await toy_port.aclose()
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        # The shielded close path runs the backend teardown exactly once.
        assert backend.aclose_calls == 1

    async def test_send_after_close_raises_serial_closed(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        await toy_port.aclose()
        with pytest.raises(SerialClosedError):
            await toy_port.send(b"x")

    async def test_concurrent_receive_raises_busy_resource(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        # Two receives racing on the same port must trip the resource guard.
        async with anyio.create_task_group() as tg:
            tg.start_soon(toy_port.receive, 16)
            await anyio.sleep(0.01)
            with pytest.raises(anyio.BusyResourceError):
                await toy_port.receive(16)
            tg.cancel_scope.cancel()
        await toy_port.aclose()

    async def test_async_context_manager_closes(self) -> None:
        backend = _ToyAsyncBackend()
        async with SerialPort(backend, SerialConfig()) as port:
            assert port.is_open
        assert backend.aclose_calls == 1


# ---------------------------------------------------------------------------
# Control plane delegation
# ---------------------------------------------------------------------------


class TestControlDelegation:
    async def test_configure_updates_local_and_calls_backend(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        new_cfg = SerialConfig(baudrate=19200)
        await toy_port.configure(new_cfg)
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        assert backend.configure_calls == [new_cfg]
        assert toy_port.config == new_cfg
        await toy_port.aclose()

    async def test_drain_and_drain_exact_both_call_backend_drain(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        await toy_port.drain()
        await toy_port.drain_exact()
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        # Per design-windows-backend §7: async backends collapse drain /
        # drain_exact to the single Protocol method.
        assert backend.drain_calls == 2
        await toy_port.aclose()

    async def test_send_break_delegates_with_duration(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        await toy_port.send_break(0.05)
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        assert backend.send_break_calls == [0.05]
        await toy_port.aclose()

    async def test_send_break_negative_raises(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        with pytest.raises(ValueError, match="duration must be non-negative"):
            await toy_port.send_break(-1.0)
        await toy_port.aclose()

    async def test_modem_and_control_lines(self, toy_port: _AsyncBackendSerialPort) -> None:
        snap = await toy_port.modem_lines()
        assert snap.cts is True
        assert snap.cd is True
        await toy_port.set_control_lines(rts=True, dtr=False)
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        assert backend.set_control_calls == [(True, False)]
        await toy_port.aclose()

    async def test_input_output_waiting_pass_through(
        self,
        toy_port: _AsyncBackendSerialPort,
    ) -> None:
        backend = toy_port._backend
        assert isinstance(backend, _ToyAsyncBackend)
        backend.feed(b"abcd")
        assert toy_port.input_waiting() == 4
        assert toy_port.output_waiting() == 0
        await toy_port.aclose()


# ---------------------------------------------------------------------------
# Windows skeleton dispatch — proves selector → open_serial_port → async
# variant → backend.open() → UnsupportedPlatformError chain
# ---------------------------------------------------------------------------


class TestWindowsSkeleton:
    def test_selector_returns_windows_backend_on_win32(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from anyserial._backend import select_backend  # noqa: PLC0415 — scoped
        from anyserial._windows.backend import WindowsBackend  # noqa: PLC0415 — scoped

        monkeypatch.setattr(sys, "platform", "win32")
        backend = select_backend("COM1", SerialConfig())
        assert isinstance(backend, WindowsBackend)
        assert isinstance(backend, AsyncSerialBackend)

    def test_windows_backend_does_not_satisfy_sync_protocol(self) -> None:
        # Defensive: the dispatch in __new__ uses isinstance against both
        # Protocols. WindowsBackend must only match AsyncSerialBackend.
        from anyserial._windows.backend import WindowsBackend  # noqa: PLC0415 — scoped

        assert not isinstance(WindowsBackend(), SyncSerialBackend)

    async def test_open_serial_port_on_win32_raises_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Full dispatch trip: select_backend → AsyncSerialBackend branch in
        # open_serial_port → await backend.open(). On a non-Windows CI host
        # the actual failure mode depends on the running async runtime —
        # asyncio's SelectorEventLoop trips the Proactor check, Trio passes
        # the runtime probe and then fails at ``load_kernel32`` because
        # ctypes has no ``WinDLL``. Either path raises
        # :class:`UnsupportedPlatformError`, which is what we care about.
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(UnsupportedPlatformError):
            await open_serial_port("COM1")
