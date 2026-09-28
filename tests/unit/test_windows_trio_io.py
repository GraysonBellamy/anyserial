# pyright: reportPrivateUsage=false
"""Tests for :mod:`anyserial._windows._trio_io`.

Trio's overlapped-I/O helpers exist only on Windows, so these tests
replace them on ``trio.lowlevel`` (and replace the kernel32 loader) and
run on every platform.

- An idle read under the "wait-for-any" ``COMMTIMEOUTS`` policy completes
  with ``STATUS_TIMEOUT``, which Trio raises as ``ERROR_TIMEOUT``. It must
  reach the backend's read loop as an empty completion, not an error.
- ``wait_comm_event`` must hand Trio the ``OVERLAPPED``'s address (Trio
  keys its waiters by it) and wait for the completion packet whether
  ``WaitCommEvent`` pends or finishes synchronously.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest
import trio

from anyserial._windows import _trio_io
from anyserial._windows import _win32 as w
from anyserial._windows.backend import WindowsBackend
from anyserial.exceptions import SerialClosedError, SerialDisconnectedError, SerialError

pytestmark = pytest.mark.anyio

_ERROR_CRC = 23


def _winerror(code: int) -> OSError:
    """Return an ``OSError`` shaped like the one Trio raises for ``code``.

    ``OSError``'s fourth constructor argument sets ``winerror`` on Windows
    only, so the attribute is assigned directly to build the same object on
    every platform.
    """
    exc = OSError(0, f"winerror {code}")
    exc.winerror = code  # type: ignore[attr-defined]
    return exc


class _ReadScript:
    """Stand-in for ``trio.lowlevel.readinto_overlapped``.

    Each call consumes the next outcome: bytes are copied into the caller's
    buffer, an ``OSError`` is raised.
    """

    def __init__(self, *outcomes: bytes | OSError) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self, handle: int, buffer: bytearray | memoryview) -> int:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, OSError):
            raise outcome
        memoryview(buffer)[: len(outcome)] = outcome
        return len(outcome)


@pytest.fixture
def read_script(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a :class:`_ReadScript` built from the outcomes given."""

    def install(*outcomes: bytes | OSError) -> _ReadScript:
        script = _ReadScript(*outcomes)
        monkeypatch.setattr(trio.lowlevel, "readinto_overlapped", script, raising=False)
        return script

    return install


def _open_trio_backend() -> WindowsBackend:
    """Return a :class:`WindowsBackend` in the state ``open()`` leaves under Trio."""
    backend = WindowsBackend()
    backend._path = "COM8"
    backend._handle = 0x1234
    backend._runtime = "trio"
    backend._open = True
    return backend


class TestReadinto:
    async def test_timeout_is_an_empty_read(self, read_script: Any) -> None:
        read_script(_winerror(w.ERROR_TIMEOUT))
        assert await _trio_io.readinto(0x1234, bytearray(8)) == 0

    async def test_data_is_returned(self, read_script: Any) -> None:
        read_script(b"abc")
        buffer = bytearray(8)
        assert await _trio_io.readinto(0x1234, buffer) == 3
        assert buffer[:3] == b"abc"

    @pytest.mark.parametrize(
        "code",
        [w.ERROR_GEN_FAILURE, w.ERROR_DEVICE_REMOVED, w.ERROR_OPERATION_ABORTED, _ERROR_CRC],
    )
    async def test_other_errors_propagate(self, read_script: Any, code: int) -> None:
        read_script(_winerror(code))
        with pytest.raises(OSError, match=f"winerror {code}") as info:
            await _trio_io.readinto(0x1234, bytearray(8))
        assert getattr(info.value, "winerror", None) == code


class TestBackendReadLoop:
    async def test_receive_reissues_after_timeouts(self, read_script: Any) -> None:
        script = read_script(_winerror(w.ERROR_TIMEOUT), _winerror(w.ERROR_TIMEOUT), b"data")
        backend = _open_trio_backend()
        assert await backend.receive(16) == b"data"
        assert script.calls == 3

    async def test_receive_into_reissues_after_a_timeout(self, read_script: Any) -> None:
        script = read_script(_winerror(w.ERROR_TIMEOUT), b"xy")
        backend = _open_trio_backend()
        buffer = bytearray(4)
        assert await backend.receive_into(buffer) == 2
        assert buffer[:2] == b"xy"
        assert script.calls == 2

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            (w.ERROR_GEN_FAILURE, SerialDisconnectedError),
            (w.ERROR_DEVICE_REMOVED, SerialDisconnectedError),
            (w.ERROR_OPERATION_ABORTED, SerialClosedError),
            (_ERROR_CRC, SerialError),
        ],
    )
    async def test_other_errors_still_fail_the_read(
        self,
        read_script: Any,
        code: int,
        expected: type[SerialError],
    ) -> None:
        read_script(_winerror(code))
        backend = _open_trio_backend()
        with pytest.raises(expected) as info:
            await backend.receive(16)
        assert type(info.value) is expected
        assert getattr(info.value, "winerror", None) == code


class _FakeKernel32:
    """Records the ``WaitCommEvent`` call and returns a scripted result."""

    def __init__(self, result: int) -> None:
        self._result = result
        self.mask: Any = None
        self.overlapped_address: int | None = None

    def WaitCommEvent(self, handle: int, mask: Any, overlapped: Any) -> int:  # noqa: N802 — Win32 name
        self.mask = mask._obj
        self.overlapped_address = ctypes.addressof(overlapped._obj)
        return self._result


class _WaitRecorder:
    """Stand-in for ``trio.lowlevel.wait_overlapped``.

    Records the ``lpOverlapped`` argument and, like the kernel, writes the
    event mask before the completion is delivered.
    """

    def __init__(self, kernel32: _FakeKernel32, mask: int) -> None:
        self._kernel32 = kernel32
        self._mask = mask
        self.arguments: list[object] = []

    async def __call__(self, handle: int, overlapped: object) -> None:
        self.arguments.append(overlapped)
        self._kernel32.mask.value = self._mask


def _install_wait_comm_event(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: int,
    mask: int,
) -> tuple[_FakeKernel32, _WaitRecorder]:
    kernel32 = _FakeKernel32(result)
    recorder = _WaitRecorder(kernel32, mask)
    monkeypatch.setattr(w, "load_kernel32", lambda: kernel32)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: w.ERROR_IO_PENDING, raising=False)
    monkeypatch.setattr(trio.lowlevel, "wait_overlapped", recorder, raising=False)
    return kernel32, recorder


class TestWaitCommEvent:
    async def test_pending_wait_passes_the_overlapped_address(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kernel32, recorder = _install_wait_comm_event(monkeypatch, result=0, mask=w.EV_CTS)
        assert await _trio_io.wait_comm_event(0x1234) == w.EV_CTS
        assert recorder.arguments == [kernel32.overlapped_address]
        assert isinstance(recorder.arguments[0], int)

    async def test_synchronous_completion_still_waits_for_the_packet(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kernel32, recorder = _install_wait_comm_event(monkeypatch, result=1, mask=w.EV_RING)
        assert await _trio_io.wait_comm_event(0x1234) == w.EV_RING
        assert recorder.arguments == [kernel32.overlapped_address]
