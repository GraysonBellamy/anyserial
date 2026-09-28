"""Trio overlapped-I/O hot path for the Windows backend.

design-windows-backend.md §4 — uses Trio's batteries-included
``readinto_overlapped`` / ``write_overlapped`` so Trio owns the OVERLAPPED
lifecycle, ``CancelIoEx``, and the post-cancellation completion wait.

``wait_comm_event`` (§6.4) uses ``trio.lowlevel.wait_overlapped``
with a ctypes ``OVERLAPPED`` + ``DWORD`` mask because Trio has no
dedicated ``WaitCommEvent`` helper. The ctypes structures are caller-owned
and kept alive across the ``await``; Trio receives the ``OVERLAPPED`` by
address.

Trio treats every non-zero completion status as a failure, including the
success-class ``STATUS_TIMEOUT`` that ends an idle read under the
"wait-for-any" ``COMMTIMEOUTS`` policy (§6.3). :func:`readinto` maps that
status back to the empty completion the backend's read loop expects.

Imports are lazy at the function level so this module is harmless to
load on POSIX (where Trio is an optional dep) and so the asyncio path's
import graph never pulls in Trio.

Minimum Trio version: 0.22.
"""

from __future__ import annotations

from ctypes import addressof, byref, c_uint32
from typing import Any

from anyserial._windows._win32 import ERROR_TIMEOUT, OVERLAPPED


async def register(handle: int) -> None:
    """Associate ``handle`` with Trio's IOCP. Idempotent per Trio's docs."""
    import trio  # noqa: PLC0415 — lazy by runtime

    register_fn: Any = trio.lowlevel.register_with_iocp  # type: ignore[attr-defined]
    register_fn(handle)


async def readinto(handle: int, buffer: bytearray | memoryview) -> int:
    """Zero-copy overlapped read into the caller's buffer.

    Returns the number of bytes written into ``buffer``; ``0`` when the
    read timed out with no data, which the caller reissues. Cancellation
    is automatic: if the awaiting task is cancelled, Trio issues
    ``CancelIoEx`` and waits for the actual completion before raising,
    so the buffer is safe to release.
    """
    import trio  # noqa: PLC0415 — lazy by runtime

    readinto: Any = trio.lowlevel.readinto_overlapped  # type: ignore[attr-defined]
    try:
        return int(await readinto(handle, buffer))  # pyright: ignore[reportUnknownArgumentType]
    except OSError as exc:
        # Trio raises ``STATUS_TIMEOUT`` as ERROR_TIMEOUT. Under the
        # wait-for-any policy a read only times out when no byte arrived,
        # so the completion transferred nothing.
        if getattr(exc, "winerror", None) == ERROR_TIMEOUT:
            return 0
        raise


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
    ``DWORD`` mask, pass them to the kernel, and hand Trio the
    ``OVERLAPPED``'s address: Trio keys its waiters by that address (a
    ctypes structure is unhashable) and drives the completion wait via its
    IOCP integration.
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

    # The handle is associated with Trio's completion port without
    # FILE_SKIP_COMPLETION_PORT_ON_SUCCESS, so the kernel queues a
    # completion packet even when WaitCommEvent finished synchronously.
    # Wait for it in both cases; ``ov`` and ``mask`` stay alive until then.
    wait_overlapped: Any = trio.lowlevel.wait_overlapped  # type: ignore[attr-defined]
    await wait_overlapped(handle, addressof(ov))

    return int(mask.value)


__all__ = ["readinto", "register", "wait_comm_event", "write"]
