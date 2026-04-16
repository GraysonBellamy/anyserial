"""Async runtime detection for the Windows backend.

design-windows-backend.md §2 specifies runtime-native probes — no sniffio
re-add. Detection is one-shot, called from
:meth:`anyserial._windows.backend.WindowsBackend.open` so the hot path
never pays for it.

Two outcomes matter:

- ``"asyncio"`` with a Proactor event loop.
- ``"trio"``.

asyncio without a Proactor (the user explicitly forced
``WindowsSelectorEventLoopPolicy``) raises
:class:`UnsupportedPlatformError` with a message pointing at
``WindowsProactorEventLoopPolicy``. There is no worker-thread fallback by
design.
"""

from __future__ import annotations

from typing import Literal

from anyserial.exceptions import UnsupportedAsyncBackendError, UnsupportedPlatformError

RuntimeFlavour = Literal["asyncio", "trio"]


def detect_runtime() -> RuntimeFlavour:
    """Return the active async runtime name.

    Probes asyncio first (it's the common case on Windows) and falls back
    to Trio. Either probe re-raising means we couldn't find a running
    loop, which raises :class:`UnsupportedAsyncBackendError`.
    """
    asyncio_loop = _running_asyncio_loop()
    if asyncio_loop is not None:
        _require_proactor(asyncio_loop)
        return "asyncio"
    if _trio_is_running():
        return "trio"
    msg = (
        "anyserial Windows backend requires a running asyncio or trio event "
        "loop; none detected. Open the port from inside an async task."
    )
    raise UnsupportedAsyncBackendError(msg)


def _running_asyncio_loop() -> object | None:
    """Return the running asyncio loop, or ``None`` if asyncio isn't active."""
    try:
        import asyncio  # noqa: PLC0415 — local to keep imports lazy
    except ImportError:
        return None
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _trio_is_running() -> bool:
    """Return ``True`` if a Trio task is currently executing."""
    try:
        import trio  # noqa: PLC0415 — optional dep on POSIX, ensured on Windows
    except ImportError:
        return False
    try:
        trio.lowlevel.current_task()
    except RuntimeError:
        return False
    return True


def _require_proactor(loop: object) -> None:
    """Verify the asyncio loop is a Proactor — raise with a clean message otherwise.

    Per design-windows-backend.md §2, the only supported asyncio loop on
    Windows is ``ProactorEventLoop`` (the default since Python 3.8). The
    ``_proactor`` attribute is the runtime tell.
    """
    proactor = getattr(loop, "_proactor", None)
    if proactor is None:
        msg = (
            "anyserial requires asyncio.ProactorEventLoop on Windows. This is "
            "the default since Python 3.8. If you have overridden the event "
            "loop policy, switch back to WindowsProactorEventLoopPolicy: "
            "asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())."
        )
        raise UnsupportedPlatformError(msg)


__all__ = [
    "RuntimeFlavour",
    "detect_runtime",
]
