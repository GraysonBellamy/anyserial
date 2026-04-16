"""Trio overlapped-I/O hot path for the Windows backend.

design-windows-backend.md §4 — uses Trio's batteries-included
``readinto_overlapped`` / ``write_overlapped`` so Trio owns the OVERLAPPED
lifecycle, ``CancelIoEx``, and the post-cancellation completion wait.

``wait_comm_event`` (§6.4) uses ``trio.lowlevel.wait_overlapped``
with a ctypes ``OVERLAPPED`` + ``DWORD`` mask because Trio has no
dedicated ``WaitCommEvent`` helper. The ctypes structures are caller-owned
and kept alive across the ``await``.

Imports are lazy at the function level so this module is harmless to
load on POSIX (where Trio is an optional dep) and so the asyncio path's
import graph never pulls in Trio.

Minimum Trio version: 0.22.
"""

from __future__ import annotations

from ctypes import byref, c_uint32
from typing import Any

from anyserial._windows._win32 import OVERLAPPED


async def register(handle: int) -> None:
    """Associate ``handle`` with Trio's IOCP. Idempotent per Trio's docs."""
    import trio  # noqa: PLC0415 — lazy by runtime

    register_fn: Any = trio.lowlevel.register_with_iocp  # type: ignore[attr-defined]
    register_fn(handle)


async def readinto(handle: int, buffer: bytearray | memoryview) -> int:
    """Zero-copy overlapped read into the caller's buffer.

    Returns the number of bytes written into ``buffer``. Cancellation is
    automatic: if the awaiting task is cancelled, Trio issues
    ``CancelIoEx`` and waits for the actual completion before raising,
    so the buffer is safe to release.
    """
    import trio  # noqa: PLC0415 — lazy by runtime

    readinto: Any = trio.lowlevel.readinto_overlapped  # type: ignore[attr-defined]
    return int(await readinto(handle, buffer))  # pyright: ignore[reportUnknownArgumentType]


async def write(handle: int, data: bytes | memoryview) -> int:
    """Overlapped write from ``data``; returns bytes accepted by the kernel."""
    import trio  # noqa: PLC0415 — lazy by runtime

    # ``trio.lowlevel.write_overlapped`` is documented but mypy's vendored
    # stubs don't expose it on every version pin.
    write_overlapped: Any = trio.lowlevel.write_overlapped  # type: ignore[attr-defined]
    return int(await write_overlapped(handle, data))  # pyright: ignore[reportUnknownArgumentType]


async def wait_comm_event(handle: int) -> int:
    """Issue ``WaitCommEvent`` and await the overlapped completion.

    Returns the raw event mask (``DWORD``) so the caller can interpret the
    bits. Cancellation is automatic: if the awaiting task is cancelled,
    Trio issues ``CancelIoEx`` and waits for the actual completion before
    raising, so the ctypes buffers are safe to release.

    Uses ``trio.lowlevel.wait_overlapped`` because Trio has no dedicated
    ``WaitCommEvent`` helper. We allocate a ctypes ``OVERLAPPED`` and
    ``DWORD`` mask on the stack and pass them to the kernel; Trio drives
    the completion wait via its IOCP integration.
    """
    import trio  # noqa: PLC0415 — lazy by runtime

    from anyserial._windows import _win32 as w  # noqa: PLC0415

    kernel32 = w.load_kernel32()
    ov = OVERLAPPED()
    mask = c_uint32(0)

    # WaitCommEvent returns FALSE + ERROR_IO_PENDING on overlapped success.
    # A TRUE return means the event completed synchronously (rare but valid).
    result = kernel32.WaitCommEvent(handle, byref(mask), byref(ov))
    if not result:
        import ctypes  # noqa: PLC0415

        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        if err != w.ERROR_IO_PENDING:
            raise ctypes.WinError(err)  # type: ignore[attr-defined]

        # Pend on the IOCP completion packet.
        wait_overlapped: Any = trio.lowlevel.wait_overlapped  # type: ignore[attr-defined]
        await wait_overlapped(handle, ov)

    return int(mask.value)


__all__ = ["readinto", "register", "wait_comm_event", "write"]
