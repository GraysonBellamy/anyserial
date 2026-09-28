# pyright: reportPrivateUsage=false
"""Tests for the asyncio ``WaitCommEvent`` path in :mod:`anyserial._windows._asyncio_io`.

The proactor waits only on the event handle; the ``WaitCommEvent``
operation itself belongs to ``wait_comm_event``. These tests replace the
kernel32 bindings and the proactor so they run on every platform, and pin
that:

- the ``OVERLAPPED``'s ``hEvent`` has its low-order bit set, so the kernel
  queues no completion packet to the proactor's port;
- a cancelled wait cancels the operation and waits for the kernel to
  finish with the buffers before releasing them;
- buffers the kernel has not finished with are kept, not released.
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

from anyserial._windows import _asyncio_io
from anyserial._windows import _win32 as w

pytestmark = pytest.mark.anyio

_EVENT = 0x100
_PORT = 0x1234
_WAIT_TIMEOUT = 0x102


class _Interrupted(BaseException):
    """Stands in for the cancellation exception of whichever runtime runs the test."""


class _FakeKernel32:
    def __init__(self, *, wait_comm_event_result: int = 0, cancel_wait_result: int = 0) -> None:
        self._wait_comm_event_result = wait_comm_event_result
        self._cancel_wait_result = cancel_wait_result
        self.overlapped: Any = None
        self.mask: Any = None
        self.cancelled: list[int] = []
        self.waited: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def CreateEventW(self, *args: object) -> int:  # noqa: N802 — Win32 name
        return _EVENT

    def WaitCommEvent(self, handle: int, mask: Any, overlapped: Any) -> int:  # noqa: N802
        self.mask = mask._obj
        self.overlapped = overlapped._obj
        return self._wait_comm_event_result

    def CancelIoEx(self, handle: int, overlapped: Any) -> int:  # noqa: N802
        self.cancelled.append(ctypes.addressof(overlapped._obj))
        return 1

    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int:  # noqa: N802
        self.waited.append((handle, milliseconds))
        return self._cancel_wait_result

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        self.closed.append(handle)
        return 1


class _FakeProactor:
    """``wait_for_handle`` either delivers ``mask`` or raises ``error``."""

    def __init__(
        self, kernel32: _FakeKernel32, *, mask: int = 0, error: BaseException | None = None
    ) -> None:
        self._kernel32 = kernel32
        self._mask = mask
        self._error = error
        self.waited_on: list[int] = []

    async def wait_for_handle(self, handle: int) -> None:
        self.waited_on.append(handle)
        if self._error is not None:
            raise self._error
        self._kernel32.mask.value = self._mask


@pytest.fixture
def abandoned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, object, int]]:
    waits: list[tuple[object, object, int]] = []
    monkeypatch.setattr(_asyncio_io, "_abandoned_waits", waits)
    return waits


def _install(
    monkeypatch: pytest.MonkeyPatch,
    kernel32: _FakeKernel32,
    proactor: _FakeProactor,
) -> None:
    monkeypatch.setattr(w, "load_kernel32", lambda: kernel32)
    monkeypatch.setattr(_asyncio_io, "_running_proactor", lambda: proactor)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: w.ERROR_IO_PENDING, raising=False)


async def test_pending_wait_returns_the_event_mask(
    monkeypatch: pytest.MonkeyPatch,
    abandoned: list[tuple[object, object, int]],
) -> None:
    kernel32 = _FakeKernel32()
    proactor = _FakeProactor(kernel32, mask=w.EV_DSR)
    _install(monkeypatch, kernel32, proactor)

    assert await _asyncio_io.wait_comm_event(_PORT) == w.EV_DSR
    assert proactor.waited_on == [_EVENT]
    assert kernel32.cancelled == []
    assert kernel32.closed == [_EVENT]
    assert abandoned == []


async def test_completion_port_packet_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    abandoned: list[tuple[object, object, int]],
) -> None:
    kernel32 = _FakeKernel32()
    _install(monkeypatch, kernel32, _FakeProactor(kernel32))

    await _asyncio_io.wait_comm_event(_PORT)
    assert kernel32.overlapped.hEvent == _EVENT | 1


async def test_synchronous_completion_does_not_wait(
    monkeypatch: pytest.MonkeyPatch,
    abandoned: list[tuple[object, object, int]],
) -> None:
    kernel32 = _FakeKernel32(wait_comm_event_result=1)
    proactor = _FakeProactor(kernel32)
    _install(monkeypatch, kernel32, proactor)

    assert await _asyncio_io.wait_comm_event(_PORT) == 0
    assert proactor.waited_on == []
    assert kernel32.closed == [_EVENT]


async def test_cancelled_wait_cancels_the_operation_before_releasing_it(
    monkeypatch: pytest.MonkeyPatch,
    abandoned: list[tuple[object, object, int]],
) -> None:
    kernel32 = _FakeKernel32(cancel_wait_result=w.WAIT_OBJECT_0)
    _install(monkeypatch, kernel32, _FakeProactor(kernel32, error=_Interrupted()))

    with pytest.raises(_Interrupted):
        await _asyncio_io.wait_comm_event(_PORT)
    assert kernel32.cancelled == [ctypes.addressof(kernel32.overlapped)]
    assert kernel32.waited == [(_EVENT, _asyncio_io._CANCEL_WAIT_MS)]
    assert kernel32.closed == [_EVENT]
    assert abandoned == []


async def test_unfinished_cancellation_keeps_the_buffers(
    monkeypatch: pytest.MonkeyPatch,
    abandoned: list[tuple[object, object, int]],
) -> None:
    kernel32 = _FakeKernel32(cancel_wait_result=_WAIT_TIMEOUT)
    _install(monkeypatch, kernel32, _FakeProactor(kernel32, error=_Interrupted()))

    with pytest.raises(_Interrupted):
        await _asyncio_io.wait_comm_event(_PORT)
    assert kernel32.closed == []
    assert len(abandoned) == 1
    overlapped, mask, event = abandoned[0]
    assert overlapped is kernel32.overlapped
    assert mask is kernel32.mask
    assert event == _EVENT
