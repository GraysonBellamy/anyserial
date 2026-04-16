"""Tests for :func:`anyserial._windows._errors.winerror_to_exception`.

The helper is the bridge between Win32 ``OSError(winerror=...)`` and the
:class:`anyserial.exceptions.SerialError` hierarchy. Covers the full
design-windows-backend.md §9 matrix.
"""

from __future__ import annotations

import pytest

from anyserial._windows import _win32 as w
from anyserial._windows._errors import winerror_to_exception
from anyserial.exceptions import (
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialDisconnectedError,
    SerialError,
    UnsupportedConfigurationError,
)


def _winerror(code: int, *, msg: str = "winerr", filename: str | None = None) -> OSError:
    """Build an ``OSError`` shaped like ``ctypes.WinError`` returns."""
    exc = OSError(0, msg, filename)
    exc.winerror = code  # type: ignore[attr-defined]
    return exc


class TestMapping:
    def test_file_not_found_maps_to_port_not_found(self) -> None:
        out = winerror_to_exception(_winerror(w.ERROR_FILE_NOT_FOUND), context="open", path="COM99")
        assert isinstance(out, PortNotFoundError)
        assert out.filename == "COM99"

    @pytest.mark.parametrize("code", [w.ERROR_ACCESS_DENIED, w.ERROR_SHARING_VIOLATION])
    def test_busy_codes_map_to_port_busy(self, code: int) -> None:
        out = winerror_to_exception(_winerror(code), context="open", path="COM1")
        assert isinstance(out, PortBusyError)

    def test_invalid_handle_maps_to_serial_closed(self) -> None:
        out = winerror_to_exception(_winerror(w.ERROR_INVALID_HANDLE), context="io", path="COM1")
        assert isinstance(out, SerialClosedError)

    def test_invalid_parameter_in_ioctl_context_maps_to_unsupported_config(
        self,
    ) -> None:
        out = winerror_to_exception(
            _winerror(w.ERROR_INVALID_PARAMETER), context="ioctl", path="COM1"
        )
        assert isinstance(out, UnsupportedConfigurationError)

    def test_invalid_parameter_in_other_contexts_falls_back_to_serial_error(
        self,
    ) -> None:
        out = winerror_to_exception(
            _winerror(w.ERROR_INVALID_PARAMETER), context="open", path="COM1"
        )
        # Not UnsupportedConfigurationError outside ioctl — open-time
        # invalid params can mean the path is malformed, which is
        # different policy.
        assert type(out) is SerialError

    @pytest.mark.parametrize(
        "code",
        [w.ERROR_DEVICE_REMOVED, w.ERROR_NOT_READY, w.ERROR_GEN_FAILURE],
    )
    def test_disconnect_codes_map_to_serial_disconnected(self, code: int) -> None:
        out = winerror_to_exception(_winerror(code), context="io", path="COM1")
        assert isinstance(out, SerialDisconnectedError)

    def test_operation_aborted_maps_to_serial_closed(self) -> None:
        # ERROR_OPERATION_ABORTED surfaces when aclose() aborts in-flight
        # overlapped ops via PurgeComm or SetCommMask(handle, 0). Both
        # runtimes normally absorb this; if it leaks through, it maps to
        # SerialClosedError so callers get a clean close signal.
        out = winerror_to_exception(_winerror(w.ERROR_OPERATION_ABORTED), context="io", path="COM1")
        assert isinstance(out, SerialClosedError)

    def test_operation_aborted_maps_to_serial_closed_in_ioctl_context(self) -> None:
        out = winerror_to_exception(
            _winerror(w.ERROR_OPERATION_ABORTED), context="ioctl", path="COM1"
        )
        assert isinstance(out, SerialClosedError)

    def test_unknown_code_falls_back_to_serial_error(self) -> None:
        out = winerror_to_exception(_winerror(0xDEAD), context="io", path="COM1")
        assert type(out) is SerialError

    def test_pre_wrapped_serial_error_passes_through(self) -> None:
        original = PortBusyError(0, "already wrapped", "COM1")
        out = winerror_to_exception(original, context="open", path="COM1")
        assert out is original


class TestMetadata:
    def test_winerror_attribute_preserved(self) -> None:
        out = winerror_to_exception(_winerror(w.ERROR_DEVICE_REMOVED), context="io", path="COM7")
        assert out.winerror == w.ERROR_DEVICE_REMOVED  # type: ignore[attr-defined]

    def test_cause_chain_preserved(self) -> None:
        original = _winerror(w.ERROR_FILE_NOT_FOUND, msg="not found")
        out = winerror_to_exception(original, context="open", path="COM1")
        assert out.__cause__ is original
