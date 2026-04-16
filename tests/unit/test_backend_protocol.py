"""Tests for :mod:`anyserial._backend`.

Exercises the ``@runtime_checkable`` Protocols and the platform selector.
The tests use tiny hand-written stubs rather than :class:`MockBackend` —
we want to catch drift in the Protocol shape itself.
"""

from __future__ import annotations

import sys

import pytest

from anyserial._backend import AsyncSerialBackend, SyncSerialBackend, select_backend
from anyserial._types import Capability, ModemLines
from anyserial.capabilities import SerialCapabilities
from anyserial.config import SerialConfig
from anyserial.exceptions import UnsupportedPlatformError

# Routed through a module-level bool so the skipif expressions don't trip
# mypy's sys.platform narrowing (which would flag post-skip code as
# unreachable on whichever platform the type-check is run on).
_IS_LINUX: bool = sys.platform.startswith("linux")


def _caps() -> SerialCapabilities:
    return SerialCapabilities(
        platform="test",
        backend="stub",
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


class _SyncStub:
    """Minimal object structurally conforming to :class:`SyncSerialBackend`."""

    def __init__(self) -> None:
        self._open = False

    @property
    def path(self) -> str:
        return "/dev/null"

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def capabilities(self) -> SerialCapabilities:
        return _caps()

    def open(self, path: str, config: SerialConfig) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def fileno(self) -> int:
        return -1

    def read_nonblocking(self, buffer: bytearray | memoryview) -> int:
        return 0

    def write_nonblocking(self, data: memoryview) -> int:
        return len(data)

    def configure(self, config: SerialConfig) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def reset_output_buffer(self) -> None:
        pass

    def set_break(self, on: bool) -> None:
        pass

    def tcdrain_blocking(self) -> None:
        pass

    def modem_lines(self) -> ModemLines:
        return ModemLines(cts=False, dsr=False, ri=False, cd=False)

    def set_control_lines(self, *, rts: bool | None = None, dtr: bool | None = None) -> None:
        pass

    def input_waiting(self) -> int:
        return 0

    def output_waiting(self) -> int:
        return 0


class _AsyncStub:
    """Minimal object structurally conforming to :class:`AsyncSerialBackend`."""

    def __init__(self) -> None:
        self._open = False

    @property
    def path(self) -> str:
        return "COM1"

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def capabilities(self) -> SerialCapabilities:
        return _caps()

    async def open(self, path: str, config: SerialConfig) -> None:
        self._open = True

    async def aclose(self) -> None:
        self._open = False

    async def receive(self, max_bytes: int) -> bytes:
        return b""

    async def receive_into(self, buffer: bytearray | memoryview) -> int:
        return 0

    async def send(self, data: memoryview) -> None:
        pass

    async def configure(self, config: SerialConfig) -> None:
        pass

    async def reset_input_buffer(self) -> None:
        pass

    async def reset_output_buffer(self) -> None:
        pass

    async def drain(self) -> None:
        pass

    async def send_break(self, duration: float) -> None:
        pass

    async def modem_lines(self) -> ModemLines:
        return ModemLines(cts=False, dsr=False, ri=False, cd=False)

    async def set_control_lines(self, *, rts: bool | None = None, dtr: bool | None = None) -> None:
        pass

    def input_waiting(self) -> int:
        return 0

    def output_waiting(self) -> int:
        return 0


class TestRuntimeCheckable:
    def test_sync_stub_passes(self) -> None:
        assert isinstance(_SyncStub(), SyncSerialBackend)

    def test_async_stub_passes(self) -> None:
        assert isinstance(_AsyncStub(), AsyncSerialBackend)

    def test_random_object_fails_sync(self) -> None:
        assert not isinstance(object(), SyncSerialBackend)

    def test_random_object_fails_async(self) -> None:
        assert not isinstance(object(), AsyncSerialBackend)

    def test_sync_and_async_are_disjoint_shapes(self) -> None:
        # Sync stub has no ``aclose`` / async methods, so it must not
        # masquerade as AsyncSerialBackend.
        assert not isinstance(_SyncStub(), AsyncSerialBackend)
        # And the async stub has no ``close`` / ``fileno``.
        assert not isinstance(_AsyncStub(), SyncSerialBackend)


class TestSelector:
    @pytest.mark.skipif(not _IS_LINUX, reason="Linux-only branch")
    def test_returns_linux_backend_on_linux(self) -> None:
        from anyserial._linux.backend import LinuxBackend  # noqa: PLC0415 — scoped

        backend = select_backend("/dev/ttyX", SerialConfig())
        assert isinstance(backend, LinuxBackend)
        # The returned backend is constructed unopened — callers (the
        # ``open_serial_port`` orchestrator) drive ``open(path, config)``.
        assert not backend.is_open

    def test_raises_unsupported_platform_on_unwired_platforms(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the unsupported-platform branch regardless of the host OS by
        # monkeypatching sys.platform to a value the selector doesn't handle.
        # Every real platform we ship now has a backend, so the only way to
        # reach this branch in test is to lie about the platform.
        monkeypatch.setattr(sys, "platform", "haiku")
        with pytest.raises(UnsupportedPlatformError):
            select_backend("/dev/ttyX", SerialConfig())

    def test_error_message_includes_path_on_unwired_platforms(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "platform", "haiku")
        try:
            select_backend("/dev/ttyWEIRD", SerialConfig())
        except UnsupportedPlatformError as exc:
            assert "/dev/ttyWEIRD" in str(exc)
        else:
            msg = "selector must raise UnsupportedPlatformError"
            raise AssertionError(msg)
