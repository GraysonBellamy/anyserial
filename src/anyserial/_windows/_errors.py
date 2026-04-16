"""Win32 ``OSError`` → :class:`SerialError` translation.

Mirrors the POSIX :func:`anyserial.exceptions.errno_to_exception` helper
but keys off ``OSError.winerror`` rather than ``errno``. Covers the full
design-windows-backend.md §9 matrix.

``ctypes.WinError(...)`` raises an ``OSError`` whose ``winerror`` field is
the Win32 code; this helper takes that ``OSError`` and returns the right
:class:`anyserial` domain exception.
"""

from __future__ import annotations

from anyserial._windows import _win32 as w
from anyserial.exceptions import (
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialDisconnectedError,
    SerialError,
    UnsupportedConfigurationError,
)

_OPEN_CONTEXT = "open"
_IO_CONTEXT = "io"
_IOCTL_CONTEXT = "ioctl"


def winerror_to_exception(
    exc: OSError,
    *,
    context: str,
    path: str | None = None,
) -> SerialError:
    """Translate a Win32 ``OSError`` into a :class:`SerialError` subclass.

    ``context`` mirrors the POSIX helper: ``"open"``, ``"ioctl"``, or
    ``"io"`` so the same error code can map to different exception types
    depending on the call site. Pre-wrapped :class:`SerialError` instances
    pass through unchanged.
    """
    if isinstance(exc, SerialError):
        return exc

    # ``OSError.winerror`` exists only on Windows; mypy stubs on POSIX
    # don't declare it. Fall back through ``getattr`` so the helper stays
    # importable on every platform (the unit tests run on Linux).
    code = getattr(exc, "winerror", None) or exc.errno
    filename = exc.filename or path

    cls: type[SerialError]
    if code == w.ERROR_FILE_NOT_FOUND:
        cls = PortNotFoundError
    elif code in {w.ERROR_ACCESS_DENIED, w.ERROR_SHARING_VIOLATION}:
        cls = PortBusyError
    elif code in {w.ERROR_INVALID_HANDLE, w.ERROR_OPERATION_ABORTED}:
        # ERROR_INVALID_HANDLE: operation on a closed handle.
        # ERROR_OPERATION_ABORTED (§9): the overlapped op was cancelled
        # by PurgeComm or SetCommMask(handle, 0) during aclose(). Both
        # runtimes normally absorb this before it reaches user code; if
        # it leaks through (e.g. a race between aclose and a pending
        # WaitCommEvent), mapping to SerialClosedError gives callers the
        # same signal as other close-time conditions.
        cls = SerialClosedError
    elif code == w.ERROR_INVALID_PARAMETER and context == _IOCTL_CONTEXT:
        cls = UnsupportedConfigurationError
    elif code in {w.ERROR_DEVICE_REMOVED, w.ERROR_NOT_READY, w.ERROR_GEN_FAILURE}:
        cls = SerialDisconnectedError
    else:
        cls = SerialError

    new = cls(exc.errno or 0, exc.strerror, filename)
    # Stash the Win32 code so debug logging / tests can pick it up.
    new.winerror = code  # type: ignore[attr-defined]
    new.__cause__ = exc
    return new


__all__ = ["winerror_to_exception"]
