"""Tests for :mod:`anyserial.exceptions`.

Covers the multi-inheritance contract (existing ``except OSError`` /
``anyio.ClosedResourceError`` callers catch our exceptions automatically)
and the ``errno_to_exception`` mapping.
"""

from __future__ import annotations

import errno

import anyio
import pytest

from anyserial.exceptions import (
    ConfigurationError,
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialDisconnectedError,
    SerialError,
    UnsupportedConfigurationError,
    UnsupportedFeatureError,
    UnsupportedPlatformError,
    errno_to_exception,
)


class TestHierarchy:
    def test_serial_error_is_oserror(self) -> None:
        assert issubclass(SerialError, OSError)

    def test_configuration_error_is_valueerror(self) -> None:
        assert issubclass(ConfigurationError, ValueError)
        assert issubclass(ConfigurationError, SerialError)

    def test_port_not_found_is_filenotfounderror(self) -> None:
        assert issubclass(PortNotFoundError, FileNotFoundError)
        assert issubclass(PortNotFoundError, SerialError)

    def test_unsupported_feature_is_notimplementederror(self) -> None:
        assert issubclass(UnsupportedFeatureError, NotImplementedError)

    def test_unsupported_platform_is_notimplementederror(self) -> None:
        assert issubclass(UnsupportedPlatformError, NotImplementedError)
        assert issubclass(UnsupportedPlatformError, SerialError)

    def test_unsupported_configuration_is_valueerror(self) -> None:
        assert issubclass(UnsupportedConfigurationError, ValueError)

    def test_serial_closed_is_anyio_closed(self) -> None:
        assert issubclass(SerialClosedError, anyio.ClosedResourceError)

    def test_serial_disconnected_is_anyio_broken(self) -> None:
        assert issubclass(SerialDisconnectedError, anyio.BrokenResourceError)


class TestExistingCatchesStillWork:
    """An ``except`` on the stdlib / AnyIO base catches our subclasses."""

    def test_except_oserror_catches_serialerror(self) -> None:
        with pytest.raises(OSError):
            raise SerialError(errno.EIO, "boom")

    def test_except_filenotfounderror_catches_portnotfound(self) -> None:
        with pytest.raises(FileNotFoundError):
            raise PortNotFoundError(errno.ENOENT, "nope", "/dev/ttyX")

    def test_except_anyio_closed_catches_serialclosed(self) -> None:
        with pytest.raises(anyio.ClosedResourceError):
            raise SerialClosedError(errno.EBADF, "closed")

    def test_except_anyio_broken_catches_disconnected(self) -> None:
        with pytest.raises(anyio.BrokenResourceError):
            raise SerialDisconnectedError(errno.EIO, "gone")


class TestErrnoMapping:
    def test_enoent_on_open_is_portnotfound(self) -> None:
        src = OSError(errno.ENOENT, "no such device", "/dev/ttyX")
        mapped = errno_to_exception(src, context="open")
        assert isinstance(mapped, PortNotFoundError)
        assert mapped.__cause__ is src
        assert mapped.errno == errno.ENOENT
        assert mapped.filename == "/dev/ttyX"

    def test_enxio_on_open_is_portnotfound(self) -> None:
        src = OSError(errno.ENXIO, "no such device", None)
        mapped = errno_to_exception(src, context="open", path="/dev/ttyY")
        assert isinstance(mapped, PortNotFoundError)
        assert mapped.filename == "/dev/ttyY"

    def test_ebusy_is_portbusy(self) -> None:
        src = OSError(errno.EBUSY, "locked", "/dev/ttyUSB0")
        mapped = errno_to_exception(src, context="lock")
        assert isinstance(mapped, PortBusyError)

    def test_einval_on_ioctl_is_unsupportedfeature(self) -> None:
        src = OSError(errno.EINVAL, "unknown ioctl", None)
        mapped = errno_to_exception(src, context="ioctl")
        assert isinstance(mapped, UnsupportedFeatureError)

    def test_enotty_on_ioctl_is_unsupportedfeature(self) -> None:
        src = OSError(errno.ENOTTY, "not a tty", None)
        mapped = errno_to_exception(src, context="ioctl")
        assert isinstance(mapped, UnsupportedFeatureError)

    def test_eio_on_io_is_disconnected(self) -> None:
        src = OSError(errno.EIO, "i/o", "/dev/ttyUSB0")
        mapped = errno_to_exception(src, context="io")
        assert isinstance(mapped, SerialDisconnectedError)

    def test_unknown_errno_falls_back_to_serialerror(self) -> None:
        src = OSError(errno.EPERM, "nope", None)
        mapped = errno_to_exception(src, context="io")
        # Not disconnected, not known — plain SerialError.
        assert type(mapped) is SerialError
        assert mapped.__cause__ is src
