"""Exception hierarchy and errno mapping for ``anyserial``.

Every exception class multi-inherits from the most natural standard-library
or AnyIO base so callers that already catch ``OSError``, ``ValueError``,
``FileNotFoundError``, ``NotImplementedError``, ``anyio.ClosedResourceError``,
or ``anyio.BrokenResourceError`` pick up our exceptions without new ``except``
clauses.

The ``errno_to_exception`` helper turns an ``OSError`` caught around a serial
syscall into the right domain exception, preserving ``__cause__``, ``errno``,
``filename``, and ``strerror``.
"""

from __future__ import annotations

import errno as _errno

import anyio


class SerialError(OSError):
    """Base class for all serial-port failures raised by ``anyserial``."""


class ConfigurationError(SerialError, ValueError):
    """The supplied :class:`SerialConfig` is internally invalid.

    Raised for bad baud rates, impossible flow-control combinations, or other
    violations detectable without touching the OS.
    """


class PortNotFoundError(SerialError, FileNotFoundError):
    """The requested port does not exist or is not reachable."""


class PortBusyError(SerialError):
    """The port is already in use or locked exclusively by another process."""


class UnsupportedFeatureError(SerialError, NotImplementedError):
    """A requested feature is unsupported by the backend, driver, or device."""


class UnsupportedConfigurationError(SerialError, ValueError):
    """A configuration the platform advertises was rejected at runtime.

    Example: Linux reports ``custom_baudrate = SUPPORTED`` generically, but a
    specific USB adapter returns ``EINVAL`` when the rate is applied.
    """


class UnsupportedPlatformError(SerialError, NotImplementedError):
    """No backend is available for the current platform."""


class SerialClosedError(SerialError, anyio.ClosedResourceError):
    """Operation attempted on a port that has already been closed."""


class SerialDisconnectedError(SerialError, anyio.BrokenResourceError):
    """The device was removed or became unusable during I/O."""


class UnsupportedAsyncBackendError(SerialError, RuntimeError):
    """The active AnyIO backend is unsupported by this operation."""


_OPEN_NOT_FOUND: frozenset[int] = frozenset(
    {_errno.ENOENT, _errno.ENODEV, _errno.ENXIO},
)
_BUSY: frozenset[int] = frozenset({_errno.EBUSY, _errno.EACCES})
_DISCONNECT: frozenset[int] = frozenset({_errno.EIO, _errno.ENXIO})

_CONTEXT_NOT_FOUND = "open"
_CONTEXT_BUSY = "lock"
_CONTEXT_IOCTL = "ioctl"
_CONTEXT_IO = "io"


def errno_to_exception(
    exc: OSError,
    *,
    context: str,
    path: str | None = None,
) -> SerialError:
    """Translate an ``OSError`` from a serial syscall into a domain exception.

    Args:
        exc: The caught ``OSError``. Its ``errno``, ``strerror``, and
            ``filename`` are carried onto the returned exception, and it is
            set as the ``__cause__`` of the new exception.
        context: Short tag describing what was being attempted. One of
            ``"open"``, ``"lock"``, ``"ioctl"``, or ``"io"``. Callers pass
            the tag that matches the syscall site so the mapping can be
            unambiguous.
        path: Optional device path for the ``filename`` attribute when the
            caught ``OSError`` does not carry one.

    Returns:
        A :class:`SerialError` subclass. Never returns ``None``; unknown
        errno values are wrapped in :class:`SerialError` itself.
    """
    # Pre-wrapped domain exceptions pass through unchanged. Backends may
    # raise UnsupportedFeatureError directly during open() (e.g. when
    # config.low_latency=True with policy=RAISE on a driver that lacks
    # TIOCSSERIAL); those should reach the caller as-is rather than be
    # demoted to a generic SerialError.
    if isinstance(exc, SerialError):
        return exc

    err = exc.errno
    filename = exc.filename or path

    cls: type[SerialError]
    if context == _CONTEXT_NOT_FOUND and err in _OPEN_NOT_FOUND:
        cls = PortNotFoundError
    elif context == _CONTEXT_BUSY or err in _BUSY:
        cls = PortBusyError
    elif context == _CONTEXT_IOCTL and err in {_errno.EINVAL, _errno.ENOTTY}:
        cls = UnsupportedFeatureError
    elif context == _CONTEXT_IO and err in _DISCONNECT:
        cls = SerialDisconnectedError
    else:
        cls = SerialError

    new = cls(err, exc.strerror, filename)
    new.__cause__ = exc
    return new


__all__ = [
    "ConfigurationError",
    "PortBusyError",
    "PortNotFoundError",
    "SerialClosedError",
    "SerialDisconnectedError",
    "SerialError",
    "UnsupportedAsyncBackendError",
    "UnsupportedConfigurationError",
    "UnsupportedFeatureError",
    "UnsupportedPlatformError",
    "errno_to_exception",
]
# UnsupportedPlatformError is deliberately included above; it is not in
# DESIGN §10's original list but is re-exported from the top-level package.
