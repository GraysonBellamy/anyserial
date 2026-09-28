"""asyncio Proactor overlapped-I/O hot path for the Windows backend.

design-windows-backend.md §4 — uses CPython's private but ABI-stable
``_overlapped`` module so ``ReadFileInto`` lands bytes directly in the
caller's buffer (zero-copy) and ``loop._proactor._register`` returns a
future that the proactor cancels via ``CancelIoEx`` and then awaits to
completion. The result is the same release-after-completion contract
Trio gives us.

``wait_comm_event`` (§6.4) uses a manual-reset Win32 event +
``proactor.wait_for_handle`` because ``_overlapped.Overlapped`` has no
``WaitCommEvent`` method. The kernel signals the event on completion;
the proactor's handle-wait machinery delivers the notification to the
awaiting coroutine.

We deliberately accept the private-API dependency — see §4.1 of the
design doc — because the alternative is reimplementing CPython's IOCP
completion worker, which is exactly what the proactor already does.

CPython ≥ 3.12 provides ``Overlapped.ReadFileInto``; ``anyserial`` requires
≥ 3.13 so the legacy ``ReadFile`` + copy path is never needed and is not
implemented here.
"""

from __future__ import annotations

import contextlib
from typing import Any

_NULL_HANDLE: int = 0


class HandleWrapper:
    """Wraps a raw Win32 HANDLE for CPython's proactor.

    ``IocpProactor._register_with_iocp(obj)`` calls ``obj.fileno()`` to
    obtain the OS handle and stores ``obj`` as a dict key in its internal
    completion cache.  A raw ``int`` has no ``.fileno()`` method (raising
    ``AttributeError`` at registration) and could alias Python's small-int
    cache, producing cache collisions for different handles.

    Trio's ``register_with_iocp`` accepts a raw int — this wrapper is only
    needed on the asyncio path.

    See design-windows-backend.md §3.
    """

    # ``__weakref__`` is required because CPython >= 3.12's ``IocpProactor``
    # stores registered objects in a ``weakref.WeakSet`` (Lib/asyncio/
    # windows_events.py); ``_register_with_iocp(obj)`` therefore takes a
    # weak reference to ``obj``. Slotted classes need to declare
    # ``__weakref__`` explicitly to be weak-referenceable.
    __slots__ = ("__weakref__", "_handle")

    def __init__(self, handle: int) -> None:
        self._handle = handle

    def fileno(self) -> int:
        return self._handle


def _overlapped_module() -> Any:
    import _overlapped  # type: ignore[import-not-found]  # noqa: PLC0415 — Windows-only

    return _overlapped


def _running_proactor() -> Any:
    import asyncio  # noqa: PLC0415 — lazy by runtime
    from typing import cast  # noqa: PLC0415 — local to the one use

    loop = asyncio.get_running_loop()
    # ``loop._proactor`` is a CPython-private attribute exposed only on
    # ``ProactorEventLoop`` (design §4.1). Runtime-detection in ``open()``
    # guarantees this branch is reached only with a proactor loop.
    return cast("Any", loop._proactor)  # type: ignore[attr-defined]


async def register(wrapper: HandleWrapper) -> None:
    """Associate the wrapped handle with the running proactor's completion port."""
    proactor = _running_proactor()
    # Internal API name is stable since CPython 3.4; design §4.1.
    proactor._register_with_iocp(wrapper)


async def readinto(
    handle: int,
    wrapper: HandleWrapper,
    buffer: bytearray | memoryview,
) -> int:
    """Zero-copy overlapped read into ``buffer``; returns bytes read.

    ``wrapper`` is passed to ``proactor._register`` so the proactor's
    internal cache stays keyed on the same object used at registration.
    The raw ``handle`` int is passed to ``ReadFileInto`` (kernel API).

    The caller is responsible for keeping ``buffer`` alive until this
    coroutine returns or the cancellation completion packet is delivered;
    both conditions are satisfied by the await staying in scope.
    """
    proactor = _running_proactor()
    overlapped_mod = _overlapped_module()
    ov = overlapped_mod.Overlapped(_NULL_HANDLE)
    ov.ReadFileInto(handle, buffer)
    return await proactor._register(ov, wrapper, _read_callback)  # type: ignore[no-any-return]


