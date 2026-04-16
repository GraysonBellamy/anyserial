# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# Reason: ``ctypes.WinDLL`` and its dynamically-resolved function-pointer
# attributes are intrinsically untyped. Every binding in ``_bind_kernel32``
# does ``kernel32.SomeApi`` → attribute lookup on an untyped WinDLL, and
# pyright can't narrow those beyond ``Unknown | Any``. The ``Kernel32Bindings``
# class exposes the final bindings as ``Any``, which is what callers see —
# the ``Unknown`` leak is confined to this module's internals.
"""Win32 ctypes bindings for the Windows serial backend.

Layout matches the Microsoft headers; field offsets / sizes are pinned by
unit tests in :mod:`tests.unit.test_windows_win32`. Structures and constants
are pure-Python and import on every platform — only the function pointers in
:func:`load_kernel32` actually touch ``windll``, and the loader raises a
clean :class:`UnsupportedPlatformError` when called off-Windows. That split
lets the DCB-translation and capability tests run on Linux CI while the
actual syscalls remain Windows-only.

Why we write the bindings fresh instead of pulling pyserial's
``serial/win32.py``: scope (only the calls we use), correctness
(``use_last_error=True`` and proper ``errcheck`` hooks throughout), and
no dependency on a package whose async story we can't keep up with.

References:
- design-windows-backend.md §6 (Win32 surface).
- MSDN: DCB, COMMTIMEOUTS, COMSTAT, OVERLAPPED, CreateFileW, etc.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    c_char,
    c_uint8,
    c_uint16,
    c_uint32,
    c_void_p,
)
from typing import TYPE_CHECKING, Any

from anyserial.exceptions import UnsupportedPlatformError

if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CreateFileW
GENERIC_READ: int = 0x80000000
GENERIC_WRITE: int = 0x40000000
OPEN_EXISTING: int = 3
FILE_FLAG_OVERLAPPED: int = 0x40000000
INVALID_HANDLE_VALUE: int = -1

# DCB.Parity
NOPARITY: int = 0
ODDPARITY: int = 1
EVENPARITY: int = 2
MARKPARITY: int = 3
SPACEPARITY: int = 4

# DCB.StopBits
ONESTOPBIT: int = 0
ONE5STOPBITS: int = 1
TWOSTOPBITS: int = 2

# DCB.fRtsControl / fDtrControl bitfield values
RTS_CONTROL_DISABLE: int = 0
RTS_CONTROL_ENABLE: int = 1
RTS_CONTROL_HANDSHAKE: int = 2
RTS_CONTROL_TOGGLE: int = 3

DTR_CONTROL_DISABLE: int = 0
DTR_CONTROL_ENABLE: int = 1
DTR_CONTROL_HANDSHAKE: int = 2

# Default XON / XOFF characters per the historical PC convention.
XON_CHAR: int = 0x11
XOFF_CHAR: int = 0x13

# EscapeCommFunction codes
SETXOFF: int = 1
SETXON: int = 2
SETRTS: int = 3
CLRRTS: int = 4
SETDTR: int = 5
CLRDTR: int = 6
SETBREAK: int = 8
CLRBREAK: int = 9

# PurgeComm flags
PURGE_TXABORT: int = 0x0001
PURGE_RXABORT: int = 0x0002
PURGE_TXCLEAR: int = 0x0004
PURGE_RXCLEAR: int = 0x0008

# GetCommModemStatus bits
MS_CTS_ON: int = 0x0010
MS_DSR_ON: int = 0x0020
MS_RING_ON: int = 0x0040
MS_RLSD_ON: int = 0x0080  # carrier detect

# Default queue sizing for SetupComm — design-windows-backend.md §6.1.
DEFAULT_INPUT_QUEUE: int = 4096
DEFAULT_OUTPUT_QUEUE: int = 4096

# Used by the "wait-for-any" COMMTIMEOUTS policy — design-windows-backend.md §6.3.
MAXDWORD: int = 0xFFFFFFFF

# SetCommMask / WaitCommEvent event flags — design-windows-backend.md §6.4.
# EV_RXCHAR is deliberately excluded: we do not use comm events for
# data-path readiness.
EV_CTS: int = 0x0008
EV_DSR: int = 0x0010
EV_RLSD: int = 0x0020  # carrier detect change
EV_BREAK: int = 0x0040
EV_ERR: int = 0x0080  # framing / overrun / parity error
EV_RING: int = 0x0100

# Combined mask for SetCommMask — all modem-line and error events.
EV_ALL_MODEM: int = EV_CTS | EV_DSR | EV_RLSD | EV_BREAK | EV_ERR | EV_RING

# Win32 error codes referenced by the translator (design-windows-backend.md §9).
ERROR_FILE_NOT_FOUND: int = 2
ERROR_ACCESS_DENIED: int = 5
ERROR_INVALID_HANDLE: int = 6
ERROR_NOT_READY: int = 21
ERROR_SHARING_VIOLATION: int = 32
ERROR_GEN_FAILURE: int = 31
ERROR_INVALID_PARAMETER: int = 87
ERROR_IO_PENDING: int = 997
ERROR_OPERATION_ABORTED: int = 995
ERROR_DEVICE_REMOVED: int = 1617


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------


class DCB(Structure):
    _pack_ = 1
    _fields_ = (
        ("DCBlength", c_uint32),
        ("BaudRate", c_uint32),
        # 32 packed bits — order matters per Microsoft's DCB layout.
        ("fBinary", c_uint32, 1),
        ("fParity", c_uint32, 1),
        ("fOutxCtsFlow", c_uint32, 1),
        ("fOutxDsrFlow", c_uint32, 1),
        ("fDtrControl", c_uint32, 2),
        ("fDsrSensitivity", c_uint32, 1),
        ("fTXContinueOnXoff", c_uint32, 1),
        ("fOutX", c_uint32, 1),
        ("fInX", c_uint32, 1),
        ("fErrorChar", c_uint32, 1),
        ("fNull", c_uint32, 1),
        ("fRtsControl", c_uint32, 2),
        ("fAbortOnError", c_uint32, 1),
        ("fDummy2", c_uint32, 17),
        ("wReserved", c_uint16),
        ("XonLim", c_uint16),
        ("XoffLim", c_uint16),
        ("ByteSize", c_uint8),
        ("Parity", c_uint8),
        ("StopBits", c_uint8),
        ("XonChar", c_char),
        ("XoffChar", c_char),
        ("ErrorChar", c_char),
        ("EofChar", c_char),
        ("EvtChar", c_char),
        ("wReserved1", c_uint16),
    )


class COMMTIMEOUTS(Structure):
    _fields_ = (
        ("ReadIntervalTimeout", c_uint32),
        ("ReadTotalTimeoutMultiplier", c_uint32),
        ("ReadTotalTimeoutConstant", c_uint32),
        ("WriteTotalTimeoutMultiplier", c_uint32),
        ("WriteTotalTimeoutConstant", c_uint32),
    )


class COMSTAT(Structure):
    _fields_ = (
        ("fCtsHold", c_uint32, 1),
        ("fDsrHold", c_uint32, 1),
        ("fRlsdHold", c_uint32, 1),
        ("fXoffHold", c_uint32, 1),
        ("fXoffSent", c_uint32, 1),
        ("fEof", c_uint32, 1),
        ("fTxim", c_uint32, 1),
        ("fReserved", c_uint32, 25),
        ("cbInQue", c_uint32),
        ("cbOutQue", c_uint32),
    )


class OVERLAPPED(Structure):
    """OVERLAPPED record passed to ``ReadFile`` / ``WriteFile``.

    Used by the ``WaitCommEvent`` path. The hot-path I/O lets the runtime
    own its own ``OVERLAPPED`` structures (Trio: ``readinto_overlapped``;
    asyncio: ``_overlapped``).
    """

    _fields_ = (
        ("Internal", c_void_p),
        ("InternalHigh", c_void_p),
        ("Offset", c_uint32),
        ("OffsetHigh", c_uint32),
        ("hEvent", c_void_p),
    )


# ---------------------------------------------------------------------------
# Function-pointer loader
# ---------------------------------------------------------------------------


class Kernel32Bindings:
    """Resolved kernel32 function pointers — populated by :func:`load_kernel32`.

    Each attribute corresponds to the Win32 function of the same name with
    ``argtypes`` and ``restype`` configured. The loader installs ``errcheck``
    hooks for the ``BOOL``-returning calls so a return value of zero raises
    ``OSError`` carrying the ``GetLastError`` code.

    Attributes are declared as :data:`typing.Any` because ctypes function
    pointers don't have a static type — the runtime guarantee is that they
    behave like callables matching the Win32 signature.
    """

    __slots__ = (
        "ClearCommBreak",
        "ClearCommError",
        "CloseHandle",
        "CreateEventW",
        "CreateFileW",
        "EscapeCommFunction",
        "FlushFileBuffers",
        "GetCommModemStatus",
        "GetCommState",
        "PurgeComm",
        "ResetEvent",
        "SetCommBreak",
        "SetCommMask",
        "SetCommState",
        "SetCommTimeouts",
        "SetupComm",
        "WaitCommEvent",
    )

    # Type stubs so static checkers see the attribute names that
    # ``_bind_kernel32`` populates at runtime.
    ClearCommBreak: Any
    ClearCommError: Any
    CloseHandle: Any
    CreateEventW: Any
    CreateFileW: Any
    EscapeCommFunction: Any
    FlushFileBuffers: Any
    GetCommModemStatus: Any
    GetCommState: Any
    PurgeComm: Any
    ResetEvent: Any
    SetCommBreak: Any
    SetCommMask: Any
    SetCommState: Any
    SetCommTimeouts: Any
    SetupComm: Any
    WaitCommEvent: Any


_kernel32_cache: Kernel32Bindings | None = None


def load_kernel32() -> Kernel32Bindings:
    """Return the lazily-initialised kernel32 binding table.

    Called from :class:`anyserial._windows.backend.WindowsBackend` on first
    use. Raises :class:`UnsupportedPlatformError` if invoked on a non-
    Windows host so the import-time graph stays clean for cross-platform
    static checks; runtime callers shouldn't reach this path because
    :func:`anyserial._backend.selector.select_backend` only constructs
    ``WindowsBackend`` on ``win32``.
    """
    global _kernel32_cache  # noqa: PLW0603 — module-level cache by design
    if _kernel32_cache is not None:
        return _kernel32_cache
    # Read into a local so mypy / pyright don't narrow the comparison and
    # flag everything below as unreachable on non-Windows type-check hosts.
    # The ``hasattr`` check also covers ``sys.platform`` monkeypatching in
    # dispatch tests on Linux CI.
    platform = sys.platform
    if platform != "win32" or not hasattr(ctypes, "WinDLL"):
        msg = (
            "anyserial._windows._win32.load_kernel32 called on "
            f"{platform!r}; kernel32 bindings are Windows-only"
        )
        raise UnsupportedPlatformError(msg)
    _kernel32_cache = _bind_kernel32()
    return _kernel32_cache


def _bind_kernel32() -> Kernel32Bindings:  # noqa: PLR0915 — one binding per Win32 call
    # Imported lazily so static checkers on POSIX never see ``windll``.
    # Cast to ``Any`` at the entry point: ctypes function pointers are
    # untyped attribute lookups on the WinDLL instance, and threading
    # ``# type: ignore`` through every ``kernel32.Foo`` line would be far
    # noisier than acknowledging the whole subsystem is dynamic.
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    def _check_bool(result: int, func: Callable[..., Any], args: tuple[Any, ...]) -> int:
        if not result:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise ctypes.WinError(err)  # type: ignore[attr-defined]
        return result

    def _check_handle(result: int, func: Callable[..., Any], args: tuple[Any, ...]) -> int:
        # CreateFileW returns INVALID_HANDLE_VALUE (-1) on failure.
        if result == ctypes.c_void_p(INVALID_HANDLE_VALUE).value:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise ctypes.WinError(err)  # type: ignore[attr-defined]
        return result

    bindings = Kernel32Bindings()

    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,  # lpFileName
        c_uint32,  # dwDesiredAccess
        c_uint32,  # dwShareMode
        c_void_p,  # lpSecurityAttributes
        c_uint32,  # dwCreationDisposition
        c_uint32,  # dwFlagsAndAttributes
        c_void_p,  # hTemplateFile
    ]
    create.restype = c_void_p
    create.errcheck = _check_handle
    bindings.CreateFileW = create

    close = kernel32.CloseHandle
    close.argtypes = [c_void_p]
    close.restype = c_uint32
    close.errcheck = _check_bool
    bindings.CloseHandle = close

    get_state = kernel32.GetCommState
    get_state.argtypes = [c_void_p, POINTER(DCB)]
    get_state.restype = c_uint32
    get_state.errcheck = _check_bool
    bindings.GetCommState = get_state

    set_state = kernel32.SetCommState
    set_state.argtypes = [c_void_p, POINTER(DCB)]
    set_state.restype = c_uint32
    set_state.errcheck = _check_bool
    bindings.SetCommState = set_state

    set_timeouts = kernel32.SetCommTimeouts
    set_timeouts.argtypes = [c_void_p, POINTER(COMMTIMEOUTS)]
    set_timeouts.restype = c_uint32
    set_timeouts.errcheck = _check_bool
    bindings.SetCommTimeouts = set_timeouts

    setup = kernel32.SetupComm
    setup.argtypes = [c_void_p, c_uint32, c_uint32]
    setup.restype = c_uint32
    setup.errcheck = _check_bool
    bindings.SetupComm = setup

    purge = kernel32.PurgeComm
    purge.argtypes = [c_void_p, c_uint32]
    purge.restype = c_uint32
    purge.errcheck = _check_bool
    bindings.PurgeComm = purge

    escape = kernel32.EscapeCommFunction
    escape.argtypes = [c_void_p, c_uint32]
    escape.restype = c_uint32
    escape.errcheck = _check_bool
    bindings.EscapeCommFunction = escape

    modem = kernel32.GetCommModemStatus
    modem.argtypes = [c_void_p, POINTER(c_uint32)]
    modem.restype = c_uint32
    modem.errcheck = _check_bool
    bindings.GetCommModemStatus = modem

    clear_err = kernel32.ClearCommError
    clear_err.argtypes = [c_void_p, POINTER(c_uint32), POINTER(COMSTAT)]
    clear_err.restype = c_uint32
    clear_err.errcheck = _check_bool
    bindings.ClearCommError = clear_err

    set_brk = kernel32.SetCommBreak
    set_brk.argtypes = [c_void_p]
    set_brk.restype = c_uint32
    set_brk.errcheck = _check_bool
    bindings.SetCommBreak = set_brk

    clr_brk = kernel32.ClearCommBreak
    clr_brk.argtypes = [c_void_p]
    clr_brk.restype = c_uint32
    clr_brk.errcheck = _check_bool
    bindings.ClearCommBreak = clr_brk

    flush = kernel32.FlushFileBuffers
    flush.argtypes = [c_void_p]
    flush.restype = c_uint32
    flush.errcheck = _check_bool
    bindings.FlushFileBuffers = flush

    set_mask = kernel32.SetCommMask
    set_mask.argtypes = [c_void_p, c_uint32]
    set_mask.restype = c_uint32
    set_mask.errcheck = _check_bool
    bindings.SetCommMask = set_mask

    # WaitCommEvent returns FALSE + ERROR_IO_PENDING on success with
    # overlapped I/O, so it must NOT use _check_bool. Callers check
    # GetLastError themselves.
    wait_event = kernel32.WaitCommEvent
    wait_event.argtypes = [c_void_p, POINTER(c_uint32), POINTER(OVERLAPPED)]
    wait_event.restype = c_uint32
    bindings.WaitCommEvent = wait_event

    # CreateEventW — manual-reset event for the asyncio WaitCommEvent path
    # (design-windows-backend.md §6.4). Returns a HANDLE; NULL on failure.
    create_event = kernel32.CreateEventW
    create_event.argtypes = [
        c_void_p,  # lpEventAttributes
        c_uint32,  # bManualReset
        c_uint32,  # bInitialState
        c_void_p,  # lpName
    ]
    create_event.restype = c_void_p
    create_event.errcheck = _check_handle
    bindings.CreateEventW = create_event

    reset_event = kernel32.ResetEvent
    reset_event.argtypes = [c_void_p]
    reset_event.restype = c_uint32
    reset_event.errcheck = _check_bool
    bindings.ResetEvent = reset_event

    return bindings


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


def normalise_com_path(path: str) -> str:
    r"""Return the ``\\.\COMn`` form expected by ``CreateFileW``.

    Required for COM10 and above (the legacy DOS ``COMn`` namespace stops
    at COM9); harmless for COM1-COM9. Paths that already start with the
    DOS-device prefix are returned unchanged so callers can pass either
    style.
    """
    if path.startswith("\\\\.\\"):
        return path
    return "\\\\.\\" + path


__all__ = [
    "CLRBREAK",
    "CLRDTR",
    "CLRRTS",
    "COMMTIMEOUTS",
    "COMSTAT",
    "DCB",
    "DEFAULT_INPUT_QUEUE",
    "DEFAULT_OUTPUT_QUEUE",
    "DTR_CONTROL_DISABLE",
    "DTR_CONTROL_ENABLE",
    "DTR_CONTROL_HANDSHAKE",
    "ERROR_ACCESS_DENIED",
    "ERROR_DEVICE_REMOVED",
    "ERROR_FILE_NOT_FOUND",
    "ERROR_GEN_FAILURE",
    "ERROR_INVALID_HANDLE",
    "ERROR_INVALID_PARAMETER",
    "ERROR_IO_PENDING",
    "ERROR_NOT_READY",
    "ERROR_OPERATION_ABORTED",
    "ERROR_SHARING_VIOLATION",
    "EVENPARITY",
    "EV_ALL_MODEM",
    "EV_BREAK",
    "EV_CTS",
    "EV_DSR",
    "EV_ERR",
    "EV_RING",
    "EV_RLSD",
    "FILE_FLAG_OVERLAPPED",
    "GENERIC_READ",
    "GENERIC_WRITE",
    "INVALID_HANDLE_VALUE",
    "MARKPARITY",
    "MAXDWORD",
    "MS_CTS_ON",
    "MS_DSR_ON",
    "MS_RING_ON",
    "MS_RLSD_ON",
    "NOPARITY",
    "ODDPARITY",
    "ONE5STOPBITS",
    "ONESTOPBIT",
    "OPEN_EXISTING",
    "OVERLAPPED",
    "PURGE_RXABORT",
    "PURGE_RXCLEAR",
    "PURGE_TXABORT",
    "PURGE_TXCLEAR",
    "RTS_CONTROL_DISABLE",
    "RTS_CONTROL_ENABLE",
    "RTS_CONTROL_HANDSHAKE",
    "RTS_CONTROL_TOGGLE",
    "SETBREAK",
    "SETDTR",
    "SETRTS",
    "SETXOFF",
    "SETXON",
    "SPACEPARITY",
    "TWOSTOPBITS",
    "XOFF_CHAR",
    "XON_CHAR",
    "Kernel32Bindings",
    "load_kernel32",
    "normalise_com_path",
]