async def write(
    handle: int,
    wrapper: HandleWrapper,
    data: bytes | memoryview,
) -> int:
    """Overlapped write; returns the number of bytes accepted by the kernel."""
    proactor = _running_proactor()
    overlapped_mod = _overlapped_module()
    ov = overlapped_mod.Overlapped(_NULL_HANDLE)
    ov.WriteFile(handle, data)
    return await proactor._register(ov, wrapper, _write_callback)  # type: ignore[no-any-return]


def _read_callback(transferred: int, key: int, ov: Any) -> int:
    return ov.getresult()  # type: ignore[no-any-return]


def _write_callback(transferred: int, key: int, ov: Any) -> int:
    return ov.getresult()  # type: ignore[no-any-return]


async def wait_comm_event(handle: int) -> int:
    """Issue ``WaitCommEvent`` and await the overlapped completion.

    Returns the raw event mask (``DWORD``). Uses a manual-reset Win32
    event + ``proactor.wait_for_handle`` because ``_overlapped.Overlapped``
    has no ``WaitCommEvent`` method (design-windows-backend.md §6.4).

    The sequence:

    1. ``CreateEventW`` — manual-reset event, initially non-signalled.
    2. Build a ctypes ``OVERLAPPED`` whose ``hEvent`` is that event with
       the low-order bit set, so the kernel signals the event but queues
       no packet to the proactor's completion port, which has no entry
       for this ``OVERLAPPED``.
    3. ``WaitCommEvent(handle, &mask, &ov)`` — issues the overlapped op.
    4. ``proactor.wait_for_handle(event_handle)`` — parks until the kernel
       signals the event on completion.
    5. Read ``mask.value``, close the event handle.

    Cancellation: the proactor owns only the wait on the event, not the
    ``WaitCommEvent`` itself, so this function cancels it with
    ``CancelIoEx`` and waits (up to ``_CANCEL_WAIT_MS``) for the kernel to
    finish with ``ov`` and ``mask`` before they are released. If the
    kernel does not finish in time they are kept alive in
    ``_abandoned_waits`` instead of being freed under it.
    """
    import ctypes  # noqa: PLC0415

    from anyserial._windows import _win32 as w  # noqa: PLC0415
    from anyserial._windows._win32 import OVERLAPPED  # noqa: PLC0415

    kernel32 = w.load_kernel32()
    proactor = _running_proactor()

    # Manual-reset event, initially non-signalled.
    event_handle = kernel32.CreateEventW(None, 1, 0, None)
    if not event_handle:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    ov = OVERLAPPED()
    ov.hEvent = event_handle | 1
    mask = ctypes.c_uint32(0)
    released = True
    try:
        result = kernel32.WaitCommEvent(handle, ctypes.byref(mask), ctypes.byref(ov))
        if not result:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err != w.ERROR_IO_PENDING:
                raise ctypes.WinError(err)  # type: ignore[attr-defined]

            # Park until the kernel signals the event handle.
            try:
                await proactor.wait_for_handle(event_handle)
            except BaseException:
                # Cancelled (or failed) while the kernel still owns ``ov``
                # and ``mask``: cancel the operation and wait for it to end.
                kernel32.CancelIoEx(handle, ctypes.byref(ov))
                released = (
                    kernel32.WaitForSingleObject(event_handle, _CANCEL_WAIT_MS) == w.WAIT_OBJECT_0
                )
                raise
    finally:
        if released:
            with contextlib.suppress(OSError):
                kernel32.CloseHandle(event_handle)
        else:
            _abandoned_waits.append((ov, mask, event_handle))

    return int(mask.value)


# How long a cancelled wait_comm_event blocks for the kernel to finish the
# cancelled WaitCommEvent. Serial drivers complete it immediately.
_CANCEL_WAIT_MS = 1000

# Buffers and event handles of cancelled WaitCommEvent calls the kernel had
# not finished within _CANCEL_WAIT_MS. Kept for the life of the process so
# the kernel never writes into freed memory.
_abandoned_waits: list[tuple[object, object, int]] = []


__all__ = ["HandleWrapper", "readinto", "register", "wait_comm_event", "write"]
